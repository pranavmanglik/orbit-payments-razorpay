# Razorpay adapter architecture

This package implements `orbit_payments.PaymentProvider` through Razorpay's Python SDK. The
capability owns the common protocol and models; this adapter owns Razorpay order/payment/refund
resources, API credentials, webhook verification, thread execution, and the SDK session lifecycle.
It requires SDK 2.x because that line removes the runtime `pkg_resources` import that fails with
Setuptools 82 and later.

## Implemented operations

- `create_payment`: creates an Order using integer minor units, uppercase currency, merchant
  reference as the Razorpay receipt, and bounded metadata as notes.
- `get_payment`: retrieves an Order by its Razorpay Order ID.
- `refund`: creates a full or partial refund against a Razorpay Payment ID.
- `verify_webhook`: validates HMAC-SHA256 over the original raw body with the webhook secret before
  parsing and returning normalized metadata. The normalized `payment_id` is populated only from a
  Razorpay Payment entity; an Order ID is never relabeled as a payment ID when the event omits its
  payment entity.
- `verify_checkout_signature`: verifies the Checkout response signature over `order_id|payment_id`
  with the API secret.

Razorpay's SDK is synchronous. Calls run in `asyncio.to_thread` and are bounded by a semaphore and
finite HTTP timeout. The SDK client is created inside its worker thread, so its Requests session is
used by that same thread. A new client and session are created per worker thread and retained until
provider shutdown.

Cancellation does not terminate an in-progress synchronous HTTP call. The coroutine waits for its
bounded worker call to finish before releasing the semaphore slot, so cancellation cannot make the
adapter appear to have spare capacity while a socket is still in use. Shutdown first rejects new
operations, drains submitted worker calls, and then closes every session created by the adapter.
Cleanup is represented by one shared task: concurrent shutdown calls await the same cleanup, and
cancelling one caller only cancels that caller's wait. A later `aclose()` can await cleanup again.
When a caller injects an SDK client, its client and session remain caller-owned; shutdown drains
calls made through it but does not close it. Use `async with` or call `aclose()` during application
shutdown to release adapter-owned sessions deterministically.

These lifecycle guarantees rely on the configured finite HTTP timeout. Python cannot forcibly stop
a synchronous SDK call, so an indefinitely blocked call would also delay shutdown if the underlying
transport ignored its timeout.

Razorpay's standard Orders API does not provide the same idempotency contract as Stripe's
PaymentIntent API. The adapter does not retry order creation automatically. `receipt` is a
merchant-side reconciliation reference, not a claim of exactly-once processing.
