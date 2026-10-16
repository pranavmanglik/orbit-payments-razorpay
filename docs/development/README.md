# Razorpay adapter development

Install the capability checkout first:

```bash
python -m pip install -e ../orbit-workers -e ../orbit-payments
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy
```

Keep tests deterministic with injected fake SDK resources and signed synthetic webhook payloads.
Never use live credentials in CI. Provider test-mode smoke checks are separate release gates.

The current local unit matrix passes on CPython 3.11, 3.12, 3.13, and 3.14. It validates adapter
behavior with an injected fake SDK client; it does not verify Razorpay network access, merchant
account configuration, webhook delivery, or provider-side settlement.
