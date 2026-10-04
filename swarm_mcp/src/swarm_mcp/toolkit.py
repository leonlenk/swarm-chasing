"""Small helpers that make tools friendly to LLM callers.

Module authors mostly need:
- ``ToolInputError``: raise it with a clear, actionable message for bad input.
- ``clamp_limit``: apply default/max result limits and say when clamping happened.
- ``untrusted``: the ONLY way to return dataset text (agent/human content) to a
  caller: masked, capped (default 500 chars) and wrapped as
  ``{"content": ..., "untrusted": true}`` so it is never mistaken for instructions.
- ``safe_label``: dataset-supplied NAMES (agent, author, channel, actor) returned bare: one line, capped,
  masked and neutralized. ``wrap_tool`` applies it to every value under a ``NAME_KEYS`` key of a result.
- ``truncate`` / ``snippet``: cut long text (optionally around a match) and mark it.
- ``Scrubber``: mask emails, phone numbers and credentials in returned text (``redact.mask_text``).
- ``parse_time``: accept dates/datetimes in the formats an LLM is likely to send.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import anyio.to_thread

from swarm_mcp.fence import neutralize
from swarm_mcp.redact import mask_text
from swarm_mcp.sdk import ToolError


class ToolInputError(ValueError):
    """Bad tool input. The message is returned to the caller verbatim."""


def wrap_tool(
    fn: Callable[..., Any], tool_name: str, log: logging.Logger, scrub: Callable[[str], str] | None = None
) -> Callable[..., Any]:
    """Wrap a tool function so that:

    - sync functions run in a worker thread (slow loads don't block the event loop);
    - dataset-supplied names in the result (``NAME_KEYS``: author, channel, display_name, actor...)
      go out through ``safe_label`` (``sanitize_names``), masked with ``scrub``;
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
                out = await fn(*args, **kwargs)
            else:
                out = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
            return sanitize_names(out, scrub) if isinstance(out, (dict, list)) else out
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
# Every tool's ``max_chars`` parameter accepts the same range, [MIN_MAX_CHARS, HARD_MAX_CHARS]
# (pydantic ``Field(ge=MIN_MAX_CHARS, le=HARD_MAX_CHARS)``); values outside it are rejected.
MIN_MAX_CHARS = 20  # lower bound for an explicit max_chars
HARD_MAX_CHARS = 20000  # upper bound for an explicit max_chars

# Total size (JSON characters) of the items one tool call may return. A tool that returns many texts
# stops adding items once the next one would not fit, and says so with ResponseBudget.note().
RESPONSE_BUDGET_CHARS = 80_000


class ResponseBudget:
    """Running total of the JSON size of the items a tool returns, against ``RESPONSE_BUDGET_CHARS``.

    ``admit(item)`` counts the item and returns True while it fits; once one does not, the budget is
    exhausted and every later call returns False. The first item is always admitted, so paging
    (offset=next_offset) always makes progress."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = RESPONSE_BUDGET_CHARS if limit is None else limit
        self.used = 0
        self.exhausted = False

    def admit(self, item: Any) -> bool:
        if self.exhausted:
            return False
        n = len(json.dumps(item, ensure_ascii=False, default=str))
        if self.used and self.used + n > self.limit:
            self.exhausted = True
            return False
        self.used += n
        return True

    def note(self, hint: str) -> str:
        return f"truncated: response budget reached ({self.limit:,} chars), narrow your request; {hint}"


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


# --------------------------------------------------------------------------- names

NAME_MAX_CHARS = 80  # cap for a dataset-supplied name (agent, channel, actor, artifact) in tool output
_INVISIBLE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f​‎‏‪-‮⁦-⁩﻿]")
_FENCE_RUN = re.compile(r"`{3,}|~{3,}")
_MD_LEAD = re.compile(r"^(?:#{1,6}(?=\s|$)|>|[-*+=_]{3,}(?=\s|$))\s*")
# an evidence/agent id ('village:agent:<uuid>', 'rpg:artifact:src/x.js'): passed back for lookups, never altered
_ID_LIKE = re.compile(r"[A-Za-z0-9_.-]+:[a-z_]+:[^\s<>`]+")


def safe_label(value: object, scrub: Callable[[str], str] | None = None, max_chars: int = NAME_MAX_CHARS) -> str:
    """A dataset-supplied name or identifier (agent display name, channel, actor, artifact name) made safe
    to return bare: one line (whitespace runs, newlines and invisible characters collapsed), PII masked
    (``scrub``, default ``redact.mask_text``), tag-like text neutralized (``fence.neutralize``), code-fence
    runs and markdown heading/quote starters removed, capped at ``max_chars``. Benign names (``Claude Opus
    4.5``, ``general``, ``#general``) come back unchanged, and applying it twice changes nothing. Strings
    shaped like evidence ids are returned as they are so they still resolve."""
    s = "" if value is None else str(value)
    if _ID_LIKE.fullmatch(s):
        return s
    s = " ".join(_INVISIBLE.sub("", s).split())
    s = scrub(s) if scrub is not None else mask_text(s)
    s = _FENCE_RUN.sub(lambda m: m.group()[0], neutralize(s))
    while (m := _MD_LEAD.match(s)) and m.end():
        s = s[m.end() :]
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


def label_matches(raw: object, query: str | None) -> bool:
    """Does ``query`` equal ``raw`` as ``safe_label`` returns it (masked or not)? Lets a resolver accept a
    name exactly as a tool returned it, even when sanitizing changed it."""
    if not query or raw is None:
        return False
    q = query.strip()
    return q in (safe_label(raw), safe_label(raw, scrub=str))


# Output keys whose bare string values (or the strings in their lists) are dataset-supplied names.
# ``wrap_tool`` passes them through ``safe_label`` on the way out, whichever module built the result.
NAME_KEYS = frozenset(
    {
        "author", "agent", "actor", "from_actor", "to_actor", "actors", "display_name", "name", "names",
        "agents", "humans", "channel", "channels", "group", "group_id", "source_name", "target_name", "from", "to",
        "coined_by",
    }
)  # fmt: skip


def sanitize_names(obj: Any, scrub: Callable[[str], str] | None = None, *, _key: str | None = None) -> Any:
    """``obj`` with every string under a ``NAME_KEYS`` key replaced by ``safe_label`` (lists included;
    ``{"content", "untrusted"}`` wrappers are left alone). Builds new containers; ``obj`` is not modified."""
    if isinstance(obj, dict):
        if obj.get("untrusted") is True and "content" in obj:
            return obj
        return {k: sanitize_names(v, scrub, _key=k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_names(v, scrub, _key=_key) for v in obj]
    if isinstance(obj, str) and _key in NAME_KEYS:
        return safe_label(obj, scrub)
    return obj


def iso(ts: str | None) -> str | None:
    """Dataset timestamp -> compact ISO string (second precision, explicit Z)."""
    if not ts:
        return None
    return ts[:19].replace(" ", "T") + "Z"
