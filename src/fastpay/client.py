"""Standalone client for the FastPay payment gateway API.

This module has **no Django dependency** on purpose: it only needs ``requests``.
Copy it into any project and drive it with plain config values.

Contract (verified against FastPay's staging environment; where FastPay's
documentation says otherwise, the gateway's actual answer is what is coded).
FastPay runs two hosts over **one order space**:

* the payment gateway (PGW): FastPay's own payment page, where the customer
  scans a QR code or logs in with their wallet and an OTP::

      POST {base_url}/api/v1/public/pgw/payment/initiation
      POST {base_url}/api/v1/public/pgw/payment/validate
      POST {base_url}/api/v1/public/pgw/payment/refund
      POST {base_url}/api/v1/public/pgw/payment/refund/validation

* the QR API: a QR code to scan, or to hand to the FastPay app by deep link::

      POST {qr_base_url}/api/v1/public/vending/qr
      POST {qr_base_url}/api/v1/public/vending/status

Every call carries the store credentials in its body; there is no token. An
order id is unique across both hosts, and ``vending/status`` reads an order
started on either: ``PAID``, ``UNPAID``, or ``DECLINED`` once the customer
cancels on the page. :func:`transaction_state` reads it.

The PGW answers HTTP 200 whatever happens, with the outcome in the body --
``{"code": 200, "messages": [...], "data": {...}}``, and a refusal
``{"code": 422, "messages": ["<reason>"], "data": null}``. The QR host answers
with the real HTTP status and ``{"code", "message", "errors": [...], "data"}``.
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import quote, urlencode

import requests

from .exceptions import FastPayAPIError, FastPayAuthError

DEFAULT_TIMEOUT = 30

# (payment gateway, QR API) base URLs per environment
ENVIRONMENTS = {
    'staging': (
        'https://staging-apigw-merchant.fast-pay.iq',
        'https://staging-qr.fast-pay.iq',
    ),
    'production': (
        'https://apigw-merchant.fast-pay.iq',
        'https://qr.fast-pay.iq',
    ),
}

# the only currency FastPay takes
CURRENCY_IQD = 'IQD'
# FastPay: "alphanumeric, between 8-32 characters", unique per store
ORDER_ID_PATTERN = re.compile(r'[A-Za-z0-9]{8,32}')

# minutes FastPay's payment page stays payable; FastPay sets it, not us
PAGE_LIFETIME = 10
# minutes a QR payment may be made to last (its `expiresIn`), and the default
QR_MIN_LIFETIME = 3
QR_MAX_LIFETIME = 15
QR_DEFAULT_LIFETIME = 10

# what transaction_state() makes of an answer
PAID = 'PAID'
FAILED = 'FAILED'
PENDING = 'PENDING'

# `payment_status` in a vending/status answer
STATUS_PAID = 'PAID'
STATUS_UNPAID = 'UNPAID'
STATUS_DECLINED = 'DECLINED'
# `status` in a validate answer or an IPN, as FastPay's docs list them
FAILED_STATUSES = frozenset({'failed', 'cancel', 'cancelled', 'canceled', 'declined'})

# opens the FastPay app on a QR payment (FastPay's "deep link integration")
APP_LINK = 'appFpp://fast-pay.cash/qrpay'
# the FastPay app sends the customer back to `<this prefix><app name>://`
CLIENT_URI_PREFIX = 'appfpclient'

try:
    _VERSION = version('fastpay')
except PackageNotFoundError:  # client.py copied on its own
    _VERSION = 'dev'

# FastPay sits behind Cloudflare, which refuses Python's own urllib user agent
# on initiation (error 1010). Naming ourselves keeps the client clear of that.
HEADERS = {
    'Accept': 'application/json',
    'User-Agent': f'fastpay/{_VERSION} (+https://github.com/brwa-ata/fastpay)',
}


class FastPayClient:
    """Thin, reusable wrapper around FastPay's merchant endpoints.

    ``environment`` picks both hosts (``staging`` or ``production``);
    ``base_url`` / ``qr_base_url`` override either one.
    """

    def __init__(
        self,
        store_id,
        store_password,
        *,
        environment='staging',
        base_url=None,
        qr_base_url=None,
        timeout=DEFAULT_TIMEOUT,
    ):
        if not store_id or not store_password:
            raise FastPayAuthError(
                'FastPay client requires store_id and store_password.'
            )
        if environment not in ENVIRONMENTS:
            raise ValueError(
                f'FastPay environment is one of {sorted(ENVIRONMENTS)}, '
                f'not {environment!r}.'
            )

        pgw_url, qr_url = ENVIRONMENTS[environment]
        self.store_id = store_id
        self.store_password = store_password
        self.base_url = (base_url or pgw_url).rstrip('/')
        self.qr_base_url = (qr_base_url or qr_url).rstrip('/')
        self.timeout = timeout

    # -- urls ---------------------------------------------------------------
    def _pgw_url(self, path):
        return f'{self.base_url}/api/v1/public/pgw/payment/{path}'

    def _qr_url(self, path):
        return f'{self.qr_base_url}/api/v1/public/vending/{path}'

    # -- requests -----------------------------------------------------------
    def _pgw_credentials(self):
        return {'store_id': self.store_id, 'store_password': self.store_password}

    def _qr_credentials(self):
        return {'storeId': self.store_id, 'storePassword': self.store_password}

    def _post(self, url, body):
        try:
            resp = requests.post(url, json=body, headers=HEADERS, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FastPayAPIError(f'FastPay request to {url} failed: {exc}') from exc

        payload = _safe_json(resp)
        code = _code(payload)
        if resp.status_code >= 400 or code != 200:
            raise FastPayAPIError(
                f'FastPay {url} returned {code or resp.status_code}: '
                f'{_reason(payload, resp)}',
                status_code=resp.status_code,
                code=code,
                payload=payload,
            )

        return payload.get('data')

    # -- the payment page -----------------------------------------------------
    def initiate_payment(
        self,
        order_id,
        amount,
        *,
        success_url=None,
        cancel_url=None,
        callback_url=None,
        cart=None,
        description='',
    ):
        """Start a payment on FastPay's page and return FastPay's answer.

        ``order_id`` is ours: 8 to 32 letters and digits, never used before
        (on either host). The customer is sent to ``success_url`` or
        ``cancel_url`` afterwards with ``?order_id=...`` added, by their own
        browser, so those may be any address the customer can open.
        ``callback_url`` is where FastPay posts its notification (IPN) after a
        successful payment; it must be public to arrive.

        ``cart`` is a list of ``{name, qty, unit_price, sub_total}``; by
        default one line named ``description`` for the whole amount.

        Answer key: ``redirect_uri``, the page to send the customer to.
        """
        order_id = _order_id(order_id)
        amount = format_amount(amount)
        if cart is None:
            cart = [
                {
                    'name': description or f'Order {order_id}',
                    'qty': 1,
                    'unit_price': amount,
                    'sub_total': amount,
                }
            ]

        body = {
            **self._pgw_credentials(),
            'order_id': order_id,
            'bill_amount': amount,
            'currency': CURRENCY_IQD,
            # FastPay refuses a cart sent as JSON: it must be a JSON *string*
            'cart': cart if isinstance(cart, str) else json.dumps(cart),
        }
        for key, url in (
            ('success_url', success_url),
            ('cancel_url', cancel_url),
            ('callback_url', callback_url),
        ):
            if url:
                body[key] = url

        return _answer(self._post(self._pgw_url('initiation'), body))

    def validate_payment(self, order_id):
        """The payment page's own check: the transaction once it is paid.

        Answer keys: ``gw_transaction_id``, ``received_amount``,
        ``customer_name``, ``customer_mobile_number``, ``status``
        (``Success``), ... Until the order is paid FastPay refuses with code
        ``404`` ("No transaction has been found"), raised as a
        ``FastPayAPIError``; :meth:`check_status` reads that as unpaid.
        """
        body = {**self._pgw_credentials(), 'order_id': _order_id(order_id)}
        return _answer(self._post(self._pgw_url('validate'), body))

    def refund(self, order_id, amount, *, msisdn, refund_secret_key):
        """Refund ``amount`` of a paid order to the customer's wallet.

        ``msisdn`` is the customer's FastPay number (``+964...``);
        ``refund_secret_key`` is in the store details of FastPay's merchant
        panel. Answers ``{"summary": {"recipient": {...}, "invoice_id"}}``.
        """
        body = {
            **self._pgw_credentials(),
            'order_id': _order_id(order_id),
            'amount': format_amount(amount),
            'refund_secret_key': refund_secret_key,
            'msisdn': msisdn,
        }
        return _answer(self._post(self._pgw_url('refund'), body))

    def refund_status(self, order_id):
        """Whether the order has been refunded: ``{"refund_status": bool, ...}``."""
        body = {**self._pgw_credentials(), 'order_id': _order_id(order_id)}
        return _answer(self._post(self._pgw_url('refund/validation'), body))

    # -- QR payments ------------------------------------------------------------
    def generate_qr(
        self,
        order_id,
        amount,
        *,
        success_url=None,
        cancel_url=None,
        callback_url=None,
        expires_in=None,
    ):
        """Start a QR payment and return FastPay's answer.

        ``expires_in`` is the minutes it stays payable, 3 to 15 (FastPay's
        default is 10). Answer keys: ``qrUrl`` (an image of the code) and
        ``qrText`` (the code itself, which :func:`app_link` hands to the
        FastPay app).
        """
        body = {
            **self._qr_credentials(),
            'orderId': _order_id(order_id),
            'billAmount': format_amount(amount),
            'currency': CURRENCY_IQD,
        }
        for key, url in (
            ('successUrl', success_url),
            ('cancelUrl', cancel_url),
            ('callbackUrl', callback_url),
        ):
            if url:
                body[key] = url
        if expires_in is not None:
            expires_in = int(expires_in)
            if not QR_MIN_LIFETIME <= expires_in <= QR_MAX_LIFETIME:
                raise ValueError(
                    f'A FastPay QR payment lasts {QR_MIN_LIFETIME} to '
                    f'{QR_MAX_LIFETIME} minutes, not {expires_in}.'
                )
            body['expiresIn'] = expires_in

        return _answer(self._post(self._qr_url('qr'), body))

    # -- status -------------------------------------------------------------------
    def payment_status(self, order_id):
        """The order's status, started on either host.

        Answer keys: ``payment_status`` (``PAID``, ``UNPAID`` or
        ``DECLINED``), and once paid ``gw_transaction_id``,
        ``received_amount``, ``customer_name``, ``customer_mobile_number``,
        ``received_at``, ... (``"N/A"`` before). An unknown order is refused.
        """
        body = {**self._qr_credentials(), 'orderId': _order_id(order_id)}
        return _answer(self._post(self._qr_url('status'), body))

    def check_status(self, order_id):
        """:meth:`payment_status`, falling back to :meth:`validate_payment`.

        FastPay documents ``vending/status`` with its QR API and only
        ``validate`` with its payment page, though both read either kind of
        order. Should the QR host ever refuse to report an order, the
        payment page's own check still completes it: its answer is returned
        with ``payment_status`` added (``PAID``, or ``UNPAID`` for its
        ``404``). A call that got no answer at all is raised, not retried.
        """
        try:
            return self.payment_status(order_id)
        except FastPayAPIError as exc:
            # no HTTP answer at all (a timeout, a dropped connection)
            if exc.status_code is None:
                raise

        try:
            data = self.validate_payment(order_id)
        except FastPayAPIError as exc:
            if exc.code == 404:
                return {'order_id': str(order_id), 'payment_status': STATUS_UNPAID}
            raise

        paid = transaction_state(data) == PAID
        return {**data, 'payment_status': STATUS_PAID if paid else STATUS_UNPAID}


def transaction_state(data):
    """``PAID``, ``FAILED`` or ``PENDING`` for a status, validate or IPN body."""
    data = data or {}
    payment_status = str(data.get('payment_status') or '').strip().upper()
    if payment_status:
        if payment_status == STATUS_PAID:
            return PAID
        if payment_status == STATUS_DECLINED:
            return FAILED
        return PENDING

    status = str(data.get('status') or '').strip().lower()
    if status == 'success':
        return PAID
    if status in FAILED_STATUSES:
        return FAILED
    return PENDING


def app_link(qr_text, order_id, client_uri):
    """The deep link that opens the FastPay app on a QR payment.

    ``client_uri`` is the URL scheme the calling app registered for FastPay to
    send the customer back to; FastPay names it ``appfpclient<AppName>`` and
    the prefix is added when missing. After paying, the FastPay app opens
    ``<client_uri>://fast-pay.cash?transactionStatus=...&orderId=...``.
    """
    if not client_uri:
        raise ValueError('A FastPay app link needs the client app URI scheme.')
    if not client_uri.lower().startswith(CLIENT_URI_PREFIX):
        client_uri = f'{CLIENT_URI_PREFIX}{client_uri}'

    # %20, not +, for the spaces in the code: iOS reads + literally
    query = urlencode(
        {'qrdata': qr_text, 'clientUri': client_uri, 'transactionId': order_id},
        quote_via=quote,
    )
    return f'{APP_LINK}?{query}'


def format_amount(amount):
    """Whole dinars, rounded half up: FastPay refuses anything but an integer."""
    return int(Decimal(str(amount)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_amount(value):
    """``received_amount`` as a Decimal (``"1000.00"``, ``1000.0``), or None."""
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _order_id(order_id):
    order_id = str(order_id)
    if not ORDER_ID_PATTERN.fullmatch(order_id):
        raise ValueError(
            f'A FastPay order id is 8 to 32 letters and digits, not {order_id!r}.'
        )
    return order_id


def _safe_json(resp):
    if not resp.content:
        return {}
    try:
        body = resp.json()
    except ValueError:
        return {'raw': resp.text}
    return body if isinstance(body, dict) else {'data': body}


def _code(payload):
    """FastPay's own ``code`` as an int, or None when the body has none."""
    try:
        return int(payload.get('code'))
    except (TypeError, ValueError):
        return None


def _answer(data):
    """An answer is a JSON object; anything else is no answer at all."""
    if not isinstance(data, dict):
        raise FastPayAPIError(
            f'FastPay answered no JSON object: {data!r}', code=200, payload=data
        )
    return data


def _reason(payload, resp):
    """The readable part of a refusal, whichever host gave it."""
    for key in ('messages', 'errors'):
        problems = payload.get(key)
        if isinstance(problems, str):
            problems = [problems]
        if isinstance(problems, list) and any(problems):
            return '; '.join(str(problem) for problem in problems if problem)
    for key in ('message', 'detail', 'title'):
        if payload.get(key):
            return str(payload[key])
    return resp.text or str(resp.status_code)
