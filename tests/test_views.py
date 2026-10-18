"""The HTTP surface: the public notification, and the two payment endpoints."""

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from fastpay import conf
from fastpay.exceptions import FastPayAPIError

from .conftest import PAGE, QR_TEXT, QR_URL, UNPAID
from .conftest import PAID as PAID_STATUS
from .testapp.models import Receipt

pytestmark = pytest.mark.django_db

CALLBACK_URL = '/api/fastpay/callback/'
PAYMENTS_URL = '/api/fastpay/payments/'
SUCCESS_URL = 'https://shop.example/payment/return'

# FastPay's notification (IPN) body, as its documentation gives it
IPN_BODY = {
    'gw_transaction_id': 'A1KSO55655',
    'merchant_order_id': '7T1792323005',
    'received_amount': '1000.00',
    'currency': 'IQD',
    'customer_name': 'Sandbox User',
    'customer_mobile_number': '+9647988776655',
    'at': '2026-10-18 14:30:25',
    'transaction_id': 'A1KSO55655',
    'order_id': '7T1792323005',
    'customer_account_no': '+9647988776655',
    'status': 'Success',
    'received_at': '2026-10-18 14:30:25',
}


@pytest.fixture
def receipt():
    return Receipt.objects.create(amount=1000, ref_no='7T1792323005')


@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(User.objects.create_user('staff', is_staff=True))
    return client


@pytest.fixture
def customer():
    client = APIClient()
    client.force_authenticate(User.objects.create_user('customer'))
    return client


def completed(receipt):
    receipt.refresh_from_db()
    return receipt.is_completed


# -- the notification ------------------------------------------------------------


def test_the_notification_is_public_and_re_verified(client, fastpay, receipt):
    fastpay.check_status.return_value = dict(PAID_STATUS)

    response = client.post(CALLBACK_URL, IPN_BODY, content_type='application/json')

    assert response.status_code == 200
    fastpay.check_status.assert_called_once_with('7T1792323005')
    assert completed(receipt)


def test_a_forged_notification_completes_nothing(client, fastpay, receipt):
    response = client.post(CALLBACK_URL, IPN_BODY, content_type='application/json')

    assert response.status_code == 200
    assert not completed(receipt)


def test_the_order_may_come_as_merchant_order_id_and_a_form(client, fastpay, receipt):
    fastpay.check_status.return_value = dict(PAID_STATUS)

    response = client.post(CALLBACK_URL, {'merchant_order_id': '7T1792323005'})

    assert response.status_code == 200
    assert completed(receipt)


def test_a_notification_without_an_order_is_refused(client, fastpay):
    response = client.post(CALLBACK_URL, {}, content_type='application/json')

    assert response.status_code == 400
    fastpay.check_status.assert_not_called()


def test_an_unknown_order_is_acknowledged(client, fastpay):
    response = client.post(
        CALLBACK_URL, {'order_id': '9T1792323005'}, content_type='application/json'
    )

    assert response.status_code == 200


def test_fastpay_out_of_reach_asks_for_a_retry(client, fastpay, receipt):
    fastpay.check_status.side_effect = FastPayAPIError('down')

    response = client.post(CALLBACK_URL, IPN_BODY, content_type='application/json')

    assert response.status_code == 503
    assert not completed(receipt)


# -- starting a payment ---------------------------------------------------------


def test_staff_start_a_payment_on_the_page(staff, fastpay, receipt):
    response = staff.post(
        PAYMENTS_URL,
        {'receipt_id': receipt.pk, 'success_url': SUCCESS_URL},
        format='json',
    )

    assert response.status_code == 201, response.content
    data = response.json()
    assert data['redirect_uri'] == PAGE
    assert set(data) == {'orderId', 'redirect_uri', 'expiresAt'}
    kwargs = fastpay.initiate_payment.call_args.kwargs
    assert kwargs['success_url'] == SUCCESS_URL
    assert kwargs['callback_url'] == f'http://testserver{CALLBACK_URL}'


def test_the_callback_url_setting_wins(staff, fastpay, receipt, settings):
    settings.FASTPAY_CALLBACK_URL = 'https://api.shop.example/api/fastpay/callback/'
    conf.reset()

    staff.post(PAYMENTS_URL, {'receipt_id': receipt.pk}, format='json')

    assert (
        fastpay.initiate_payment.call_args.kwargs['callback_url']
        == 'https://api.shop.example/api/fastpay/callback/'
    )


def test_staff_start_a_qr_payment(staff, fastpay, receipt):
    response = staff.post(
        PAYMENTS_URL,
        {'receipt_id': receipt.pk, 'mode': 'qr', 'client_uri': 'Shop'},
        format='json',
    )

    assert response.status_code == 201, response.content
    data = response.json()
    assert data['qrUrl'] == QR_URL
    assert data['qrText'] == QR_TEXT
    assert data['appLink'].startswith('appFpp://fast-pay.cash/qrpay?')
    assert 'clientUri=appfpclientShop' in data['appLink']


def test_an_unknown_mode_is_refused(staff, fastpay, receipt):
    response = staff.post(
        PAYMENTS_URL, {'receipt_id': receipt.pk, 'mode': 'sms'}, format='json'
    )

    assert response.status_code == 422


def test_a_receipt_is_required(staff, fastpay):
    assert staff.post(PAYMENTS_URL, {}, format='json').status_code == 422


def test_fastpays_refusal_is_reported(staff, fastpay, receipt):
    fastpay.initiate_payment.side_effect = FastPayAPIError('wrong store', code=422)

    response = staff.post(PAYMENTS_URL, {'receipt_id': receipt.pk}, format='json')

    assert response.status_code == 502
    assert response.json()['message'] == 'wrong store'


def test_a_customer_may_not_use_the_staff_endpoints(customer, fastpay, receipt):
    response = customer.post(PAYMENTS_URL, {'receipt_id': receipt.pk}, format='json')

    assert response.status_code == 403
    fastpay.initiate_payment.assert_not_called()


def test_who_may_is_a_setting(customer, fastpay, receipt, settings):
    settings.FASTPAY_VIEW_PERMISSION_CLASSES = [
        'rest_framework.permissions.IsAuthenticated'
    ]
    conf.reset()

    response = customer.post(PAYMENTS_URL, {'receipt_id': receipt.pk}, format='json')

    assert response.status_code == 201


# -- the status ------------------------------------------------------------------


def test_the_status_is_re_synced(staff, fastpay, receipt):
    fastpay.check_status.return_value = dict(PAID_STATUS)

    response = staff.get(f'{PAYMENTS_URL}{receipt.pk}/status/')

    assert response.status_code == 200
    data = response.json()
    assert data['state'] == 'PAID'
    assert data['is_completed'] is True
    assert data['transaction']['gw_transaction_id'] == 'A1KSO55655'


def test_an_unpaid_status(staff, fastpay, receipt):
    fastpay.check_status.return_value = dict(UNPAID)

    data = staff.get(f'{PAYMENTS_URL}{receipt.pk}/status/').json()

    assert data['state'] == 'PENDING'
    assert data['is_completed'] is False


def test_a_status_fastpay_refuses_is_reported(staff, fastpay, receipt):
    fastpay.check_status.side_effect = FastPayAPIError('down')

    response = staff.get(f'{PAYMENTS_URL}{receipt.pk}/status/')

    assert response.status_code == 502
