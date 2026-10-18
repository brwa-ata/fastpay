"""HTTP surface for FastPay payments.

* ``POST fastpay/payments/``            -> start a FastPay payment for an existing receipt
* ``GET  fastpay/payments/<pk>/status`` -> re-sync + return the payment status
* ``POST fastpay/callback/``            -> FastPay's notification (public, re-verified)

The payment endpoints operate on a receipt that the project has already
created (pending, ``is_completed=False``). Creating that receipt stays in the
project so this app never needs to know how receipts are built. They look a
receipt up by id alone, so they are for staff:
``FASTPAY_VIEW_PERMISSION_CLASSES`` decides who may call them (``IsAdminUser``
unless the project says otherwise).
"""

from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.module_loading import import_string
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import service
from .client import transaction_state
from .conf import get_conf
from .exceptions import FastPayError
from .logs import logger

MODE_PAGE = 'page'
MODE_QR = 'qr'


def _callback_url(request):
    conf = get_conf()
    if conf.callback_url:
        return conf.callback_url
    return request.build_absolute_uri(reverse('fastpay-callback'))


class ProjectPermissionsMixin:
    """Take the permission classes from ``FASTPAY_VIEW_PERMISSION_CLASSES``."""

    def get_permissions(self):
        return [import_string(path)() for path in get_conf().view_permission_classes]


class FastPayStartPaymentView(ProjectPermissionsMixin, APIView):
    """Start a FastPay payment for an existing, pending receipt.

    ``mode`` is ``page`` (FastPay's payment page, the default) or ``qr`` (a QR
    code, plus a FastPay app link when a client URI is known).
    """

    def post(self, request):
        receipt_id = request.data.get('receipt_id')
        if not receipt_id:
            return Response({'message': 'receipt_id is required'}, status=422)

        mode = request.data.get('mode') or MODE_PAGE
        if mode not in (MODE_PAGE, MODE_QR):
            return Response({'message': 'mode is page or qr'}, status=422)

        receipt = get_object_or_404(service.get_receipt_model(), pk=receipt_id)
        urls = {
            'success_url': request.data.get('success_url'),
            'cancel_url': request.data.get('cancel_url'),
            'callback_url': _callback_url(request),
        }
        try:
            if mode == MODE_QR:
                payment = service.start_qr_payment(
                    receipt, client_uri=request.data.get('client_uri'), **urls
                )
            else:
                payment = service.start_payment(
                    receipt, description=request.data.get('description'), **urls
                )
        except FastPayError as exc:
            logger.error('FastPay start payment failed: %s', exc)
            return Response({'message': str(exc)}, status=502)

        if mode == MODE_QR:
            fields = ('orderId', 'qrUrl', 'qrText', 'appLink', 'expiresAt')
        else:
            fields = ('orderId', 'redirect_uri', 'expiresAt')

        return Response(
            {key: payment.get(key) for key in fields},
            status=status.HTTP_201_CREATED,
        )


class FastPayPaymentStatusView(ProjectPermissionsMixin, APIView):
    """Re-sync a receipt's payment against FastPay and return the status."""

    def get(self, request, pk):
        conf = get_conf()
        receipt = get_object_or_404(service.get_receipt_model(), pk=pk)
        try:
            payload = service.sync_status(receipt)
        except FastPayError as exc:
            return Response({'message': str(exc)}, status=502)

        return Response(
            {
                'state': transaction_state(payload),
                'is_completed': getattr(receipt, conf.status_field),
                'transaction': payload,
            }
        )


class FastPayCallbackView(APIView):
    """Public endpoint FastPay posts a successful payment to (its IPN).

    FastPay sends it to the payment's ``callback_url``, or to the IPN URL set
    in its merchant panel.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        data = request.data
        order_id = data.get('order_id') or data.get('merchant_order_id')
        if not order_id:
            logger.warning('FastPay notification without an order_id: %s', dict(data))
            return Response({'message': 'missing order_id'}, status=400)

        try:
            service.handle_callback(str(order_id), claimed=data)
        except FastPayError as exc:
            # tell FastPay to retry later rather than swallow a transient failure
            logger.error('FastPay notification processing failed: %s', exc)
            return Response({'message': str(exc)}, status=503)

        return Response({'message': 'ok'})
