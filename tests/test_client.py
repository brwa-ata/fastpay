"""The raw client: what is sent to FastPay, and reading its answers.

The answers mocked here are the shapes FastPay's staging environment gave: the
payment gateway answers HTTP 200 with its own ``code`` in the body, the QR host
the real HTTP status.
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from fastpay.client import (
    FAILED,
    HEADERS,
    PAID,
    PENDING,
    FastPayClient,
    app_link,
    format_amount,
    parse_amount,
    transaction_state,
)
from fastpay.exceptions import FastPayAPIError, FastPayAuthError, FastPayError

from .conftest import DECLINED, PAGE, QR_TEXT, QR_URL, UNPAID
from .conftest import PAID as PAID_STATUS

PGW = 'https://staging-apigw-merchant.fast-pay.iq/api/v1/public/pgw/payment'
QR = 'https://staging-qr.fast-pay.iq/api/v1/public/vending'
ORDER_ID = '7T1792323005'


def make_client(**kwargs):
    return FastPayClient('749347_291', 'store-password', **kwargs)


def response(status_code=200, body=None):
    resp = MagicMock(status_code=status_code, content=b'x', text=json.dumps(body))
    resp.json.return_value = body
    return resp


def pgw(data=None, code=200, messages=()):
    """The payment gateway's answer: always HTTP 200."""
    return response(200, {'code': code, 'messages': list(messages), 'data': data})


def qr(data=None, code=200, errors=(), message=''):
    """The QR host's answer: the HTTP status is the code."""
    body = {'code': code, 'message': message, 'errors': list(errors), 'data': data}
    return response(code, body)


def posted(post):
    """(url, json body) of the one call made."""
    return post.call_args.args[0], post.call_args.kwargs['json']


# -- setting up -------------------------------------------------------------------


def test_staging_is_the_default_environment():
    client = make_client()

    assert client.base_url == 'https://staging-apigw-merchant.fast-pay.iq'
    assert client.qr_base_url == 'https://staging-qr.fast-pay.iq'


def test_production_picks_both_live_hosts():
    client = make_client(environment='production')

    assert client.base_url == 'https://apigw-merchant.fast-pay.iq'
    assert client.qr_base_url == 'https://qr.fast-pay.iq'


def test_a_host_can_be_overridden():
    client = make_client(base_url='https://pgw.example/', qr_base_url='https://qr.x/')

    assert client.base_url == 'https://pgw.example'
    assert client.qr_base_url == 'https://qr.x'


def test_an_unknown_environment_is_refused():
    with pytest.raises(ValueError, match='sandbox'):
        make_client(environment='sandbox')


@pytest.mark.parametrize('store_id, password', [('', 'p'), ('749347_291', '')])
def test_the_store_credentials_are_required(store_id, password):
    with pytest.raises(FastPayAuthError):
        FastPayClient(store_id, password)


# -- the payment page ---------------------------------------------------------------


def test_initiation_posts_the_order_with_its_cart_as_a_json_string():
    with patch(
        'fastpay.client.requests.post', return_value=pgw({'redirect_uri': PAGE})
    ) as post:
        answer = make_client().initiate_payment(
            ORDER_ID,
            Decimal('25000.00'),
            success_url='https://shop.example/return',
            cancel_url='https://shop.example/return?status=cancelled',
            callback_url='https://shop.example/api/fastpay/callback/',
            description='Payment RV7',
        )

    url, body = posted(post)
    assert answer == {'redirect_uri': PAGE}
    assert url == f'{PGW}/initiation'
    assert body == {
        'store_id': '749347_291',
        'store_password': 'store-password',
        'order_id': ORDER_ID,
        'bill_amount': 25000,
        'currency': 'IQD',
        # FastPay: "The cart must be a valid JSON string."
        'cart': json.dumps(
            [{'name': 'Payment RV7', 'qty': 1, 'unit_price': 25000, 'sub_total': 25000}]
        ),
        'success_url': 'https://shop.example/return',
        'cancel_url': 'https://shop.example/return?status=cancelled',
        'callback_url': 'https://shop.example/api/fastpay/callback/',
    }


def test_initiation_sends_only_the_urls_it_is_given():
    with patch(
        'fastpay.client.requests.post', return_value=pgw({'redirect_uri': PAGE})
    ) as post:
        make_client().initiate_payment(ORDER_ID, 1000)

    body = posted(post)[1]
    assert not {'success_url', 'cancel_url', 'callback_url'} & body.keys()
    assert json.loads(body['cart'])[0]['name'] == f'Order {ORDER_ID}'


