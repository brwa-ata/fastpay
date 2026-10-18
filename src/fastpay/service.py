"""Bridge between the FastPay client and a project's receipt model.

The only assumptions about the receipt model are that it has:

* a *reference* field to store our order id (default ``ref_no``)
* a boolean *status* field that gates whether the receipt counts
  (default ``is_completed``) -- ``False`` until FastPay confirms the payment
* an *amount* field (default ``amount``): a receipt only completes once
  FastPay reports receiving at least that much, as FastPay asks merchants to
  check. Set ``FASTPAY_RECEIPT_AMOUNT_FIELD = ''`` to skip the check.

Optionally, a JSON field keeps the verified transaction
(``FASTPAY_RECEIPT_DETAILS_FIELD``): its ``gw_transaction_id`` and the payer's
wallet number are what FastPay's support and a refund ask for.

The model and the field names are all configurable (see :mod:`fastpay.conf`),
so this file never imports the project's model.

Nothing FastPay *sends* us is trusted: the notification (IPN) only names an
order, and the status is always read back from FastPay before a receipt moves.
"""

import time
from datetime import timedelta

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from .client import (
    FAILED,
    PAGE_LIFETIME,
    PAID,
    PENDING,
    app_link,
    format_amount,
    parse_amount,
    transaction_state,
)
from .conf import get_client, get_conf
from .exceptions import FastPayAPIError
from .logs import logger


def get_receipt_model():
    model = get_conf().receipt_model
    if not model:
        raise ImproperlyConfigured(
            'Set FASTPAY_RECEIPT_MODEL to the model that records payments, '
            "e.g. 'shop.Receipt'."
        )
    return apps.get_model(model)


def new_order_id(receipt):
    """``<prefix><receipt pk>T<unix time>``: letters and digits, as FastPay wants.

    The pk makes it unique here; the time keeps it unique across the
    databases (local, staging, live) that share one store, and lets a receipt
    be paid again after a cancelled attempt.
    """
    return f'{get_conf().order_prefix}{receipt.pk}T{int(time.time())}'


def start_payment(
    receipt,
    *,
    success_url=None,
    cancel_url=None,
    callback_url=None,
    amount=None,
    description=None,
    cart=None,
):
    """Start a payment on FastPay's page for ``receipt`` and store its order id.

    The page lets the customer scan a QR code with the FastPay app, or log in
    with their wallet number, password and an OTP, so it works on a computer
    and a phone alike. Every URL defaults to its setting; ``cancel_url`` then
    to ``success_url``.

    Marks the receipt pending (status field -> ``False``) and returns
    FastPay's answer -- ``redirect_uri``, the page to send the customer to --
    with two keys of ours added: ``orderId``, and ``expiresAt``, the ISO time
    after which the page no longer takes the payment.
    """
    conf = get_conf()
    order_id = new_order_id(receipt)
    success_url = success_url or conf.success_url

    response = get_client().initiate_payment(
        order_id,
        amount if amount is not None else _receipt_amount(receipt),
        success_url=success_url,
        cancel_url=cancel_url or conf.cancel_url or success_url,
        callback_url=callback_url or conf.callback_url,
        cart=cart,
        description=description or _default_description(receipt),
    )

    if not response.get('redirect_uri'):
        # a FastPayError, so the caller's own `except FastPayError` refuses it
        raise FastPayAPIError(
            f'FastPay initiation returned no redirect_uri: {response}',
            code=200,
            payload=response,
        )

    _mark_pending(receipt, order_id)
    logger.info('FastPay payment %s started for receipt %s', order_id, receipt.pk)

    return {
        **response,
        'orderId': order_id,
        'expiresAt': _expires_at(PAGE_LIFETIME),
    }


def start_qr_payment(
    receipt,
    *,
    client_uri=None,
    success_url=None,
    cancel_url=None,
    callback_url=None,
    amount=None,
    expires_in=None,
):
    """Start a QR payment for ``receipt`` and store its order id.

    For a mobile app: the answer's ``appLink`` opens the FastPay app on the
    payment, and the FastPay app sends the customer back to ``client_uri``
    (default ``FASTPAY_APP_CLIENT_URI``) afterwards; with no client URI there
    is no ``appLink``. ``qrUrl`` is an image of the code, for a screen the
    customer scans with another phone.

    Marks the receipt pending and returns FastPay's answer (``qrUrl``,
    ``qrText``) with ``orderId``, ``expiresAt`` and ``appLink`` added.
    """
    conf = get_conf()
    order_id = new_order_id(receipt)
    expires_in = expires_in or conf.qr_lifetime
    success_url = success_url or conf.success_url

    response = get_client().generate_qr(
        order_id,
        amount if amount is not None else _receipt_amount(receipt),
        success_url=success_url,
        cancel_url=cancel_url or conf.cancel_url or success_url,
        callback_url=callback_url or conf.callback_url,
        expires_in=expires_in,
    )

    if not response.get('qrText'):
        raise FastPayAPIError(
            f'FastPay QR generation returned no qrText: {response}',
            code=200,
            payload=response,
        )

    _mark_pending(receipt, order_id)
    logger.info('FastPay QR payment %s started for receipt %s', order_id, receipt.pk)

    client_uri = client_uri or conf.app_client_uri
    return {
        **response,
        'orderId': order_id,
        'expiresAt': _expires_at(expires_in),
        'appLink': app_link(response['qrText'], order_id, client_uri)
        if client_uri
        else None,
    }


