"""Logging: to the `fastpay` logger, and to a file only when asked."""

import logging

import pytest

from fastpay import conf, logs, service

from .conftest import PAID
from .testapp.models import Receipt


@pytest.fixture(autouse=True)
def clean_logger():
    """Leave the logger as the next test expects it."""
    before = list(logs.logger.handlers), logs.logger.propagate, logs.logger.level
    yield
    for handler in logs.logger.handlers:
        if handler not in before[0]:
            handler.close()
    logs.logger.handlers[:] = before[0]
    logs.logger.propagate, logs.logger.level = before[1], before[2]


def set_log_file(settings, value):
    settings.FASTPAY_LOG_FILE = value
    conf.reset()


def test_without_a_file_nothing_is_attached(settings):
    set_log_file(settings, '')

    assert logs.configure() is None
    assert logs.logger.name == 'fastpay'


@pytest.mark.django_db
def test_the_file_gets_the_packages_log(settings, tmp_path, fastpay):
    set_log_file(settings, tmp_path / 'logs' / 'fastpay.log')

    logs.configure()
    receipt = Receipt.objects.create(amount=1)
    payment = service.start_payment(receipt, success_url='https://x/back/')

    text = (tmp_path / 'logs' / 'fastpay.log').read_text()
    assert (
        f'INFO fastpay: FastPay payment {payment["orderId"]} started for '
        f'receipt {receipt.pk}'
    ) in text


def test_a_relative_file_is_under_base_dir(settings, tmp_path):
    settings.BASE_DIR = tmp_path
    set_log_file(settings, 'fastpay.log')

    handler = logs.configure()

    assert handler.baseFilename == str((tmp_path / 'fastpay.log').resolve())
    assert logs.logger.propagate is False


def test_configuring_twice_adds_one_handler(settings, tmp_path):
    set_log_file(settings, tmp_path / 'fastpay.log')

    first = logs.configure()
    second = logs.configure()

    assert first is second
    assert logs.logger.handlers.count(first) == 1


def test_the_level_is_a_setting(settings, tmp_path):
    settings.FASTPAY_LOG_LEVEL = 'WARNING'
    set_log_file(settings, tmp_path / 'fastpay.log')

    logs.configure()

    assert logs.logger.level == logging.WARNING


@pytest.mark.django_db
def test_a_completion_is_logged_with_fastpays_transaction(settings, tmp_path, fastpay):
    set_log_file(settings, tmp_path / 'fastpay.log')
    logs.configure()
    receipt = Receipt.objects.create(amount=1000, ref_no='7T1792323005')

    service.apply_status(receipt, dict(PAID))

    text = (tmp_path / 'fastpay.log').read_text()
    assert (
        f'FastPay payment 7T1792323005 for receipt {receipt.pk} marked completed '
        '(transaction A1KSO55655)'
    ) in text


@pytest.mark.django_db
def test_a_short_payment_is_logged_as_an_error(settings, tmp_path, fastpay):
    set_log_file(settings, tmp_path / 'fastpay.log')
    logs.configure()
    receipt = Receipt.objects.create(amount=25000, ref_no='7T1792323005')

    service.apply_status(receipt, dict(PAID))

    text = (tmp_path / 'fastpay.log').read_text()
    assert 'ERROR fastpay: FastPay payment 7T1792323005' in text
    assert 'less than the receipt amount 25000' in text
