from django.urls import path

from .views import (
    FastPayCallbackView,
    FastPayPaymentStatusView,
    FastPayStartPaymentView,
)

urlpatterns = [
    path('payments/', FastPayStartPaymentView.as_view(), name='fastpay-start-payment'),
    path(
        'payments/<int:pk>/status/',
        FastPayPaymentStatusView.as_view(),
        name='fastpay-payment-status',
    ),
    path('callback/', FastPayCallbackView.as_view(), name='fastpay-callback'),
]
