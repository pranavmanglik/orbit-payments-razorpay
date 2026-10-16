import asyncio
import hashlib
import hmac
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from orbit_payments import PaymentRequest, PaymentWebhookError, RefundRequest

from orbit_payments_razorpay import RazorpayConfig, RazorpayPaymentProvider


def config() -> RazorpayConfig:
    return RazorpayConfig(key_id="key-id", key_secret="key-secret", webhook_secret="webhook-secret")


class FakeRazorpayClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.order = SimpleNamespace(create=self.create_order, fetch=self.fetch_order)
        self.payment = SimpleNamespace(refund=self.create_refund)

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("order.create", (payload,)))
        return {
            "id": "order_123",
            "amount": payload["amount"],
            "currency": payload["currency"],
            "status": "created",
            "receipt": payload["receipt"],
            "created_at": 1_780_000_000,
        }

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("order.fetch", (order_id,)))
        return {
            "id": order_id,
            "amount": 100,
            "currency": "INR",
            "status": "paid",
            "receipt": "order-1",
        }

    def create_refund(self, payment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("payment.refund", (payment_id, payload)))
        return {
            "id": "rfnd_123",
            "payment_id": payment_id,
            "amount": payload.get("amount", 100),
            "currency": "INR",
            "status": "processed",
        }


@pytest.mark.asyncio
async def test_create_retrieve_refund_and_drain() -> None:
    client = FakeRazorpayClient()
    provider = RazorpayPaymentProvider(config(), client=client)
    payment = await provider.create_payment(
        PaymentRequest(amount_minor=100, currency="INR", reference="order-1")
    )
    retrieved = await provider.get_payment("order_123")
    refund = await provider.refund(
        RefundRequest(payment_id="pay_123", reference="refund-1", amount_minor=50)
    )

    assert payment.provider_id == "order_123"
    assert payment.reference == "order-1"
    assert retrieved.status.value == "succeeded"
    assert refund.amount_minor == 50
    assert refund.status.value == "succeeded"
    assert [name for name, _ in client.calls] == [
        "order.create",
        "order.fetch",
        "payment.refund",
    ]
    await provider.aclose()


def test_webhook_checks_raw_signature_before_parsing_and_rejects_duplicate_keys() -> None:
    provider = RazorpayPaymentProvider(config(), client=FakeRazorpayClient())
    body = json.dumps({"event": "payment.captured", "created_at": 1_780_000_000}).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    event = provider.verify_webhook(body, signature)
    assert event.event_type == "payment.captured"
    assert event.event_id == hashlib.sha256(body).hexdigest()

    with pytest.raises(PaymentWebhookError):
        provider.verify_webhook(body, "0" * 64)

    duplicate_body = b'{"event":"payment.captured","event":"payment.failed"}'
    duplicate_signature = hmac.new(b"webhook-secret", duplicate_body, hashlib.sha256).hexdigest()
    with pytest.raises(PaymentWebhookError):
        provider.verify_webhook(duplicate_body, duplicate_signature)


def test_webhook_never_exposes_an_order_id_as_a_payment_id() -> None:
    provider = RazorpayPaymentProvider(config(), client=FakeRazorpayClient())

    for payload, expected_payment_id in (
        ({"order": {"entity": {"id": "order_123"}}}, None),
        (
            {
                "order": {"entity": {"id": "order_123"}},
                "payment": {"entity": {"id": "pay_123"}},
            },
            "pay_123",
        ),
    ):
        body = json.dumps(
            {"event": "order.paid", "payload": payload}, separators=(",", ":")
        ).encode()
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

        assert provider.verify_webhook(body, signature).payment_id == expected_payment_id


def test_checkout_signature_matches_documented_order_payment_pair() -> None:
    provider = RazorpayPaymentProvider(config(), client=FakeRazorpayClient())
    message = b"order_123|pay_123"
    signature = hmac.new(b"key-secret", message, hashlib.sha256).hexdigest()
    assert provider.verify_checkout_signature("order_123", "pay_123", signature)
    assert not provider.verify_checkout_signature("order_123", "pay_other", signature)


@pytest.mark.asyncio
async def test_closed_provider_rejects_new_operations() -> None:
    provider = RazorpayPaymentProvider(config(), client=FakeRazorpayClient())
    await provider.aclose()
    with pytest.raises(RuntimeError):
        await provider.get_payment("order_123")


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_skip_owned_session_cleanup(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    class FakeSession:
        closed = False

        def mount(self, *_: object) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    session = FakeSession()

    class BlockingClient:
        def __init__(self, *, session: FakeSession, auth: tuple[str, str]) -> None:
            self.session = session
            self.auth = auth
            self.order = SimpleNamespace(fetch=self.fetch_order)

        def fetch_order(self, order_id: str) -> dict[str, Any]:
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test did not release fake request")
            return {
                "id": order_id,
                "amount": 100,
                "currency": "INR",
                "status": "paid",
                "receipt": "order-1",
            }

    monkeypatch.setattr("orbit_payments_razorpay.provider.requests.Session", lambda: session)
    monkeypatch.setattr("orbit_payments_razorpay.provider.razorpay.Client", BlockingClient)
    provider = RazorpayPaymentProvider(config())
    operation = asyncio.create_task(provider.get_payment("order_123"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        close_waiter = asyncio.create_task(provider.aclose())
        await asyncio.sleep(0)
        assert not session.closed

        close_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_waiter
        assert not session.closed
    finally:
        release.set()

    assert (await operation).provider_id == "order_123"
    await provider.aclose()
    assert session.closed


@pytest.mark.asyncio
async def test_failed_owned_session_close_can_be_retried() -> None:
    """Successfully closed sessions are released while failed sessions remain owned."""

    class Session:
        def __init__(self, *, fail_once: bool) -> None:
            self.close_calls = 0
            self.fail_once = fail_once
            self.closed = False

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("session close included sensitive transport detail")
            self.closed = True

    class Client:
        def __init__(self, session: Session) -> None:
            self.session = session

    successful_session = Session(fail_once=False)
    retry_session = Session(fail_once=True)
    provider = RazorpayPaymentProvider(config())
    provider._clients.extend((Client(successful_session), Client(retry_session)))

    with pytest.raises(RuntimeError, match="Could not close every") as caught:
        await provider.aclose()
    assert "sensitive" not in str(caught.value)
    assert successful_session.closed
    assert retry_session.close_calls == 1
    assert len(provider._clients) == 1

    await provider.aclose()
    await provider.aclose()
    assert retry_session.closed
    assert successful_session.close_calls == 1
    assert retry_session.close_calls == 2
