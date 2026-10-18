"""Hooks the tests point FASTPAY_ON_PAYMENT_COMPLETED / _FAILED at."""

calls = []
failed_calls = []


def record(receipt, status):
    calls.append((receipt.pk, status.get('payment_status')))


def record_failed(receipt, status):
    failed_calls.append((receipt.pk, status.get('payment_status')))


def explode(receipt, status):
    raise RuntimeError('a bug in the project hook')
