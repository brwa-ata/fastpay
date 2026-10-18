"""The service: a receipt goes pending, and only FastPay's answer completes it."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from fastpay import conf, service
from fastpay.client import FAILED, PAID, PENDING
from fastpay.exceptions import FastPayAPIError, FastPayError

from . import hooks
from .conftest import DECLINED, PAGE, QR_TEXT, QR_URL, UNPAID
from .conftest import PAID as PAID_STATUS
from .testapp.models import Receipt

pytestmark = pytest.mark.django_db

SUCCESS_URL = 'https://shop.example/payment/return'
CANCEL_URL = 'https://shop.example/payment/return?status=cancelled'
CALLBACK_URL = 'https://shop.example/api/fastpay/callback/'


def start(receipt, **kwargs):
    return service.start_payment(
        receipt,
        success_url=SUCCESS_URL,
        cancel_url=CANCEL_URL,
        callback_url=CALLBACK_URL,
        **kwargs,
    )


def completed(receipt):
    receipt.refresh_from_db()
    return receipt.is_completed


def minutes_from_now(iso):
    return (datetime.fromisoformat(iso) - timezone.now()).total_seconds() / 60


# -- starting -------------------------------------------------------------------


def test_starting_a_payment_leaves_the_receipt_pending(fastpay):
    receipt = Receipt.objects.create(
        amount=Decimal(25000), invoice_no='RV7', is_completed=True
    )

    with patch('fastpay.service.time.time', return_value=1792323005.6):
        payment = start(receipt)

    receipt.refresh_from_db()
    assert payment['redirect_uri'] == PAGE
    assert payment['orderId'] == f'{receipt.pk}T1792323005'
    assert receipt.ref_no == payment['orderId']
    assert receipt.is_completed is False
    fastpay.initiate_payment.assert_called_once_with(
        payment['orderId'],
        Decimal(25000),
        success_url=SUCCESS_URL,
        cancel_url=CANCEL_URL,
        callback_url=CALLBACK_URL,
        cart=None,
        description='Payment RV7',
    )


def test_the_page_says_when_it_stops_being_payable(fastpay):
    """FastPay's page times out after ten minutes."""
    payment = start(Receipt.objects.create(amount=1000))

    assert 9.9 < minutes_from_now(payment['expiresAt']) <= 10


def test_the_urls_default_to_their_settings(fastpay, settings):
    settings.FASTPAY_SUCCESS_URL = SUCCESS_URL
    settings.FASTPAY_CANCEL_URL = CANCEL_URL
    settings.FASTPAY_CALLBACK_URL = CALLBACK_URL
    conf.reset()

    service.start_payment(Receipt.objects.create(amount=1000))

    kwargs = fastpay.initiate_payment.call_args.kwargs
    assert kwargs['success_url'] == SUCCESS_URL
    assert kwargs['cancel_url'] == CANCEL_URL
    assert kwargs['callback_url'] == CALLBACK_URL


def test_a_cancel_url_defaults_to_the_success_url(fastpay):
    service.start_payment(Receipt.objects.create(amount=1000), success_url=SUCCESS_URL)

    assert fastpay.initiate_payment.call_args.kwargs['cancel_url'] == SUCCESS_URL


def test_an_amount_of_its_own_is_charged(fastpay):
    start(Receipt.objects.create(amount=1000), amount=1020)

    assert fastpay.initiate_payment.call_args.args[1] == 1020


def test_the_order_id_takes_the_configured_prefix(fastpay, settings):
    settings.FASTPAY_ORDER_PREFIX = 'LAV'
    conf.reset()
    receipt = Receipt.objects.create(amount=1000)

    payment = start(receipt)

    assert payment['orderId'].startswith(f'LAV{receipt.pk}T')
    assert payment['orderId'].isalnum()


def test_a_prefix_fastpay_would_refuse_stops_the_app(settings):
    settings.FASTPAY_ORDER_PREFIX = 'LAV-'

    with pytest.raises(ImproperlyConfigured):
        conf.get_conf()


def test_a_refused_payment_leaves_the_receipt_alone(fastpay):
    fastpay.initiate_payment.side_effect = FastPayAPIError('no', code=422)
    receipt = Receipt.objects.create(amount=1000, is_completed=True)

    with pytest.raises(FastPayError):
        start(receipt)

    receipt.refresh_from_db()
    assert receipt.ref_no is None
    assert receipt.is_completed is True


