# Changelog

All notable changes to this package. Projects pin a tag, so read every entry
between your tag and the new one before upgrading, and do what its
**Upgrade notes** say. How to release and how to upgrade: [UPDATING.md](UPDATING.md).

## v0.1.0

First release: FastPay payments for Django, built against FastPay's staging
environment, its developer portal and its Postman collection.

- Start a payment on FastPay's payment page for a pending receipt and return
  the page to send the customer to (`redirect_uri`) and when it stops being
  payable (`expiresAt`, 10 minutes).
- Start a QR payment for a mobile app: the code (`qrUrl`, `qrText`) and a deep
  link that opens the FastPay app and returns to the calling app (`appLink`).
- Status read from `vending/status` for both kinds, as `PAID`, `FAILED`
  (cancelled on the page) or `PENDING`, falling back to the page's `validate`
  if the QR host refuses (`client.check_status()`).
- A receipt completes only when FastPay received at least its amount, as
  FastPay asks merchants to check.
- Public notification (IPN) endpoint that re-reads every status from FastPay;
  the body is never trusted.
- Receipt model, reference, status and amount fields set by settings;
  optional JSON field that keeps the verified transaction; optional hooks on
  completion and on failure.
- Client for the page, QR, status, validate, refund and refund-status calls,
  with its own User-Agent (FastPay's Cloudflare refuses urllib's).
- Logs to the `fastpay` logger, and to `FASTPAY_LOG_FILE` when set.

Upgrade notes: none (first release).