def test_a_cart_of_its_own_is_sent_as_given():
    cart = [{'name': 'Scarf', 'qty': 2, 'unit_price': 500, 'sub_total': 1000}]
    with patch(
        'fastpay.client.requests.post', return_value=pgw({'redirect_uri': PAGE})
    ) as post:
        make_client().initiate_payment(ORDER_ID, 1000, cart=cart)

    assert json.loads(posted(post)[1]['cart']) == cart


def test_amounts_go_out_as_whole_dinars():
    """FastPay: "The bill amount must be an integer." """
    with patch(
        'fastpay.client.requests.post', return_value=pgw({'redirect_uri': PAGE})
    ) as post:
        make_client().initiate_payment(ORDER_ID, 25500.5)

    assert posted(post)[1]['bill_amount'] == 25501


def test_every_call_names_the_client():
    """Cloudflare in front of FastPay refuses Python's urllib user agent."""
    with patch(
        'fastpay.client.requests.post', return_value=pgw({'redirect_uri': PAGE})
    ) as post:
        make_client(timeout=7).initiate_payment(ORDER_ID, 1000)

    assert post.call_args.kwargs['headers'] is HEADERS
    assert HEADERS['User-Agent'].startswith('fastpay/')
    assert post.call_args.kwargs['timeout'] == 7


def test_a_refusal_at_http_200_is_raised_with_fastpays_reason():
    refused = pgw(
        code=422,
        messages=[
            (
                'Sorry! Provided Order ID matched with a previous Order ID. Kindly '
                'use a unique Order ID each time you request.'
            )
        ],
    )
    with (
        patch('fastpay.client.requests.post', return_value=refused),
        pytest.raises(FastPayAPIError, match='matched with a previous') as raised,
    ):
        make_client().initiate_payment(ORDER_ID, 1000)

    assert raised.value.code == 422
    assert raised.value.status_code == 200
    assert raised.value.payload['data'] is None


@pytest.mark.parametrize('order_id', ['A1', 'LAV-12T1792323005', 'x' * 33, ''])
def test_an_order_id_fastpay_would_refuse_is_never_sent(order_id):
    with (
        patch('fastpay.client.requests.post') as post,
        pytest.raises(ValueError, match='8 to 32 letters and digits'),
    ):
        make_client().initiate_payment(order_id, 1000)

    post.assert_not_called()


def test_validate_answers_the_paid_transaction():
    paid = {
        'gw_transaction_id': 'A1KSO55655',
        'received_amount': '1000.00',
        'status': 'Success',
    }
    with patch('fastpay.client.requests.post', return_value=pgw(paid)) as post:
        answer = make_client().validate_payment(ORDER_ID)

    assert answer == paid
    assert posted(post) == (
        f'{PGW}/validate',
        {
            'store_id': '749347_291',
            'store_password': 'store-password',
            'order_id': ORDER_ID,
        },
    )


def test_validate_refuses_an_unpaid_order_with_a_404_code():
    unpaid = pgw(
        code=404,
        messages=['Sorry! No transaction has been found against your Order ID.'],
    )
    with (
        patch('fastpay.client.requests.post', return_value=unpaid),
        pytest.raises(FastPayAPIError) as raised,
    ):
        make_client().validate_payment(ORDER_ID)

    assert raised.value.code == 404


def test_a_refund_posts_the_secret_key_and_the_wallet():
    done = {
        'summary': {
            'recipient': {'mobile_number': '+9647988776655'},
            'invoice_id': 'C1',
        }
    }
    with patch('fastpay.client.requests.post', return_value=pgw(done)) as post:
        answer = make_client().refund(
            ORDER_ID, 1000, msisdn='+9647988776655', refund_secret_key='S3CR3T'
        )

    url, body = posted(post)
    assert answer == done
    assert url == f'{PGW}/refund'
    assert body['amount'] == 1000
    assert body['msisdn'] == '+9647988776655'
    assert body['refund_secret_key'] == 'S3CR3T'


def test_refund_status_reads_the_refund_validation():
    answer = {'order_id': ORDER_ID, 'refund_status': False}
    with patch('fastpay.client.requests.post', return_value=pgw(answer)) as post:
        assert make_client().refund_status(ORDER_ID) == answer

    assert posted(post)[0] == f'{PGW}/refund/validation'


# -- QR payments ------------------------------------------------------------------


