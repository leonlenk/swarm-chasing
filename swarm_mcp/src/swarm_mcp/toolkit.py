"""Small helpers that make tools friendly to LLM callers.

Module authors mostly need:
- ``ToolInputError``: raise it with a clear, actionable message for bad input.
- ``clamp_limit``: apply default/max result limits and say when clamping happened.
- ``truncate``: cut long text and mark it as truncated.
- ``Scrubber``: mask emails/phone numbers in returned text.
- ``parse_time``: accept dates/datetimes in the formats an LLM is likely to send.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import anyio.to_thread

from swarm_mcp.sdk import ToolError


class ToolInputError(ValueError):
    """Bad tool input. The message is returned to the caller verbatim."""


def wrap_tool(fn: Callable[..., Any], tool_name: str, log: logging.Logger) -> Callable[..., Any]:
    """Wrap a tool function so that:

    - sync functions run in a worker thread (slow loads don't block the event loop);
    - ``ToolInputError`` becomes a clean error message for the caller;
    - unexpected exceptions are logged (with traceback) to stderr and reported
      to the caller as a one-line message, never a traceback.

    The signature is preserved (``functools.wraps``) so the SDK still builds the
    JSON schema from the original parameters.
    """
    is_async = inspect.iscoroutinefunction(fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            if is_async:
                return await fn(*args, **kwargs)
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except ToolError:
            raise
        except ToolInputError as e:
            raise ToolError(str(e)) from None
        except FileNotFoundError as e:
            log.warning("tool %s: file not found: %s", tool_name, e)
            raise ToolError(f"Data file not found: {e.filename or e}. Check SWARM_DATA_DIR.") from None
        except Exception as e:  # noqa: BLE001 - last-resort guard
            log.exception("tool %s raised", tool_name)
            raise ToolError(
                f"Internal error ({type(e).__name__}: {e}). The traceback was logged to the server's stderr."
            ) from None

    return wrapper


def clamp_limit(limit: int | None, default: int, maximum: int) -> tuple[int, str | None]:
    """Return (effective_limit, note). The note explains any clamping."""
    if limit is None:
        return default, None
    if limit < 1:
        return 1, f"limit {limit} raised to 1"
    if limit > maximum:
        return maximum, f"limit {limit} capped at the maximum of {maximum}"
    return limit, None


def truncate(text: str | None, max_chars: int) -> tuple[str, bool]:
    """Return (text, was_truncated). Truncated text ends with an explicit marker."""
    if text is None:
        return "", False
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rstrip()
    return f"{cut} …[truncated, {len(text) - len(cut)} more chars]", True


# --------------------------------------------------------------------------- privacy

_EMAIL_RE = re.compile(r"(?<![\w.+%-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")
# International numbers must start with "+"; national ones must look like the
# North American 3-3-4 pattern with separators. Bare digit runs are left alone
# (too many false positives: ids, counts, timestamps).
_PHONE_RE = re.compile(
    r"(?<![\w/.:#=@-])"
    r"(?:"
    r"\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}"
    r"|(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}"
    r")"
    r"(?![\w/-]|\.\d)"
)


class Scrubber:
    """Masks emails (except allow-listed domains) as ``[email]`` and phone-like
    strings as ``[phone]``. Disabled scrubbers pass text through unchanged."""

    def __init__(self, enabled: bool = True, email_allowlist: Iterable[str] = ("agentvillage.org",)):
        self.enabled = enabled
        self.allow = tuple(d.lower().lstrip("@.") for d in email_allowlist)

    def _email(self, m: re.Match[str]) -> str:
        domain = m.group(1).lower()
        if any(domain == d or domain.endswith("." + d) for d in self.allow):
            return m.group(0)
        return "[email]"

    @staticmethod
    def _phone(m: re.Match[str]) -> str:
        digits = sum(c.isdigit() for c in m.group(0))
        return "[phone]" if 8 <= digits <= 15 else m.group(0)

    def __call__(self, text: str | None) -> str:
        if not text or not self.enabled:
            return text or ""
        text = _EMAIL_RE.sub(self._email, text)
        return _PHONE_RE.sub(self._phone, text)


# --------------------------------------------------------------------------- time

TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"  # matches the dataset's UTC timestamp strings


def parse_time(value: str | None, *, end: bool = False, field: str = "time") -> str | None:
    """Parse a user-supplied date/datetime into the dataset's UTC string format.

    Accepts ``2026-01-05``, ``2026-01-05T12:00``, ``2026-01-05 12:00:00Z``,
    ``2026-01-05T12:00:00+02:00`` and ``2026-01`` (month). Naive values are UTC.
    With ``end=True`` a date-only value means "through the end of that day"
    (so intervals are half-open: [since, until)).
    """
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    date_only = len(raw) <= 10 and "T" not in raw and ":" not in raw
    candidate = raw.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        if re.fullmatch(r"\d{4}-\d{2}", candidate):
            dt = datetime.strptime(candidate, "%Y-%m")
            if end:
                dt = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            dt = datetime.fromisoformat(candidate)
            if date_only and end:
                dt = dt + timedelta(days=1)
    except ValueError:
        raise ToolInputError(
            f"Could not parse {field}={value!r}. Use ISO format, e.g. '2026-01-05' or '2026-01-05T14:30' (UTC)."
        ) from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime(TS_FORMAT)


def iso(ts: str | None) -> str | None:
    """Dataset timestamp -> compact ISO string (second precision, explicit Z)."""
    if not ts:
        return None
    return ts[:19].replace(" ", "T") + "Z"
