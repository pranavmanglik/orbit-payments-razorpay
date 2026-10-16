# Razorpay operations

Load `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET` from a secret manager.
Use test keys for test environments and restrict live keys to the minimum account capabilities.
The supported SDK line defaults to no automatic retries. This adapter makes no retry call; after an
ambiguous response, reconcile with Razorpay before repeating create/refund operations.

Pass the exact raw bytes and `X-Razorpay-Signature` value to `verify_webhook`. Do not parse and
reserialize the JSON before verification. The returned event ID is a SHA-256 fingerprint of these
exact bytes. Persist the verified envelope before returning success and deduplicate it in an
application-owned durable store; request acknowledgment, broker durability, workers, and fulfillment
are application responsibilities. See the capability's
[durable webhook processing guide](https://github.com/orbit-projects/orbit-payments/blob/main/docs/operations/webhook-processing.md).
If a create call times out, inspect/reconcile the order using its merchant receipt before retrying.

## Application shutdown

Close the provider during application lifespan shutdown, preferably with its async context
manager. Shutdown stops accepting new SDK operations, waits for already-submitted calls to finish,
and then closes the Requests sessions created by the adapter's worker threads. The configured
finite request timeout bounds normal drain time; a synchronous SDK call cannot be forcibly
cancelled by Python. If a shutdown caller is cancelled, resource cleanup continues in the background
and a later `aclose()` waits for it. If an owned session close fails, the provider retains only the
sessions still requiring cleanup and retries them on the next call; successful sessions are not
closed twice. When you inject a preconfigured SDK client, you retain ownership of that client and
its session and must close them yourself.

The unit suite uses fake SDK resources and synthetic HMAC signatures. No Razorpay test-mode or live
account acceptance is claimed. Follow the official [Python SDK](https://github.com/razorpay/razorpay-python)
and [Razorpay webhook documentation](https://razorpay.com/docs/webhooks/) for provider-side setup.