def test_a_qr_payment_is_asked_for_in_camel_case():
    with patch(
        'fastpay.client.requests.post',
        return_value=qr({'qrUrl': QR_URL, 'qrText': QR_TEXT}),
    ) as post:
        answer = make_client().generate_qr(
            ORDER_ID,
            1000,
            success_url='https://shop.example/return',
            callback_url='https://shop.example/api/fastpay/callback/',
            expires_in=15,
        )

    url, body = posted(post)
    assert answer == {'qrUrl': QR_URL, 'qrText': QR_TEXT}
    assert url == f'{QR}/qr'
    assert body == {
        'storeId': '749347_291',
        'storePassword': 'store-password',
        'orderId': ORDER_ID,
        'billAmount': 1000,
        'currency': 'IQD',
        'successUrl': 'https://shop.example/return',
        'callbackUrl': 'https://shop.example/api/fastpay/callback/',
        'expiresIn': 15,
    }


@pytest.mark.parametrize('minutes', [2, 16])
def test_a_qr_lifetime_fastpay_refuses_is_never_sent(minutes):
    with (
        patch('fastpay.client.requests.post') as post,
        pytest.raises(ValueError, match='3 to 15 minutes'),
    ):
        make_client().generate_qr(ORDER_ID, 1000, expires_in=minutes)

    post.assert_not_called()


def test_the_qr_host_refuses_with_its_http_status_and_errors():
    refused = qr(
        code=400,
        message='Invalid Request',
        errors=['expiresIn: The expires in must not be greater than 15 minutes.'],
    )
    with (
        patch('fastpay.client.requests.post', return_value=refused),
        pytest.raises(FastPayAPIError, match='expires in must not') as raised,
    ):
        make_client().generate_qr(ORDER_ID, 1000)

    assert raised.value.code == 400
    assert raised.value.status_code == 400


# -- status -------------------------------------------------------------------------


def test_the_status_is_read_from_the_qr_host():
    with patch(
        'fastpay.client.requests.post', return_value=qr(dict(PAID_STATUS))
    ) as post:
        answer = make_client().payment_status(ORDER_ID)

    assert answer['payment_status'] == 'PAID'
    assert posted(post) == (
        f'{QR}/status',
        {
            'storeId': '749347_291',
            'storePassword': 'store-password',
            'orderId': ORDER_ID,
        },
    )


def test_check_status_is_the_status_when_the_qr_host_answers():
    with patch('fastpay.client.requests.post', return_value=qr(dict(UNPAID))) as post:
        assert make_client().check_status(ORDER_ID) == UNPAID

    assert post.call_count == 1


def test_check_status_falls_back_to_validate_when_the_qr_host_refuses():
    refused = qr(code=400, message='Unsuccessful attempt', errors=['no'])
    paid = pgw(
        {
            'gw_transaction_id': 'A1KSO55655',
            'received_amount': '1000.00',
            'status': 'Success',
        }
    )
    with patch('fastpay.client.requests.post', side_effect=[refused, paid]) as post:
        answer = make_client().check_status(ORDER_ID)

    assert post.call_args.args[0] == f'{PGW}/validate'
    assert answer['payment_status'] == 'PAID'
    assert answer['gw_transaction_id'] == 'A1KSO55655'


def test_check_status_falls_back_when_the_qr_host_is_not_fastpays_json():
    """A route that is gone answers a bare 404 page, with no code of FastPay's."""
    gone = response(404, {'message': ''})
    paid = pgw({'received_amount': '1000.00', 'status': 'Success'})
    with patch('fastpay.client.requests.post', side_effect=[gone, paid]):
        answer = make_client().check_status(ORDER_ID)

    assert answer['payment_status'] == 'PAID'


def test_check_status_reads_validates_404_as_unpaid():
    refused = qr(code=400, message='Unsuccessful attempt', errors=['no'])
    unpaid = pgw(
        code=404,
        messages=['Sorry! No transaction has been found against your Order ID.'],
    )
    with patch('fastpay.client.requests.post', side_effect=[refused, unpaid]):
        answer = make_client().check_status(ORDER_ID)

    assert answer == {'order_id': ORDER_ID, 'payment_status': 'UNPAID'}


def test_check_status_raises_what_validate_refuses_otherwise():
    refused = qr(code=400, message='Unsuccessful attempt', errors=['wrong store'])
    wrong = pgw(
        code=422,
        messages=['Sorry! The Store ID and Store Password combination is wrong.'],
    )
    with (
        patch('fastpay.client.requests.post', side_effect=[refused, wrong]),
        pytest.raises(FastPayAPIError, match='combination is wrong'),
    ):
        make_client().check_status(ORDER_ID)


