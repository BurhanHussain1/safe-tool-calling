"""Strip sensitive values before anything is written to the audit log.

Two categories, deliberately handled differently:

**Secrets are erased.** A field named like a credential is replaced wholesale.
There is no version of an API key that is useful in a log and safe to keep.

**Identifiers are partially masked.** ``ana@acme.io`` becomes ``a**@acme.io``.
An audit trail that cannot distinguish two customers is not much of an audit
trail, so enough is kept to correlate records while the full address is not
sitting in a log file.

Redaction runs on the way *into* the log, never on the way out — a value that
was never stored cannot leak from storage later.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

#: Substring match on the key name, lowercased. Broad on purpose: a false
#: positive costs a masked field in a log, a false negative costs a leaked key.
_SECRET_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "session",
    "cookie",
    "signature",
)

_EMAIL = re.compile(r"([\w.+-])([\w.+-]*)(@[\w-]+\.[\w.-]+)")

#: Bearer/sk- style credentials that appear inside free text rather than as a
#: field of their own.
_INLINE_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/-]{8,}=*|gh[pousr]_[A-Za-z0-9]{16,})"
)


def is_secret_key(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def mask_email(address: str) -> str:
    """Keep the first character and the domain: ``ana@acme.io`` → ``a**@acme.io``."""

    def _mask(match: re.Match[str]) -> str:
        first, rest, domain = match.groups()
        return f"{first}{'*' * min(max(len(rest), 1), 8)}{domain}"

    return _EMAIL.sub(_mask, address)


def redact_text(text: str) -> str:
    """Mask emails and inline credentials in a free-text string."""
    return mask_email(_INLINE_SECRET.sub(REDACTED, text))


def redact(value: Any, *, _key: str | None = None) -> Any:
    """Recursively redact a JSON-like structure.

    Containers are rebuilt rather than mutated, so the caller's copy — the one
    the executor is still using — is untouched.
    """
    if _key is not None and is_secret_key(_key):
        return REDACTED

    if isinstance(value, dict):
        return {key: redact(item, _key=str(key)) for key, item in value.items()}

    if isinstance(value, list):
        return [redact(item, _key=_key) for item in value]

    if isinstance(value, tuple):
        return tuple(redact(item, _key=_key) for item in value)

    if isinstance(value, str):
        return redact_text(value)

    return value
