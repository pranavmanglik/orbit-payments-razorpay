# Copyright 2026-present Orbit Contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Razorpay Orders adapter with bounded blocking-SDK execution and webhook verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import razorpay
import requests
from orbit_payments import (
    PaymentOperationError,
    PaymentRequest,
    PaymentSession,
    PaymentStatus,
    PaymentWebhookError,
    RefundRequest,
    RefundResult,
    RefundStatus,
    VerifiedWebhook,
)
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter

from orbit_payments_razorpay.config import RazorpayConfig

_MAX_WEBHOOK_BYTES = 1_048_576


class _TimeoutAdapter(HTTPAdapter):
    """Apply a finite timeout to SDK requests unless the caller supplied one."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__()
        self._timeout_seconds = timeout_seconds

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> Response:
        """Send one SDK request with the configured finite timeout when unspecified."""
        return super().send(
            request,
            stream=stream,
            timeout=self._timeout_seconds if timeout is None else timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )


class RazorpayPaymentProvider:
    """Async capability adapter over Razorpay's synchronous official Python SDK.

    Network calls run in a worker thread because the provider SDK is synchronous. A semaphore
    bounds submitted operations. Cancellation waits for the bounded HTTP call to finish before
    releasing its slot, preventing a cancelled coroutine from freeing capacity while its request
    still consumes a worker and socket.
    """

    def __init__(self, config: RazorpayConfig, *, client: Any | None = None) -> None:
        self._config = config
        self._injected_client = client
        self._thread_local = threading.local()
        self._clients: list[Any] = []
        self._clients_lock = threading.Lock()
        self._owns_clients = client is None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._active: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def create_payment(self, request: PaymentRequest) -> PaymentSession:
        """Create a Razorpay Order with a stable receipt for reconciliation."""
        self._ensure_open()
        notes = {**request.metadata, "orbit_reference": request.reference}
        payload = {
            "amount": request.amount_minor,
            "currency": request.currency,
            "receipt": request.reference,
            "notes": notes,
        }
        try:
            result = await self._run("order.create", payload)
            return self._session(result)
        except PaymentOperationError:
            raise
        except Exception:
            raise PaymentOperationError("Razorpay could not create the payment order.") from None

    async def get_payment(self, provider_id: str) -> PaymentSession:
        """Retrieve a Razorpay Order by its provider-issued ID."""
        self._ensure_open()
        try:
            result = await self._run("order.fetch", provider_id)
            return self._session(result)
        except PaymentOperationError:
            raise
        except Exception:
            raise PaymentOperationError("Razorpay could not retrieve the payment order.") from None

    async def refund(self, request: RefundRequest) -> RefundResult:
        """Create a full or partial refund against a Razorpay payment ID."""
        self._ensure_open()
        payload: dict[str, object] = {"notes": {"orbit_reference": request.reference}}
        if request.amount_minor is not None:
            payload["amount"] = request.amount_minor
        try:
            result = await self._run("payment.refund", request.payment_id, payload)
            return RefundResult(
                provider="razorpay",
                provider_id=str(result["id"]),
                payment_id=str(result.get("payment_id", request.payment_id)),
                amount_minor=int(result["amount"]),
                currency=str(result["currency"]).upper(),
                status=self._refund_status(str(result.get("status", "pending"))),
            )
        except PaymentOperationError:
            raise
        except (KeyError, TypeError, ValueError):
            raise PaymentOperationError("Razorpay returned an invalid refund response.") from None
        except Exception:
            raise PaymentOperationError("Razorpay could not create the refund.") from None

    def verify_webhook(self, raw_body: bytes, signature: str) -> VerifiedWebhook:
        """Verify the raw Razorpay webhook body with HMAC-SHA256 before parsing JSON."""
        if not isinstance(raw_body, bytes) or not raw_body or len(raw_body) > _MAX_WEBHOOK_BYTES:
            raise PaymentWebhookError("Razorpay webhook body is invalid or too large.")
        if (
            not isinstance(signature, str)
            or len(signature) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in signature)
        ):
            raise PaymentWebhookError("Razorpay webhook signature is missing or invalid.")
        expected = hmac.new(
            self._config.webhook_secret.get_secret_value().encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise PaymentWebhookError("Razorpay webhook signature verification failed.")
        try:
            event = json.loads(raw_body.decode("utf-8"), object_pairs_hook=self._unique_object)
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError("Invalid event shape.")
            payload = event.get("payload", {})
            payment_data = payload.get("payment", {}) if isinstance(payload, dict) else {}
            payment_entity = (
                payment_data.get("entity", {}) if isinstance(payment_data, dict) else {}
            )
            payment_id = payment_entity.get("id") if isinstance(payment_entity, dict) else None
            timestamp = event.get("created_at")
            return VerifiedWebhook(
                provider="razorpay",
                # The event fingerprint is deterministic when the webhook payload has no event ID.
                event_id=hashlib.sha256(raw_body).hexdigest(),
                event_type=event["event"],
                payment_id=str(payment_id) if payment_id is not None else None,
                occurred_at=(datetime.fromtimestamp(int(timestamp), tz=UTC) if timestamp else None),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise PaymentWebhookError("Razorpay webhook payload is invalid.") from exc

    def verify_checkout_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify the client-returned Checkout signature using Razorpay's documented message."""
        if not all(isinstance(value, str) and value for value in (order_id, payment_id, signature)):
            return False
        if len(order_id) > 255 or len(payment_id) > 255 or len(signature) != 64:
            return False
        if any(char not in "0123456789abcdefABCDEF" for char in signature):
            return False
        message = f"{order_id}|{payment_id}".encode()
        expected = hmac.new(
            self._config.key_secret.get_secret_value().encode("utf-8"), message, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature.lower())

    async def aclose(self) -> None:
        """Stop submissions, drain worker calls, and close sessions created by this adapter.

        The cleanup task is shared by concurrent callers and shielded from cancellation of any
        individual caller. A caller may stop waiting, but cleanup continues and a later call can
        await the same task. Sessions supplied through an injected SDK client remain caller-owned.
        """
        self._closed = True
        task = self._close_task
        retry_failed_close = (
            task is not None and task.done() and (task.cancelled() or task.exception() is not None)
        )
        if task is None or retry_failed_close:
            task = asyncio.create_task(self._close_resources())
            self._close_task = task
        await asyncio.shield(task)

    async def _close_resources(self) -> None:
        """Drain submitted SDK calls before closing every adapter-owned session."""
        active = tuple(self._active)
        if active:
            await asyncio.gather(*(asyncio.shield(task) for task in active), return_exceptions=True)
        if self._owns_clients:
            with self._clients_lock:
                clients = tuple(self._clients)

            async def close_session(client: Any) -> None:
                session = getattr(client, "session", None)
                if session is not None:
                    await asyncio.to_thread(session.close)

            outcomes = await asyncio.gather(
                *(close_session(client) for client in clients), return_exceptions=True
            )
            failed_client_ids = {
                id(client)
                for client, outcome in zip(clients, outcomes, strict=True)
                if isinstance(outcome, BaseException)
            }
            with self._clients_lock:
                self._clients = [
                    client for client in self._clients if id(client) in failed_client_ids
                ]
            failure = next(
                (result for result in outcomes if isinstance(result, BaseException)), None
            )
            if failure is not None:
                raise RuntimeError("Could not close every Razorpay SDK session.") from None

    async def __aenter__(self) -> RazorpayPaymentProvider:
        if self._closed:
            raise RuntimeError("Razorpay payment provider is closed.")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _run(self, operation: str, *args: object) -> Any:
        self._ensure_open()
        async with self._semaphore:
            self._ensure_open()
            task = asyncio.create_task(asyncio.to_thread(self._invoke, operation, *args))
            self._active.add(task)
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            self._active.discard(task)
            if cancelled:
                # Retrieve any worker exception to avoid an unhandled-task warning, but preserve
                # cancellation as the public outcome once the bounded call has drained.
                with suppress(BaseException):
                    task.result()
                raise asyncio.CancelledError
            return task.result()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Razorpay payment provider is closed.")

    def _invoke(self, operation: str, *args: object) -> Any:
        """Resolve the SDK method inside its worker so sessions stay thread-local."""
        if self._injected_client is not None:
            client = self._injected_client
        else:
            client = getattr(self._thread_local, "client", None)
            if client is None:
                session = requests.Session()
                adapter = _TimeoutAdapter(self._config.timeout_seconds)
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                client = razorpay.Client(
                    session=session,
                    auth=(
                        self._config.key_id.get_secret_value(),
                        self._config.key_secret.get_secret_value(),
                    ),
                )
                # The SDK's retry switch defaults off. Preserve one attempt because this adapter
                # cannot promise provider idempotency for an ambiguous create/refund response.
                self._thread_local.client = client
                with self._clients_lock:
                    self._clients.append(client)
        method: Any = client
        for name in operation.split("."):
            method = getattr(method, name)
        return method(*args)

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member.")
            result[key] = value
        return result

    @staticmethod
    def _session(value: Mapping[str, Any]) -> PaymentSession:
        status = str(value.get("status", "unknown"))
        notes = value.get("notes", {})
        reference = notes.get("orbit_reference") if isinstance(notes, Mapping) else None
        if reference is None:
            reference = value.get("receipt")
        normalized = {
            "created": PaymentStatus.CREATED,
            "attempted": PaymentStatus.REQUIRES_ACTION,
            "paid": PaymentStatus.SUCCEEDED,
        }.get(status, PaymentStatus.UNKNOWN)
        created = value.get("created_at")
        return PaymentSession(
            provider="razorpay",
            provider_id=str(value["id"]),
            provider_resource="order",
            reference=(str(reference) if reference is not None else None),
            amount_minor=int(value["amount"]),
            currency=str(value["currency"]).upper(),
            status=normalized,
            client_token=str(value["id"]),
            created_at=(datetime.fromtimestamp(int(created), tz=UTC) if created else None),
        )

    @staticmethod
    def _refund_status(value: str) -> RefundStatus:
        return {
            "processed": RefundStatus.SUCCEEDED,
            "created": RefundStatus.PENDING,
            "pending": RefundStatus.PENDING,
            "failed": RefundStatus.FAILED,
        }.get(value, RefundStatus.UNKNOWN)


__all__ = ["RazorpayPaymentProvider"]
