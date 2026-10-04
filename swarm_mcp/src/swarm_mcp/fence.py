"""Fence untrusted text inside LLM prompts and generated markdown so it can't close its own fence.

Prompt blocks, ``wrap("record", text, nonce)``::

    <record-<nonce> untrusted="true">
    ...text, neutralized...
    </record-<nonce>>

Two layers keep data inside its block:

- ``new_nonce()`` is a fresh random token for every request, so data can't contain the real
  closing tag. The system prompt says a block ends only at the closing tag with the same token.
- ``neutralize`` rewrites, as ``&lt;``, every ``<``-like character in the data that starts
  something that reads as one of our tags (``TAG_NAMES``). That covers any letter case,
  whitespace or invisible characters before or after the slash, backslash and look-alike
  slashes, fullwidth and look-alike brackets and letters, partial tags with no ``>``, and
  nested opening tags.

Markdown: ``md_fence(text)`` puts text in a backtick fence longer than any backtick run inside
it, and ``safe_name`` renders a dataset-supplied name (field, table, path) on one line with no
backticks, so it can't start a heading or close a fence.
"""

from __future__ import annotations

import json
import re
import secrets
import unicodedata

TAG_NAMES: tuple[str, ...] = ("record", "rubric", "data")

# '<' and characters a reader could take for it
_LT_LIKE = frozenset("<˂ᐸ‹〈❬❮⟨〈《﹤＜")
# slashes (after NFKC, which maps the fullwidth one to '/') allowed between '<' and the name
_SLASHES = frozenset("/\\⁄∕╱⧸")
# invisible or combining characters skipped while reading a tag name
_SKIP_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me", "Zs", "Zl", "Zp"})
# look-alike letters for the letters of TAG_NAMES that NFKC leaves alone (Cyrillic, Greek, small caps)
_CONFUSABLE = {
    "а": "a", "е": "e", "о": "o", "с": "c", "і": "i", "ԁ": "d", "օ": "o",
    "α": "a", "ι": "i", "ο": "o", "υ": "u", "ս": "u", "ı": "i", "ɑ": "a",
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɪ": "i", "ᴏ": "o",
    "ʀ": "r", "ᴛ": "t", "ᴜ": "u",
}  # fmt: skip


def new_nonce() -> str:
    """A fresh random token for one request's delimiter tags."""
    return secrets.token_hex(8)


def _letters(raw: str) -> str:
    out = []
    for ch in unicodedata.normalize("NFKC", raw).casefold():
        if ch.isspace() or unicodedata.category(ch) in _SKIP_CATEGORIES:
            continue
        out.append(_CONFUSABLE.get(ch, ch))
    return "".join(out)


def _reads_as_tag(text: str, start: int, names: tuple[str, ...]) -> bool:
    """Does ``text[start:]`` (just after a '<'-like char) read as ``[/]name`` for one of ``names``?"""
    longest = max(map(len, names))
    got = ""
    j = start
    while j < len(text) and len(got) < longest:
        part = _letters(text[j])
        j += 1
        if not got:
            part = part.lstrip("".join(_SLASHES))
        if not part:
            continue
        got += part
        if not any(n.startswith(got) or got.startswith(n) for n in names):
            return False
    return any(got.startswith(n) for n in names)


def neutralize(text: object, names: tuple[str, ...] = TAG_NAMES) -> str:
    """``text`` with every '<'-like char that opens one of our tags (any variant) replaced by ``&lt;``."""
    s = "" if text is None else str(text)
    if not any(c in _LT_LIKE for c in s):
        return s
    return "".join("&lt;" if c in _LT_LIKE and _reads_as_tag(s, i + 1, names) else c for i, c in enumerate(s))


def wrap(name: str, text: object, nonce: str, attrs: str = 'untrusted="true"') -> str:
    """``text`` as a ``<name-nonce attrs>`` block that only ``</name-nonce>`` closes."""
    opening = f"<{name}-{nonce}" + (f" {attrs}" if attrs else "") + ">"
    return f"{opening}\n{neutralize(text)}\n</{name}-{nonce}>"


def md_fence(text: str, info: str = "") -> str:
    """``text`` in a backtick code fence that no backtick run inside it can close."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    bar = "`" * max(3, longest + 1)
    return f"{bar}{info}\n{text}\n{bar}"


_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f-\x9f`  ]")
_JSON_UNSAFE = re.compile(r"[\x7f-\x9f`  ]")


def safe_name(value: object) -> str:
    """A dataset-supplied name for markdown or plain text: unchanged if it is one line with no
    backticks or control characters, else a JSON string literal with those characters escaped."""
    s = str(value)
    if not _UNSAFE_NAME.search(s):
        return s
    return _JSON_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(s, ensure_ascii=False))
