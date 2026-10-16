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
"""Validated Razorpay credentials and bounded synchronous SDK settings."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class RazorpayConfig(BaseModel):
    """Razorpay API/webhook credentials and limits; secrets are never repr'd in plaintext."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: SecretStr
    key_secret: SecretStr
    webhook_secret: SecretStr
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_concurrent_requests: int = Field(default=8, ge=1, le=256)

    @field_validator("key_id", "key_secret", "webhook_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        """Reject empty API and webhook credentials after trimming whitespace."""
        if not value.get_secret_value().strip():
            raise ValueError("Razorpay credentials must not be empty.")
        return value

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RazorpayConfig:
        """Load credentials by documented names and fail closed when any is absent."""
        values = os.environ if environ is None else environ
        required = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")
        missing = tuple(name for name in required if name not in values)
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}.")
        return cls(
            key_id=SecretStr(values[required[0]]),
            key_secret=SecretStr(values[required[1]]),
            webhook_secret=SecretStr(values[required[2]]),
        )


__all__ = ["RazorpayConfig"]
