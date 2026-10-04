"""Masking for example values shown in profiles and check reports.

``mask`` uses the shared redaction engine (``redact.mask_text``) with its default
rules and no email allow-list: emails -> ``[email]``, phone numbers -> ``[phone]``,
credentials -> ``[credential]``, ``user:pass@`` in URLs -> ``[url-credential]``.
Values are cut to ``limit`` characters.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp.redact import mask_text

EXAMPLE_CHARS = 80


def mask(text: str) -> str:
    """Mask emails, phone numbers and credentials in ``text``."""
    return mask_text(text) if text else (text or "")


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
