from unittest.mock import MagicMock, patch

import pytest

from fastpay import conf

from . import hooks

# vending/status, as FastPay's staging answered it for an order nobody has
# paid yet (one started on the payment page; the QR host reads both)
UNPAID = {
    'gw_transaction_id': 'N/A',
    'merchant_order_id': '7T1792323005',
    'received_amount': '1000.0000',
    'currency': 'IQD',
    'customer_name': 'N/A',
    'customer_mobile_number': 'N/A',
    'at': 'N/A',
    'transaction_id': 'N/A',
    'order_id': '7T1792323005',
    'customer_account_no': 'N/A',
    'received_at': 'N/A',
    'payment_status': 'UNPAID',
}
# once the customer has paid on the page
PAID = {
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
    'received_at': '2026-10-18 14:30:25',
    'payment_status': 'PAID',
}
# and once they pressed Cancel on it
DECLINED = {**UNPAID, 'payment_status': 'DECLINED'}

PAGE = 'https://staging-pgw.fast-pay.iq/pay?token=281891ba-181d-4c47-89cb-64793a46cc7c'
QR_TEXT = (
    '00020101021251350010iq.fastpay0117936846675396292925204581453033685404'
    '10005802IQ5914FastPay Test 36005Erbil61054400162300114LAVQ17923229030808'
    'PURCHASE630404AA'
)
QR_URL = 'https://fastpay-qr-staging.s3-eu-west-1.amazonaws.com/749347_291/X.png'


@pytest.fixture(autouse=True)
def fresh_state():
    """Settings are cached per process; every test starts from its own."""
    conf.reset()
    hooks.calls.clear()
    hooks.failed_calls.clear()
    yield
    conf.reset()


@pytest.fixture
def fastpay():
    """FastPay as the service sees it."""
    client = MagicMock()
    client.initiate_payment.return_value = {'redirect_uri': PAGE}
    client.generate_qr.return_value = {'qrUrl': QR_URL, 'qrText': QR_TEXT}
    client.check_status.return_value = dict(UNPAID)
    with patch('fastpay.service.get_client', return_value=client):
        yield client
