"""The smallest Django project that can host the app, for the test suite."""

SECRET_KEY = 'fastpay-tests'
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'fastpay',
    'tests.testapp',
]

DATABASES = {
    'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'},
}

ROOT_URLCONF = 'tests.urls'

FASTPAY_STORE_ID = '749347_291'
FASTPAY_STORE_PASSWORD = 'store-password'
FASTPAY_RECEIPT_MODEL = 'testapp.Receipt'
FASTPAY_ON_PAYMENT_COMPLETED = 'tests.hooks.record'
FASTPAY_ON_PAYMENT_FAILED = 'tests.hooks.record_failed'
