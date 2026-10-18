# fastpay

FastPay payment gateway for Django. A project creates a pending receipt, this
package starts the FastPay payment for it — on FastPay's own payment page, or
as a QR code that a mobile app hands to the FastPay app — and the receipt is
completed only once FastPay itself confirms the payment.

- FastPay API documentation: <https://developer.fast-pay.iq> and the
  [Postman collection](https://documenter.getpostman.com/view/19338260/2s9YkkeNKN).
  Where the gateway answers differently from the documentation, the package
  follows the gateway; see [FastPay API contract](#fastpay-api-contract).
- Import name: `fastpay`. Distribution name: `fastpay`.
- Requires Python 3.10+, Django 4.2+, Django REST framework 3.14+, requests.

`client.py` has no Django dependency. If all you need is a raw FastPay API
wrapper, that one file (plus `exceptions.py`) is enough.

## Contents

- [How a payment works](#how-a-payment-works)
- [Which flow to use](#which-flow-to-use)
- [What is in the package](#what-is-in-the-package)
- [Install into a project](#install-into-a-project)
- [Settings reference](#settings-reference)
- [HTTP endpoints](#http-endpoints)
- [Reading a status](#reading-a-status)
- [Tracking the status](#tracking-the-status)
- [Mobile apps (Flutter, Android, iOS)](#mobile-apps-flutter-android-ios)
- [Python API](#python-api)
- [FastPay API contract](#fastpay-api-contract)
- [Logging](#logging)
- [Testing](#testing)
- [Updating and releasing](#updating-and-releasing)
- [Known limits](#known-limits)

## How a payment works

```
1. The project creates a pending receipt              is_completed = False
2. service.start_payment(receipt, success_url=…)        (or start_qr_payment)
   -> FastPay creates the payment, with an order id of ours:
      <FASTPAY_ORDER_PREFIX><receipt pk>T<unix time>
   -> the order id is stored in receipt.ref_no
   -> returns redirect_uri (FastPay's page) and expiresAt
3. The customer pays on FastPay's page: scans its QR code with the FastPay
   app, or logs in with their wallet number, password and an OTP
4. FastPay's page sends the customer to success_url (or cancel_url) with
   ?order_id=… added, and posts its notification (IPN) to the callback
   -> the status is re-read from FastPay, never trusted from the body
   -> on paid, and only if FastPay received at least the receipt's amount:
      is_completed = True, the transaction is kept (optional), and the
      completion hook runs once
5. Meanwhile the client polls a status endpoint, which re-reads the status
   from FastPay too, so the payment completes even if the notification never
   arrives
```

## Which flow to use

| Flow | Call | The customer | Lives |
| --- | --- | --- | --- |
| Payment page | `service.start_payment()` | Opens `redirect_uri`: scans the page's QR code, or logs in with their wallet and an OTP. Works on a computer and on a phone, FastPay app or not | 10 minutes (FastPay's) |
| QR / app | `service.start_qr_payment()` | A mobile app opens `appLink`, which opens the FastPay app on the payment and returns to the calling app afterwards. A screen can show `qrUrl` to scan instead | 3–15 minutes (`FASTPAY_QR_LIFETIME`, default 10) |

Use the page for a website. Use QR / app for a mobile app whose customers
have the FastPay app, and fall back to the page when it is not installed.

Both flows share one order space and one status call, so everything after the
start — the status, the notification, completing the receipt — is the same.

## What is in the package

| File | What it does |
| --- | --- |
| `client.py` | `FastPayClient`: page, QR, status, validate, refund; `transaction_state()`, `app_link()`. No Django |
| `conf.py` | Reads the `FASTPAY_*` settings once and builds one shared client |
| `service.py` | Ties the client to your receipt model: start, sync, complete, notification |
| `views.py` / `urls.py` | The notification FastPay calls, plus two staff endpoints (see [HTTP endpoints](#http-endpoints)) |
| `logs.py` | The `fastpay` logger, and the optional log file |
| `exceptions.py` | `FastPayError`, `FastPayAuthError`, `FastPayAPIError` |

The package has no models and no migrations.

## Install into a project

### 1. Install the package

Projects pin an exact tag, so nothing changes until someone moves the pin.

```bash
# uv
uv add git+https://github.com/brwa-ata/fastpay --tag v0.1.0

# pip (requirements.txt)
fastpay @ git+https://github.com/brwa-ata/fastpay@v0.1.0
```

The server that installs it needs `git` and outbound HTTPS to github.com.

### 2. Register it and route it

```python
# settings.py
INSTALLED_APPS += ['fastpay']

# urls.py
path('api/fastpay/', include('fastpay.urls')),
```

The notification must be reachable by FastPay without a login; if the project
has middleware that refuses anonymous requests, let `api/fastpay/callback/`
through.

### 3. Add the credentials

```bash
# .env
FASTPAY_ENVIRONMENT=staging          # or production
FASTPAY_STORE_ID=...
FASTPAY_STORE_PASSWORD=...
# Optional. Blank = built from the request as <host>/api/fastpay/callback/
FASTPAY_CALLBACK_URL=
# Optional. Where the page sends the customer, when the caller names none
FASTPAY_SUCCESS_URL=
FASTPAY_CANCEL_URL=
```

```python
# settings.py (python-decouple shown; os.environ works the same)
FASTPAY_ENVIRONMENT = config('FASTPAY_ENVIRONMENT', default='staging')
FASTPAY_STORE_ID = config('FASTPAY_STORE_ID')
FASTPAY_STORE_PASSWORD = config('FASTPAY_STORE_PASSWORD')
FASTPAY_CALLBACK_URL = config('FASTPAY_CALLBACK_URL', default='')
FASTPAY_SUCCESS_URL = config('FASTPAY_SUCCESS_URL', default='')
FASTPAY_CANCEL_URL = config('FASTPAY_CANCEL_URL', default='')
```

| `FASTPAY_ENVIRONMENT` | Payment page API | QR API | Merchant panel |
| --- | --- | --- | --- |
| `staging` | `https://staging-apigw-merchant.fast-pay.iq` | `https://staging-qr.fast-pay.iq` | `https://staging-merchant.fast-pay.iq` |
| `production` | `https://apigw-merchant.fast-pay.iq` | `https://qr.fast-pay.iq` | `https://merchant.fast-pay.iq` |

Each environment has its own store credentials; live ones come from FastPay's
support team. Staging takes FastPay's test wallets, with the OTP `123456`.

**Set `FASTPAY_CALLBACK_URL` in production.** Built from the request, the URL
is only right if Django knows it is behind HTTPS (`SECURE_PROXY_SSL_HEADER`)
and sees the public host. FastPay also posts to the IPN URL set in its
merchant panel; point that at the same address.

The success and cancel URLs are opened by the customer's own browser (FastPay's
page redirects with script), so a `localhost` page works while developing.
Only the notification needs a public address, and polling completes a payment
without it.

### 4. Point the app at your receipt model

The package never imports your model. It needs a model with a field for our
order id, a boolean that stays `False` until FastPay confirms the payment, and
the amount to check FastPay's against:

```python
FASTPAY_RECEIPT_MODEL = 'shop.Receipt'         # required
FASTPAY_RECEIPT_REF_FIELD = 'ref_no'           # default
FASTPAY_RECEIPT_STATUS_FIELD = 'is_completed'  # default
FASTPAY_RECEIPT_AMOUNT_FIELD = 'amount'        # default; '' skips the check
# Optional: a JSONField the verified transaction is kept in, under "fastpay"
FASTPAY_RECEIPT_DETAILS_FIELD = 'meta'

# Optional: run once, on the pending -> completed transition only
FASTPAY_ON_PAYMENT_COMPLETED = 'shop.payments.on_fastpay_paid'
# Optional: run whenever FastPay reports a cancelled (declined) payment
FASTPAY_ON_PAYMENT_FAILED = 'shop.payments.on_fastpay_failed'
```

A hook is `fn(receipt, status)`, where `status` is FastPay's verified status
answer (`gw_transaction_id`, `received_amount`, `customer_mobile_number`,
...). An exception inside a hook is logged and swallowed: it never undoes a
payment FastPay has already confirmed. Only the completion hook is guaranteed
to run once; the failed hook runs each time a declined status is read, so make
it idempotent.

**The amount check.** FastPay asks merchants to compare what it received with
what they expected. A paid status completes the receipt only when its
`received_amount` is at least the receipt's amount field (in whole dinars, as
it was charged). A payment that falls short is kept in the details field,
logged as an error, and left pending for someone to look at. Charging more
than the receipt (`amount=` with a fee on top) passes.

### 5. Choose where the log goes (optional)

```python
FASTPAY_LOG_FILE = BASE_DIR / 'logs' / 'fastpay.log'   # or 'fastpay.log'
```

See [Logging](#logging); without it the package writes no file.

### 6. Write the "start payment" and "status" endpoints

Creating the receipt stays in the project, because only the project knows who
pays, how much, and what the receipt means. A customer-facing pair looks like
this:

```python
from django.db import transaction
from fastpay import service as fastpay_service
from fastpay.exceptions import FastPayError

@transaction.atomic
def start(request):
    receipt = Receipt.objects.create(
        customer=request.user, amount=amount, is_completed=False, ...
    )
    try:
        if request.data.get('mode') == 'app':
            payment = fastpay_service.start_qr_payment(
                receipt, client_uri='appfpclientShop',
            )
        else:
            payment = fastpay_service.start_payment(
                receipt, success_url='https://shop.example/payment/return',
            )
    except FastPayError as exc:
        # a returned response does not roll back the atomic block by itself
        transaction.set_rollback(True)
        return Response({'message': f'FastPay payment failed: {exc}'}, 422)

    return Response({
        'receipt_id': receipt.id,
        'ref_no': receipt.ref_no,                    # our order id
        'redirect_url': payment.get('redirect_uri'), # the page, or
        'app_link': payment.get('appLink'),          # the FastPay app
        'qr_code': payment.get('qrUrl'),
        'expires_at': payment['expiresAt'],
    }, 201)

def status(request):
    # scope it to the caller, so nobody can read another customer's payment
    receipt = get_object_or_404(Receipt, pk=..., customer=request.user)
    if not receipt.is_completed:
        try:
            fastpay_service.sync_status(receipt)
        except FastPayError:
            pass  # transient: keep it pending, the client polls again
    return Response({'is_completed': receipt.is_completed})
```

Pass `amount=` to either start call to charge something other than the
receipt's amount, e.g. the amount plus a fee the customer pays on top.

Open `redirect_uri` from a link the customer clicks (or a plain redirect), not
from script after the request: browsers block a new tab opened that late.

## Settings reference

| Setting | Default | Purpose |
| --- | --- | --- |
| `FASTPAY_STORE_ID` / `FASTPAY_STORE_PASSWORD` | *(none, required)* | Store credentials |
| `FASTPAY_ENVIRONMENT` | `staging` | `staging` or `production`; picks both API hosts. Anything else is refused at startup |
| `FASTPAY_BASE_URL` / `FASTPAY_QR_BASE_URL` | *(blank)* | Override the payment page / QR API host of the environment |
| `FASTPAY_TIMEOUT` | `30` | HTTP timeout, seconds |
| `FASTPAY_CALLBACK_URL` | *(blank)* | Where FastPay posts its notification (`callback_url`); blank builds `<host>/api/fastpay/callback/` from the request in the staff endpoint, and sends none from the service |
| `FASTPAY_SUCCESS_URL` / `FASTPAY_CANCEL_URL` | *(blank)* | Where the page sends the customer, when the caller names none. The cancel URL falls back to the success URL |
| `FASTPAY_APP_CLIENT_URI` | *(blank)* | The URL scheme a mobile app registered for the FastPay app to return to (`appfpclient<AppName>`); without one, a QR payment has no `appLink` unless the caller names it |
| `FASTPAY_ORDER_PREFIX` | *(blank)* | Letters and digits that start every order id; anything else is refused at startup |
| `FASTPAY_QR_LIFETIME` | `10` | Minutes a QR payment stays payable, 3 to 15 |
| `FASTPAY_RECEIPT_MODEL` | *(none, required)* | `app_label.Model` holding payments |
| `FASTPAY_RECEIPT_REF_FIELD` | `ref_no` | Field that stores our order id |
| `FASTPAY_RECEIPT_STATUS_FIELD` | `is_completed` | Boolean completed by a paid status |
| `FASTPAY_RECEIPT_AMOUNT_FIELD` | `amount` | What FastPay must have received before the receipt completes. Blank: no check |
| `FASTPAY_RECEIPT_DETAILS_FIELD` | *(blank)* | JSONField that keeps the verified transaction under `"fastpay"`. Blank: not kept |
| `FASTPAY_ON_PAYMENT_COMPLETED` | *(blank)* | Dotted path to `fn(receipt, status)`, run once on completion |
| `FASTPAY_ON_PAYMENT_FAILED` | *(blank)* | Dotted path to `fn(receipt, status)`, run on a declined status |
| `FASTPAY_LOG_FILE` | *(blank)* | File the package writes its log to; relative paths start at `BASE_DIR`. Blank: no file |
| `FASTPAY_LOG_LEVEL` | `INFO` | Lowest level written to `FASTPAY_LOG_FILE` |
| `FASTPAY_VIEW_PERMISSION_CLASSES` | `['rest_framework.permissions.IsAdminUser']` | Dotted paths of the DRF permissions guarding the two `payments/` endpoints |

Settings are read once per process (`conf.get_conf()`), so restart the app
server and every worker after changing them. In tests, call
`fastpay.conf.reset()` after overriding one.

## HTTP endpoints

Mounted under whatever prefix the project gives `fastpay.urls`
(`api/fastpay/` below).

| Endpoint | Who | What |
| --- | --- | --- |
| `POST api/fastpay/callback/` | FastPay (public) | FastPay's notification, JSON or form; only its `order_id` (or `merchant_order_id`) is used. Re-reads the status from FastPay and applies it. `200` done (an unknown order too), `400` no order, `503` FastPay could not be reached |
| `POST api/fastpay/payments/` | Staff | `{"receipt_id", "mode", "success_url", "cancel_url", "client_uri", "description"}`: starts a payment for an existing pending receipt. `mode` is `page` (default; answers `orderId`, `redirect_uri`, `expiresAt`) or `qr` (answers `orderId`, `qrUrl`, `qrText`, `appLink`, `expiresAt`). `422` without a receipt or with another mode, `502` if FastPay refuses |
| `GET api/fastpay/payments/<receipt pk>/status/` | Staff | Re-syncs and answers `{state, is_completed, transaction}`; `502` if FastPay refuses |

The two `payments/` endpoints find a receipt by its id alone, so they are for
staff: by default only `is_staff` users may call them. Set
`FASTPAY_VIEW_PERMISSION_CLASSES` to change who may, and give customers
endpoints of your own that scope the receipt to the caller (step 6 above).

## Reading a status

The status comes from FastPay's `vending/status`, which reads an order started
on either the page or the QR API. `client.transaction_state()` reads it:

| `payment_status` | State | What the service does |
| --- | --- | --- |
| `PAID` | `PAID` | Completes the receipt (when the amount covers it), keeps the transaction, runs the completion hook once |
| `DECLINED` | `FAILED` | The customer pressed Cancel on FastPay's page. Keeps the transaction, runs the failed hook |
| `UNPAID` | `PENDING` | Nothing. Before payment, the transaction fields read `"N/A"` |

A payment that is never paid stays `UNPAID`; after `expiresAt` it simply
cannot be paid any more.

`transaction_state()` also reads the `status` of a validate answer or a
notification (`Success` is paid; `Failed` / `Cancel` failed).

## Tracking the status

FastPay notifies only **successful** payments, and the notification can be
late, or never reach a server that is not public (local development). Combine
it with polling:

1. Give FastPay a callback URL (and the same one as the IPN URL in its panel).
2. While the customer pays, poll the status **every 5 seconds**.
3. Stop when the payment is paid or failed, or once `expiresAt` has passed.

Every status read goes back to FastPay, so polling and the notification can run
at the same time; the receipt is completed and the hook runs exactly once
either way (the receipt row is locked while it is completed).

## Mobile apps (Flutter, Android, iOS)

FastPay publishes SDKs (Flutter's is `fastpay_merchant`) that start the payment
from the app itself — which means shipping the store password inside the app,
where anyone can pull it out. Start the payment on the server instead, with
`start_qr_payment()`, and let the app only open the link it is given:

1. **Register a URL scheme** for the FastPay app to come back to, named the
   way FastPay asks: `appfpclient<AppName>`, e.g. `appfpclientShop`.

   ```xml
   <!-- Android: AndroidManifest.xml, inside the activity -->
   <intent-filter>
       <action android:name="android.intent.action.VIEW" />
       <category android:name="android.intent.category.DEFAULT" />
       <category android:name="android.intent.category.BROWSABLE" />
       <data android:scheme="appfpclientShop" android:host="fast-pay.cash" />
   </intent-filter>
   ```

   ```xml
   <!-- iOS: Info.plist -->
   <key>CFBundleURLTypes</key>
   <array><dict>
       <key>CFBundleURLSchemes</key>
       <array><string>appfpclientShop</string></array>
   </dict></array>
   <!-- so canLaunchUrl can see whether the FastPay app is installed -->
   <key>LSApplicationQueriesSchemes</key>
   <array><string>appFpp</string></array>
   ```

2. **Ask the server for the payment** and open its `appLink`
   (`appFpp://fast-pay.cash/qrpay?qrdata=…&clientUri=appfpclientShop&transactionId=…`).
   In Flutter, with `url_launcher`:

   ```dart
   final link = Uri.parse(payment['app_link']);
   if (await canLaunchUrl(link)) {
     await launchUrl(link, mode: LaunchMode.externalApplication);
   } else {
     // no FastPay app: start a page payment instead and open its
     // redirect_url in the browser (wallet login + OTP)
   }
   ```

3. **When the FastPay app returns** to
   `appfpclientShop://fast-pay.cash?transactionStatus=success&…` (catch it
   with `app_links`), poll the server's status endpoint. Never complete
   anything from the link's own parameters: it is only a sign to look.

Keep polling while the customer is in the FastPay app too; a customer who
pays and never comes back is still completed by the poll or the
notification.

## Python API

### `fastpay.service` (needs Django)

```python
from fastpay import service

# FastPay's payment page for a pending receipt.
# Sets receipt.<ref field> = order id and receipt.<status field> = False.
payment = service.start_payment(
    receipt,
    success_url='https://shop.example/payment/return',  # default: setting
    cancel_url=None,     # default: FASTPAY_CANCEL_URL, then success_url
    callback_url=None,   # default: FASTPAY_CALLBACK_URL (none if blank)
    amount=None,         # default: the receipt's amount field
    description=None,    # the cart line: "Payment <invoice_no>" or "Payment #<pk>"
    cart=None,           # or your own [{name, qty, unit_price, sub_total}]
)
# payment == {'redirect_uri': 'https://staging-pgw.fast-pay.iq/pay?token=...',
#             'orderId': '12T1792323005', 'expiresAt': '<ISO time>'}

# A QR payment, for a mobile app (or a screen to scan).
payment = service.start_qr_payment(
    receipt,
    client_uri='appfpclientShop',  # default: FASTPAY_APP_CLIENT_URI
    expires_in=None,               # minutes, 3-15; default FASTPAY_QR_LIFETIME
    success_url=None, cancel_url=None, callback_url=None, amount=None,
)
# payment == {'qrUrl': 'https://...png', 'qrText': '0002010102...',
#             'appLink': 'appFpp://fast-pay.cash/qrpay?...',
#             'orderId': '...', 'expiresAt': '<ISO time>'}

# Re-read the status from FastPay and apply it; returns FastPay's answer.
status = service.sync_status(receipt)
# status == {'payment_status': 'PAID', 'gw_transaction_id': 'A1KSO55655',
#            'received_amount': '1000.00', 'customer_mobile_number': ..., ...}

# Apply a status you already hold; returns 'PAID', 'FAILED' or 'PENDING'.
service.apply_status(receipt, status)

# What the notification view calls; returns the receipt, or None if unknown.
service.handle_callback(order_id, claimed=None)
```

Every FastPay failure raises a `FastPayError` (below), including an answer
that carries no page or QR code. Calling `sync_status` on a receipt with no
reference raises `ValueError`: that is a bug in the caller, not something
FastPay said.

### `fastpay.client.FastPayClient` (no Django)

```python
from fastpay.conf import get_client   # the shared client from settings
client = get_client()

# or standalone, anywhere:
from fastpay.client import FastPayClient, app_link, transaction_state
client = FastPayClient('749347_291', 'store-password', environment='staging')

page = client.initiate_payment('12T1792323005', 15000,
                               success_url='https://shop.example/payment/return',
                               callback_url='https://shop.example/api/fastpay/callback/')
page['redirect_uri']                         # send the customer here

qr = client.generate_qr('12T1792323006', 15000, expires_in=10)
app_link(qr['qrText'], '12T1792323006', 'appfpclientShop')

status = client.check_status('12T1792323005')
transaction_state(status)                    # 'PAID' | 'FAILED' | 'PENDING'

client.payment_status(order_id)              # vending/status, raw
client.validate_payment(order_id)            # the page's validate, raw
client.refund(order_id, 15000, msisdn='+9647...', refund_secret_key='...')
client.refund_status(order_id)               # {'refund_status': bool, ...}
```

`check_status()` is what the service uses: `payment_status()`, falling back to
`validate_payment()` if the QR host ever refuses to report an order (its
`404` reads as unpaid). A call that got no answer at all is raised, not
retried.

An order id that is not 8 to 32 letters and digits, or a QR lifetime outside
3 to 15 minutes, raises `ValueError` before anything is sent. Amounts go out as
whole dinars, rounded half up, as FastPay requires.

### Exceptions

| Exception | Raised when |
| --- | --- |
| `FastPayError` | Base class; catch this one |
| `FastPayAuthError` | The store credentials are missing |
| `FastPayAPIError` | A call failed or FastPay refused it; carries `code` (FastPay's own, or None when there was no answer), `status_code` and `payload`, and its message ends with FastPay's reason |

## FastPay API contract

| Call | Request |
| --- | --- |
| Page: start | `POST {pgw}/api/v1/public/pgw/payment/initiation`, JSON `store_id`, `store_password`, `order_id`, `bill_amount`, `currency` (`IQD`), `cart` (a JSON **string**), `success_url`, `cancel_url`, `callback_url` |
| Page: validate | `POST {pgw}/api/v1/public/pgw/payment/validate`, JSON `store_id`, `store_password`, `order_id` |
| Refund | `POST {pgw}/api/v1/public/pgw/payment/refund`, plus `amount`, `refund_secret_key`, `msisdn` |
| Refund status | `POST {pgw}/api/v1/public/pgw/payment/refund/validation` |
| QR: start | `POST {qr}/api/v1/public/vending/qr`, JSON `storeId`, `storePassword`, `orderId`, `billAmount`, `currency`, `successUrl`, `cancelUrl`, `callbackUrl`, `expiresIn` |
| Status | `POST {qr}/api/v1/public/vending/status`, JSON `storeId`, `storePassword`, `orderId` |
| Notification | FastPay `POST`s JSON to the callback: `gw_transaction_id`, `merchant_order_id`, `order_id`, `received_amount`, `currency`, `customer_name`, `customer_mobile_number`, `status` (`Success`), `at`, ... |
| App link | `appFpp://fast-pay.cash/qrpay?qrdata=<qrText>&clientUri=appfpclient<App>&transactionId=<order id>` |

What FastPay's staging environment actually does, where the documentation is
silent or differs (checked October 2026):

- The page API answers **HTTP 200 whatever happens**; the outcome is the
  body's `code` (`200`, `404`, `422`) and the reason is in `messages`. The QR
  API answers the real HTTP status, with the reason in `errors`.
- `cart` must be a JSON *string* (`422 The cart must be a valid JSON
  string.`), and is required. Its total is not checked against `bill_amount`.
- `bill_amount` must be an integer (`422 The bill amount must be an
  integer.`).
- An order id is unique across **both** APIs (`Provided Order ID matched with
  a previous Order ID`), and `vending/status` reads an order started on either.
- `vending/status` answers `PAID`, `UNPAID`, or `DECLINED` once the customer
  presses Cancel. `validate` only knows paid: before that it answers code
  `404` "No transaction has been found against your Order ID"; an order it
  never saw is a different `404` ("No such Order ID belongs in payment gateway
  against your store").
- The page lives 10 minutes. It offers the QR code (with a typeable code
  such as `54RW-OS98-90CC`) and a wallet login with OTP.
- After paying or cancelling, the page sends the browser to `success_url` /
  `cancel_url` with `?order_id=…` added, by script — so any URL the customer can
  open works, `localhost` included. Only a status read counts; never complete
  anything from that query.
- `expiresIn` is 3 to 15 minutes; the QR API took `billAmount` 500, below the
  documented minimum of 1000.
- FastPay sits behind Cloudflare, which refuses Python's own `urllib` user
  agent with error `1010`. The client sends `User-Agent: fastpay/<version>`.

## Logging

Everything is logged to the **`fastpay`** logger: payments started, completed
or failed, every notification (what it claimed and what FastPay confirmed), a
payment short of its receipt (as an error), and hook failures. Pick one of:

- **Let the package write a file**, wherever the project keeps its logs:

  ```python
  FASTPAY_LOG_FILE = BASE_DIR / 'logs' / 'fastpay.log'   # a logs/ folder
  FASTPAY_LOG_FILE = 'fastpay.log'                        # the project root
  FASTPAY_LOG_LEVEL = 'INFO'                              # default
  ```

  A relative path starts at `BASE_DIR`, a missing folder is created, and the
  file is only created once something is logged. Those records then go to
  that file only, not to the root logger.

- **Route the logger yourself** in `LOGGING`, e.g. into a file the project
  already has:

  ```python
  LOGGING['loggers']['fastpay'] = {
      'handlers': ['file'], 'level': 'INFO', 'propagate': False,
  }
  ```

- **Neither**: no file is written. Python drops `INFO` records and prints
  warnings and errors to stderr, as for any logger nobody configured.

## Testing

### In your project

Mock FastPay at the client the service uses:

```python
from unittest.mock import MagicMock, patch

fastpay = MagicMock()
fastpay.initiate_payment.return_value = {'redirect_uri': 'https://pay.example/pay?token=T'}
fastpay.generate_qr.return_value = {'qrUrl': 'https://x/qr.png', 'qrText': '000201...'}
fastpay.check_status.return_value = {'payment_status': 'PAID', 'received_amount': '1000.00'}
with patch('fastpay.service.get_client', return_value=fastpay):
    ...  # start / sync / notification
```

An unpaid status is `{'payment_status': 'UNPAID', 'received_amount': '1000.0000', 'gw_transaction_id': 'N/A', ...}`.

### The package's own tests

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests never call FastPay. GitHub Actions runs them on Python 3.10 with the
oldest supported dependencies and on Python 3.13 with the newest.

### Against FastPay's staging environment

From a project that has staging credentials in its `.env`:

```bash
uv run python manage.py shell -c "
import time
from fastpay.conf import get_client
from fastpay.client import transaction_state
c = get_client()
order_id = f'TEST{int(time.time())}'
p = c.initiate_payment(order_id, 1000, success_url='https://example.com/return')
print(p['redirect_uri'])                                 # open it and pay
print(transaction_state(c.check_status(order_id)))       # PENDING, then PAID
"
```

Pay with a staging wallet: **Login to Pay**, the wallet's number and password,
then the OTP `123456`. Cancel instead to see `FAILED`.

Add `--with-editable ../fastpay` after `uv run` to try an unreleased change
(see [UPDATING.md](UPDATING.md)).

## Updating and releasing

How to release a new version and move a project onto it:
[UPDATING.md](UPDATING.md). Every release is listed in
[CHANGELOG.md](CHANGELOG.md).

## Known limits

- FastPay notifies successful payments only; a cancel is learnt by polling.
- One currency: the Iraqi dinar, in whole dinars.
- The app link and the return to the calling app follow FastPay's deep-link
  documentation; staging cannot exercise them without the FastPay app on a
  phone, so test them on a device before release.
- The status is read from FastPay on every poll and notification; the package
  keeps only the last verified transaction (in
  `FASTPAY_RECEIPT_DETAILS_FIELD`), not a history.
- FastPay's notification carries no signature, so it is only ever a hint to
  re-read the status.
- `refund()` is a thin call with no receipt bookkeeping; recording a refund is
  the project's.
