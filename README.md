# Orbit Payments Razorpay

`orbit-payments-razorpay` implements the `orbit-payments` contract using Razorpay Orders, refunds,
and the official Razorpay Python SDK 2.x.

The 2.x SDK line removes its runtime dependency on `pkg_resources`. Setuptools 82 removed that
module, so SDK 1.x can fail during import in clean runtime environments. Orbit does not add or pin
Setuptools as a runtime compatibility shim; see the [Razorpay SDK changelog](https://github.com/razorpay/razorpay-python/blob/master/CHANGELOG.md)
and [Setuptools deprecation notice](https://setuptools.pypa.io/en/stable/deprecated/pkg_resources.html).

```bash
python -m pip install orbit-payments-razorpay
```

```python
from orbit_payments import PaymentRequest
from orbit_payments_razorpay import RazorpayConfig, RazorpayPaymentProvider

provider = RazorpayPaymentProvider(RazorpayConfig.from_environment())
try:
    order = await provider.create_payment(
        PaymentRequest(amount_minor=199900, currency="INR", reference="order-8742")
    )
    # Use order.client_token as the Razorpay Order ID in the authorized checkout flow.
finally:
    await provider.aclose()
```

Razorpay's official Python SDK performs synchronous HTTP. This adapter runs operations in worker
threads with per-worker sessions, finite request timeouts, and a bounded concurrency semaphore, so
it does not block Orbit's event loop. SDK retries remain disabled by the supported SDK default; after
an ambiguous timeout, reconcile by the stable merchant receipt before attempting another create.
The adapter does not claim a
provider idempotency guarantee for create or refund.

Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET` from a secret manager.
No credentials are provided by default. Webhooks and Checkout responses have different signatures;
use `verify_webhook` for raw webhook bytes and `verify_checkout_signature` for the documented
`order_id|payment_id` Checkout response message.

The adapter does not capture/store card data, issue payouts, manage subscriptions, reconcile
settlements, or provide durable webhook processing. Test account configuration and provider
permissions before deployment. See [architecture](docs/architecture/overview.md),
[operations](docs/operations/README.md), [security](docs/security/overview.md),
[development](docs/development/README.md), and the [documentation index](docs/README.md).

Licensed under Apache-2.0.
