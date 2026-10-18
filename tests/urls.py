from django.urls import include, path

urlpatterns = [
    path('api/fastpay/', include('fastpay.urls')),
]