def test_an_initiation_with_no_page_is_refused(fastpay):
    fastpay.initiate_payment.return_value = {}
    receipt = Receipt.objects.create(amount=1000)

    with pytest.raises(FastPayAPIError, match='no redirect_uri'):
        start(receipt)

    receipt.refresh_from_db()
    assert receipt.ref_no is None


# -- QR payments ----------------------------------------------------------------


def test_a_qr_payment_hands_the_app_a_link_into_the_fastpay_app(fastpay):
    receipt = Receipt.objects.create(amount=Decimal(25000))

    payment = service.start_qr_payment(
        receipt, client_uri='appfpclientShop', callback_url=CALLBACK_URL
    )

    receipt.refresh_from_db()
    assert receipt.ref_no == payment['orderId']
    assert receipt.is_completed is False
    assert payment['qrUrl'] == QR_URL
    assert payment['qrText'] == QR_TEXT
    query = parse_qs(urlsplit(payment['appLink']).query)
    assert query == {
        'qrdata': [QR_TEXT],
        'clientUri': ['appfpclientShop'],
        'transactionId': [payment['orderId']],
    }
    args, kwargs = fastpay.generate_qr.call_args
    assert args == (payment['orderId'], Decimal(25000))
    assert kwargs['callback_url'] == CALLBACK_URL
    assert kwargs['expires_in'] == 10


def test_a_qr_payment_lasts_as_long_as_it_was_asked_to(fastpay, settings):
    settings.FASTPAY_QR_LIFETIME = 15
    conf.reset()

    payment = service.start_qr_payment(Receipt.objects.create(amount=1000))

    assert fastpay.generate_qr.call_args.kwargs['expires_in'] == 15
    assert 14.9 < minutes_from_now(payment['expiresAt']) <= 15


def test_a_qr_lifetime_fastpay_refuses_stops_the_app(settings):
    settings.FASTPAY_QR_LIFETIME = 30

    with pytest.raises(ImproperlyConfigured):
        conf.get_conf()


def test_the_client_uri_defaults_to_its_setting(fastpay, settings):
    settings.FASTPAY_APP_CLIENT_URI = 'appfpclientShop'
    conf.reset()

    payment = service.start_qr_payment(Receipt.objects.create(amount=1000))

    assert 'clientUri=appfpclientShop' in payment['appLink']


def test_no_client_uri_means_no_app_link(fastpay):
    payment = service.start_qr_payment(Receipt.objects.create(amount=1000))

    assert payment['appLink'] is None
    assert payment['qrUrl'] == QR_URL


# -- completing ------------------------------------------------------------------


def test_a_paid_status_completes_the_receipt_once(fastpay):
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')
    fastpay.check_status.return_value = dict(PAID_STATUS)

    assert service.sync_status(receipt)['payment_status'] == 'PAID'
    service.sync_status(receipt)

    assert completed(receipt)
    assert hooks.calls == [(receipt.pk, 'PAID')]
    fastpay.check_status.assert_called_with('7T1792323005')


def test_an_unpaid_status_changes_nothing(fastpay):
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    assert service.apply_status(receipt, dict(UNPAID)) == PENDING

    assert not completed(receipt)
    assert hooks.calls == []
    assert hooks.failed_calls == []


def test_a_cancelled_payment_is_a_failure_and_never_completes(fastpay, settings):
    settings.FASTPAY_RECEIPT_DETAILS_FIELD = 'meta'
    conf.reset()
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    assert service.apply_status(receipt, dict(DECLINED)) == FAILED

    receipt.refresh_from_db()
    assert receipt.is_completed is False
    assert receipt.meta['fastpay']['payment_status'] == 'DECLINED'
    assert hooks.failed_calls == [(receipt.pk, 'DECLINED')]


def test_a_payment_short_of_the_receipt_is_kept_but_not_completed(fastpay, settings):
    """FastPay asks merchants to check the amount it received."""
    settings.FASTPAY_RECEIPT_DETAILS_FIELD = 'meta'
    conf.reset()
    receipt = Receipt.objects.create(amount=25000, ref_no='7T1792323005')

    state = service.apply_status(receipt, {**PAID_STATUS, 'received_amount': '1000.00'})

    receipt.refresh_from_db()
    assert state == PENDING
    assert receipt.is_completed is False
    assert receipt.meta['fastpay']['received_amount'] == '1000.00'
    assert hooks.calls == []