def sync_status(receipt):
    """Re-read the payment from FastPay and apply it to ``receipt``.

    Returns FastPay's status answer (``payment_status``, ``received_amount``,
    ``gw_transaction_id``, ...); ``client.transaction_state()`` reads it.
    """
    order_id = getattr(receipt, get_conf().ref_field)
    if not order_id:
        raise ValueError('Receipt has no FastPay order id to sync.')
    status = get_client().check_status(order_id)
    apply_status(receipt, status)
    return status


@transaction.atomic
def apply_status(receipt, status):
    """Apply a FastPay status (a ``check_status`` answer) to ``receipt``.

    * paid sets the status field to ``True``, keeps the transaction in the
      details field, and runs the completion hook -- once, on the pending ->
      completed transition. The row is locked first, since the notification
      and a status poll race each other. A payment that falls short of the
      receipt's amount is kept but left pending, and logged as an error;
    * failed (cancelled on FastPay's page) keeps the transaction (on a receipt
      not completed) and runs the failed hook. That hook runs every time the
      status is applied (a poll can see it more than once), so write it to be
      idempotent;
    * pending changes nothing.

    Returns the state: ``PAID``, ``FAILED`` or ``PENDING`` -- ``PENDING`` for
    a payment that falls short, too, since the receipt did not complete.
    """
    conf = get_conf()
    state = transaction_state(status)
    order_id = getattr(receipt, conf.ref_field)

    if state == PAID:
        locked = (
            type(receipt).objects.select_for_update().filter(pk=receipt.pk).first()
            or receipt
        )
        if not getattr(locked, conf.status_field):
            if not _covers(locked, status):
                fields = _keep_details(locked, status)
                if fields:
                    locked.save(update_fields=fields)
                logger.error(
                    'FastPay payment %s for receipt %s received %s, less than '
                    'the receipt amount %s: left pending',
                    order_id,
                    receipt.pk,
                    status.get('received_amount'),
                    getattr(locked, conf.amount_field),
                )
                state = PENDING
            else:
                setattr(locked, conf.status_field, True)
                fields = [conf.status_field, *_keep_details(locked, status)]
                locked.save(update_fields=fields)
                _run_hook(conf.on_completed, locked, status)
                logger.info(
                    'FastPay payment %s for receipt %s marked completed '
                    '(transaction %s)',
                    order_id,
                    receipt.pk,
                    status.get('gw_transaction_id'),
                )
        if locked is not receipt:
            receipt.refresh_from_db(fields=[conf.status_field, *_details_field(conf)])
    elif state == FAILED:
        if not getattr(receipt, conf.status_field):
            fields = _keep_details(receipt, status)
            if fields:
                receipt.save(update_fields=fields)
        _run_hook(conf.on_failed, receipt, status)
        logger.info(
            'FastPay payment %s for receipt %s failed: %s',
            order_id,
            receipt.pk,
            status.get('payment_status') or status.get('status'),
        )
    return state


def handle_callback(order_id, claimed=None):
    """Handle FastPay's notification (IPN) for ``order_id``.

    The notification is not trusted for the outcome -- the status is always
    re-read from FastPay before the receipt is updated. ``claimed`` is the
    body, for the log. Returns the receipt, or ``None`` when no receipt
    carries that order.
    """
    conf = get_conf()
    receipt = get_receipt_model().objects.filter(**{conf.ref_field: order_id}).first()
    if receipt is None:
        logger.warning('FastPay notification for unknown order %s', order_id)
        return None

    status = get_client().check_status(order_id)
    claimed = claimed or {}
    logger.info(
        'FastPay notification order=%s claimed=%s/%s verified=%s/%s %s',
        order_id,
        claimed.get('status'),
        claimed.get('received_amount'),
        status.get('payment_status'),
        status.get('received_amount'),
        transaction_state(status),
    )
    apply_status(receipt, status)
    return receipt


def _mark_pending(receipt, order_id):
    conf = get_conf()
    setattr(receipt, conf.ref_field, order_id)
    setattr(receipt, conf.status_field, False)
    receipt.save(update_fields=[conf.ref_field, conf.status_field])


def _receipt_amount(receipt):
    field_name = get_conf().amount_field or 'amount'
    return getattr(receipt, field_name)


def _covers(receipt, status):
    """Whether FastPay received at least the receipt's amount (in whole dinars,
    as it was charged). Always true with no amount field set."""
    field_name = get_conf().amount_field
    if not field_name:
        return True

    received = parse_amount(status.get('received_amount'))
    expected = getattr(receipt, field_name, None)
    if received is None or expected is None:
        return False
    return received >= format_amount(expected)


def _expires_at(minutes):
    return (timezone.now() + timedelta(minutes=minutes)).isoformat()


def _default_description(receipt):
    invoice = getattr(receipt, 'invoice_no', None)
    return f'Payment {invoice}' if invoice else f'Payment #{receipt.pk}'


def _details_field(conf):
    return [conf.details_field] if conf.details_field else []


def _keep_details(receipt, status):
    """Put the transaction under ``"fastpay"`` in the details field, if one is
    set, leaving the field's other keys alone. Returns the fields changed."""
    field_name = get_conf().details_field
    if not field_name:
        return []

    current = getattr(receipt, field_name, None) or {}
    if not isinstance(current, dict):
        logger.warning(
            'Receipt %s: %s holds no JSON object; the FastPay transaction is not kept',
            receipt.pk,
            field_name,
        )
        return []

    setattr(receipt, field_name, {**current, 'fastpay': dict(status)})
    return [field_name]


def _run_hook(dotted_path, receipt, status):
    if not dotted_path:
        return
    try:
        import_string(dotted_path)(receipt, status)
    except Exception:  # noqa: BLE001 - a hook must never break payment flow
        logger.exception('FastPay payment hook %s failed', dotted_path)
