"""Configuration bridge between Django settings and the FastPay client.

A project must set the store credentials (``FASTPAY_STORE_ID``,
``FASTPAY_STORE_PASSWORD``) and ``FASTPAY_RECEIPT_MODEL``, the model that
records its payments. Everything else has a default; ``FASTPAY_ENVIRONMENT``
is ``staging`` until a project says ``production``.

The receipt binding is what makes the app reusable: point
``FASTPAY_RECEIPT_MODEL`` / ``FASTPAY_RECEIPT_REF_FIELD`` /
``FASTPAY_RECEIPT_STATUS_FIELD`` at whatever model and fields a project uses.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .client import (
    ENVIRONMENTS,
    QR_DEFAULT_LIFETIME,
    QR_MAX_LIFETIME,
    QR_MIN_LIFETIME,
    FastPayClient,
)


@dataclass
class FastPayConf:
    store_id: str
    store_password: str
    environment: str = 'staging'
    # override either host of the environment
    base_url: str = ''
    qr_base_url: str = ''
    timeout: int = 30
    # where FastPay posts its notification (IPN) after a successful payment
    callback_url: str = ''
    # where the payment page sends the customer afterwards
    success_url: str = ''
    cancel_url: str = ''
    # the URL scheme a mobile app registered for the FastPay app to return to
    app_client_uri: str = ''
    # starts every order id: <prefix><receipt pk>T<unix time>
    order_prefix: str = ''
    # minutes a QR payment stays payable (3-15); the page's is FastPay's own
    qr_lifetime: int = QR_DEFAULT_LIFETIME
    # receipt binding
    receipt_model: str = ''
    ref_field: str = 'ref_no'
    status_field: str = 'is_completed'
    # what FastPay must have received before the receipt completes; blank skips
    amount_field: str = 'amount'
    # optional JSON field the verified transaction is kept in, under "fastpay"
    details_field: str = ''
    # optional dotted paths to callables(receipt, status)
    on_completed: str = ''
    on_failed: str = ''
    # optional file the package writes its log to, and from which level
    log_file: str = ''
    log_level: str = 'INFO'
    # who may call the payments/ endpoints (the callback is always public)
    view_permission_classes: tuple = ('rest_framework.permissions.IsAdminUser',)
    _client: FastPayClient = field(default=None, repr=False, compare=False)


_conf = None


def get_conf():
    """Build (once) and return the FastPay configuration from Django settings."""
    global _conf
    if _conf is None:
        environment = getattr(settings, 'FASTPAY_ENVIRONMENT', '') or 'staging'
        if environment not in ENVIRONMENTS:
            raise ImproperlyConfigured(
                f'FASTPAY_ENVIRONMENT is one of {sorted(ENVIRONMENTS)}, '
                f'not {environment!r}.'
            )

        order_prefix = str(getattr(settings, 'FASTPAY_ORDER_PREFIX', '') or '')
        if order_prefix and not order_prefix.isalnum():
            raise ImproperlyConfigured(
                'FASTPAY_ORDER_PREFIX must be letters and digits: FastPay takes '
                'alphanumeric order ids only.'
            )

        qr_lifetime = int(getattr(settings, 'FASTPAY_QR_LIFETIME', QR_DEFAULT_LIFETIME))
        if not QR_MIN_LIFETIME <= qr_lifetime <= QR_MAX_LIFETIME:
            raise ImproperlyConfigured(
                f'FASTPAY_QR_LIFETIME is {QR_MIN_LIFETIME} to {QR_MAX_LIFETIME} '
                'minutes: FastPay refuses anything else.'
            )

        _conf = FastPayConf(
            store_id=getattr(settings, 'FASTPAY_STORE_ID', ''),
            store_password=getattr(settings, 'FASTPAY_STORE_PASSWORD', ''),
            environment=environment,
            base_url=getattr(settings, 'FASTPAY_BASE_URL', ''),
            qr_base_url=getattr(settings, 'FASTPAY_QR_BASE_URL', ''),
            timeout=int(getattr(settings, 'FASTPAY_TIMEOUT', 30)),
            callback_url=getattr(settings, 'FASTPAY_CALLBACK_URL', ''),
            success_url=getattr(settings, 'FASTPAY_SUCCESS_URL', ''),
            cancel_url=getattr(settings, 'FASTPAY_CANCEL_URL', ''),
            app_client_uri=getattr(settings, 'FASTPAY_APP_CLIENT_URI', ''),
            order_prefix=order_prefix,
            qr_lifetime=qr_lifetime,
            receipt_model=getattr(settings, 'FASTPAY_RECEIPT_MODEL', ''),
            ref_field=getattr(settings, 'FASTPAY_RECEIPT_REF_FIELD', 'ref_no'),
            status_field=getattr(
                settings, 'FASTPAY_RECEIPT_STATUS_FIELD', 'is_completed'
            ),
            amount_field=getattr(settings, 'FASTPAY_RECEIPT_AMOUNT_FIELD', 'amount'),
            details_field=getattr(settings, 'FASTPAY_RECEIPT_DETAILS_FIELD', ''),
            on_completed=getattr(settings, 'FASTPAY_ON_PAYMENT_COMPLETED', ''),
            on_failed=getattr(settings, 'FASTPAY_ON_PAYMENT_FAILED', ''),
            log_file=str(getattr(settings, 'FASTPAY_LOG_FILE', '') or ''),
            log_level=getattr(settings, 'FASTPAY_LOG_LEVEL', 'INFO'),
            view_permission_classes=tuple(
                getattr(
                    settings,
                    'FASTPAY_VIEW_PERMISSION_CLASSES',
                    ('rest_framework.permissions.IsAdminUser',),
                )
            ),
        )
    return _conf


def get_client():
    """Return a process-wide FastPay client."""
    conf = get_conf()
    if conf._client is None:
        conf._client = FastPayClient(
            conf.store_id,
            conf.store_password,
            environment=conf.environment,
            base_url=conf.base_url or None,
            qr_base_url=conf.qr_base_url or None,
            timeout=conf.timeout,
        )
    return conf._client


def reset():
    """Drop cached config/client. Useful in tests and after settings override."""
    global _conf
    _conf = None
