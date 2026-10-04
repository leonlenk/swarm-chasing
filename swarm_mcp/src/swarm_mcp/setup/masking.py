"""Tiny local masking for example values shown in profiles and check reports.

Emails become ``[email]`` and phone-like strings ``[phone]`` (same patterns as
``toolkit.Scrubber``, no allow-list). Values are cut to ``limit`` characters.
This is a stand-in: once the redact engine lands, ``mask`` should delegate to it.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp.toolkit import _EMAIL_RE, _PHONE_RE

EXAMPLE_CHARS = 80


def _phone(m) -> str:
    digits = sum(c.isdigit() for c in m.group(0))
    return "[phone]" if 8 <= digits <= 15 else m.group(0)


def mask(text: str) -> str:
    """Mask emails and phone numbers in ``text``."""
    if not text:
        return text or ""
    return _PHONE_RE.sub(_phone, _EMAIL_RE.sub("[email]", text))


def show(value: Any, limit: int = EXAMPLE_CHARS) -> str:
    """A value as a short, masked, single-line string for reports."""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(value)
    s = mask(" ".join(s.split()))
    return s if len(s) <= limit else s[: limit - 1] + "…"