def test_check_status_does_not_fall_back_when_fastpay_did_not_answer():
    with (
        patch(
            'fastpay.client.requests.post', side_effect=requests.ConnectionError('down')
        ) as post,
        pytest.raises(FastPayAPIError) as raised,
    ):
        make_client().check_status(ORDER_ID)

    assert raised.value.code is None
    assert post.call_count == 1


# -- failures ---------------------------------------------------------------------


def test_a_network_failure_is_a_fastpay_error():
    with (
        patch('fastpay.client.requests.post', side_effect=requests.Timeout('slow')),
        pytest.raises(FastPayError, match='request to .* failed: slow'),
    ):
        make_client().payment_status(ORDER_ID)


def test_cloudflares_refusal_is_read_too():
    blocked = response(
        403,
        {
            'title': 'Error 1010: Access denied',
            'status': 403,
            'detail': "The site owner has blocked access based on your browser's signature.",
        },
    )
    with (
        patch('fastpay.client.requests.post', return_value=blocked),
        pytest.raises(FastPayAPIError, match='blocked access') as raised,
    ):
        make_client().initiate_payment(ORDER_ID, 1000)

    assert raised.value.code is None
    assert raised.value.status_code == 403


def test_a_page_that_is_not_json_is_refused():
    page = MagicMock(
        status_code=502, content=b'<html>', text='<html>Bad gateway</html>'
    )
    page.json.side_effect = ValueError
    with (
        patch('fastpay.client.requests.post', return_value=page),
        pytest.raises(FastPayAPIError, match='Bad gateway'),
    ):
        make_client().payment_status(ORDER_ID)


def test_a_success_code_without_an_object_is_no_answer():
    with (
        patch('fastpay.client.requests.post', return_value=pgw(None)),
        pytest.raises(FastPayAPIError, match='no JSON object'),
    ):
        make_client().validate_payment(ORDER_ID)


# -- reading answers ------------------------------------------------------------------


@pytest.mark.parametrize(
    'data, state',
    [
        (PAID_STATUS, PAID),
        (UNPAID, PENDING),
        (DECLINED, FAILED),
        ({'payment_status': 'paid'}, PAID),
        ({'payment_status': 'SOMETHING_NEW'}, PENDING),
        # a validate answer, or FastPay's notification
        ({'status': 'Success'}, PAID),
        ({'status': 'success'}, PAID),
        ({'status': 'Failed'}, FAILED),
        ({'status': 'Cancel'}, FAILED),
        ({'status': ''}, PENDING),
        ({}, PENDING),
        (None, PENDING),
    ],
)
def test_transaction_state(data, state):
    assert transaction_state(data) == state


def test_an_app_link_hands_the_code_to_the_fastpay_app():
    link = app_link(QR_TEXT, ORDER_ID, 'appfpclientLavender')

    assert link.startswith('appFpp://fast-pay.cash/qrpay?')
    assert parse_qs(urlsplit(link).query) == {
        'qrdata': [QR_TEXT],
        'clientUri': ['appfpclientLavender'],
        'transactionId': [ORDER_ID],
    }
    # the code's spaces travel as %20: iOS reads a + literally
    assert '+' not in link
    assert 'FastPay%20Test' in link


def test_an_app_link_adds_fastpays_prefix_to_a_bare_app_name():
    link = app_link(QR_TEXT, ORDER_ID, 'Lavender')

    assert parse_qs(urlsplit(link).query)['clientUri'] == ['appfpclientLavender']


def test_an_app_link_needs_a_client_uri():
    with pytest.raises(ValueError):
        app_link(QR_TEXT, ORDER_ID, '')


@pytest.mark.parametrize(
    'amount, whole',
    [(1000, 1000), ('1000.4', 1000), (Decimal('1000.5'), 1001), (25500.5, 25501)],
)
def test_format_amount(amount, whole):
    assert format_amount(amount) == whole


@pytest.mark.parametrize(
    'value, amount',
    [
        ('1000.0000', Decimal(1000)),
        ('1000.00', Decimal(1000)),
        (1000.0, Decimal(1000)),
        ('N/A', None),
        (None, None),
    ],
)
def test_parse_amount(value, amount):
    assert parse_amount(value) == amount