def test_a_payment_with_no_amount_does_not_complete(fastpay):
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    state = service.apply_status(receipt, {**PAID_STATUS, 'received_amount': 'N/A'})

    assert state == PENDING
    assert not completed(receipt)


def test_the_amount_is_compared_in_the_whole_dinars_charged(fastpay):
    """A receipt of 1000.4 was charged 1000."""
    receipt = Receipt.objects.create(amount=Decimal('1000.40'), ref_no='7T1792323005')

    assert service.apply_status(receipt, dict(PAID_STATUS)) == PAID


def test_paying_more_than_the_receipt_completes_it(fastpay):
    """The charge carries the fee when the customer pays it."""
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    assert (
        service.apply_status(receipt, {**PAID_STATUS, 'received_amount': '1020.00'})
        == PAID
    )


def test_the_amount_check_can_be_switched_off(fastpay, settings):
    settings.FASTPAY_RECEIPT_AMOUNT_FIELD = ''
    conf.reset()
    receipt = Receipt.objects.create(amount=25000, ref_no='7T1792323005')

    service.apply_status(receipt, {**PAID_STATUS, 'received_amount': '1.00'})

    assert completed(receipt)


def test_the_transaction_is_kept_beside_the_fields_other_keys(fastpay, settings):
    settings.FASTPAY_RECEIPT_DETAILS_FIELD = 'meta'
    conf.reset()
    receipt = Receipt.objects.create(
        amount=1000, ref_no='7T1792323005', meta={'note': 'kept'}
    )

    service.apply_status(receipt, dict(PAID_STATUS))

    receipt.refresh_from_db()
    assert receipt.meta['note'] == 'kept'
    assert receipt.meta['fastpay']['gw_transaction_id'] == 'A1KSO55655'
    assert receipt.meta['fastpay']['customer_mobile_number'] == '+9647988776655'


def test_a_failure_never_replaces_a_completed_transaction(fastpay, settings):
    settings.FASTPAY_RECEIPT_DETAILS_FIELD = 'meta'
    conf.reset()
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')
    service.apply_status(receipt, dict(PAID_STATUS))

    service.apply_status(receipt, dict(DECLINED))

    receipt.refresh_from_db()
    assert receipt.is_completed
    assert receipt.meta['fastpay']['payment_status'] == 'PAID'


def test_a_broken_hook_does_not_undo_the_payment(fastpay, settings):
    settings.FASTPAY_ON_PAYMENT_COMPLETED = 'tests.hooks.explode'
    conf.reset()
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    assert service.apply_status(receipt, dict(PAID_STATUS)) == PAID

    assert completed(receipt)


def test_syncing_a_receipt_without_an_order_is_a_bug(fastpay):
    with pytest.raises(ValueError):
        service.sync_status(Receipt.objects.create(amount=1000))


# -- the notification ------------------------------------------------------------


def test_the_notification_re_reads_the_status(fastpay):
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')
    fastpay.check_status.return_value = dict(PAID_STATUS)

    assert (
        service.handle_callback('7T1792323005', claimed={'status': 'Success'})
        == receipt
    )

    fastpay.check_status.assert_called_once_with('7T1792323005')
    assert completed(receipt)


def test_a_forged_notification_does_nothing(fastpay):
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    service.handle_callback('7T1792323005', claimed={'status': 'Success'})

    assert not completed(receipt)


def test_a_notification_for_an_unknown_order_is_ignored(fastpay):
    assert service.handle_callback('9T1792323005') is None

    fastpay.check_status.assert_not_called()


def test_a_project_must_name_its_receipt_model(settings):
    settings.FASTPAY_RECEIPT_MODEL = ''

    with pytest.raises(ImproperlyConfigured):
        service.get_receipt_model()


def test_an_unknown_environment_stops_the_app(settings):
    settings.FASTPAY_ENVIRONMENT = 'live'

    with pytest.raises(ImproperlyConfigured):
        conf.get_conf()


def test_the_shared_client_follows_the_environment(settings):
    settings.FASTPAY_ENVIRONMENT = 'production'

    client = conf.get_client()

    assert client.base_url == 'https://apigw-merchant.fast-pay.iq'
    assert conf.get_client() is client


def test_the_receipt_the_caller_holds_shows_the_completion(fastpay):
    """The row is completed through a locked copy; the caller's is refreshed."""
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')
    held = Receipt.objects.get(pk=receipt.pk)

    service.apply_status(held, dict(PAID_STATUS))

    assert held.is_completed is True
