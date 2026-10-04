"""Small helpers that make tools friendly to LLM callers.

Module authors mostly need:
- ``ToolInputError``: raise it with a clear, actionable message for bad input.
- ``clamp_limit``: apply default/max result limits and say when clamping happened.
- ``untrusted``: the ONLY way to return dataset text (agent/human content) to a
  caller: masked, capped (default 500 chars) and wrapped as
  ``{"content": ..., "untrusted": true}`` so it is never mistaken for instructions.
- ``truncate`` / ``snippet``: cut long text (optionally around a match) and mark it.
- ``Scrubber``: mask emails, phone numbers and credentials in returned text (``redact.mask_text``).
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

from swarm_mcp.redact import mask_text
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


DEFAULT_MAX_CHARS = 500  # default cap for any dataset text returned to a caller
HARD_MAX_CHARS = 20000  # upper bound for an explicit max_chars


def truncate(text: str | None, max_chars: int) -> tuple[str, bool]:
    """Return (text, was_truncated). Truncated text ends with an explicit marker."""
    if text is None:
        return "", False
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rstrip()
    return f"{cut} …[truncated, {len(text) - len(cut)} more chars]", True


def snippet(text: str | None, max_chars: int, focus: re.Pattern[str] | None = None) -> tuple[str, bool]:
    """Return (snippet, was_cut): at most ~``max_chars`` of ``text``.

    Without ``focus`` (or when it does not match) this is ``truncate``. With a
    matching ``focus`` pattern the window is centred on the first match and
    elided ends are marked with "…".
    """
    if text is None:
        return "", False
    if len(text) <= max_chars:
        return text, False
    m = focus.search(text) if focus is not None else None
    if not m or m.end() <= max_chars - 20:
        return truncate(text, max_chars)
    half = max(0, (max_chars - (m.end() - m.start())) // 2)
    a = max(0, m.start() - half)
    b = min(len(text), a + max_chars)
    a = max(0, b - max_chars)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else ""), True


def untrusted(
    text: str | None,
    scrub: "Scrubber | None" = None,
    max_chars: int | None = None,
    focus: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    """Wrap dataset text for return to an MCP caller.

    Text is masked (``scrub``), capped at ``max_chars`` (default 500; clamped to
    [1, 20000]) and returned as ``{"content": ..., "untrusted": True}``, plus
    ``truncated``/``total_chars`` when it was cut. Agent output is data, not
    instructions; this delimiting keeps that explicit for the client model.
    """
    cap = DEFAULT_MAX_CHARS if max_chars is None else max(1, min(int(max_chars), HARD_MAX_CHARS))
    raw = text or ""
    clean = scrub(raw) if scrub is not None else raw
    cut_text, cut = snippet(clean, cap, focus)
    out: dict[str, Any] = {"content": cut_text, "untrusted": True}
    if cut:
        out["truncated"] = True
        out["total_chars"] = len(clean)
    return out


# --------------------------------------------------------------------------- privacy

class Scrubber:
    """Masks dataset text with the shared redaction engine (``redact.mask_text``, default rules):
    emails (except allow-listed domains) -> ``[email]``, phone numbers -> ``[phone]``, credentials
    (provider keys, JWTs, auth headers, key=secret pairs, private keys) -> ``[credential]`` and
    ``user:pass@`` in URLs -> ``[url-credential]``. VCS remotes such as ``git@github.com:org/repo``
    are kept. Disabled scrubbers pass text through unchanged."""

    def __init__(self, enabled: bool = True, email_allowlist: Iterable[str] = ("agentvillage.org",)):
        self.enabled = enabled
        self.allow = tuple(d.lower().lstrip("@.") for d in email_allowlist)

    def __call__(self, text: str | None) -> str:
        if not text or not self.enabled:
            return text or ""
        return mask_text(text, self.allow)


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
