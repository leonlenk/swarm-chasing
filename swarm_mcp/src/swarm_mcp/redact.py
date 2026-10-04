"""Redaction engine for sharing swarm records. Pure Python, stdlib only.

    r = Redactor(allow_email_domains=["agentvillage.org"])
    clean, counts = r.redact("mail bob@example.com, key AKIA...")
    # clean  -> "mail [email], key [credential]"
    # counts -> Counter({"email": 1, "credential": 1})

Every match is replaced by a typed placeholder, never by a hash or a partial
value, so redacted text leaks nothing about the original:

    [email]  [phone]  [credential]  [url-credential]  [ip]

Rules (``RULES``; name -> placeholder type). They run in this order, so a URL's
``user:pass@`` is gone before the email rule could misread ``pass@host``:

    url_credential  [url-credential]  scheme://user:pass@host -> scheme://[url-credential]@host
    private_key     [credential]      -----BEGIN ... PRIVATE KEY----- blocks
    provider_key    [credential]      well-known provider key prefixes (AWS, GitHub, Slack, ...)
    jwt             [credential]      eyJ<header>.<payload>.<signature>
    auth_header     [credential]      Bearer <token>, Authorization: Basic|Token <value>
    keyword_secret  [credential]      <...key|token|secret|password...> = <high-entropy value>
    email           [email]           user@domain.tld, minus allow-listed domains
    phone           [phone]           +<international> or North American 3-3-4 with separators
    ip              [ip]              private, loopback and link-local IPv4/IPv6 (off by default)

Groups: ``default`` (all but ``ip``), ``all``, ``credential`` (the six
credential rules). ``Redactor(rules=..., disable=...)`` takes rule names,
group names or custom ``Rule`` objects, so each rule can be toggled.

Design goals, in order: (1) never leak a value we matched, (2) be idempotent:
placeholders contain ``[``/``]``, which no rule's value pattern accepts, so
``redact(redact(x)) == redact(x)``, (3) linear-time patterns (no nested
quantifiers) with cheap substring pre-checks so megabytes of chat stay fast,
(4) precision over recall where a false positive would destroy research
content (version numbers, dates, hex ids, counts). Each rule's docstring
lists its known false positives and negatives.

``mask_text(text, allow_email_domains)`` is the one-call entry point for
record-level masking elsewhere in the package.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

__all__ = [
    "ALL_RULES",
    "DEFAULT_RULES",
    "GROUPS",
    "PLACEHOLDER_TYPES",
    "RULES",
    "Redactor",
    "Rule",
    "mask_text",
]

PLACEHOLDER_TYPES = ("email", "phone", "credential", "url-credential", "ip")

# A replacer gets the match and the active Redactor and returns the replacement for
# the whole match, or None to leave the match untouched (a rejected candidate).
Replacer = Callable[[re.Match[str], "Redactor"], "str | None"]


@dataclass(frozen=True)
class Rule:
    """One redaction rule.

    ``pattern`` finds candidates; ``replace`` validates a candidate and returns its
    replacement (or None to keep it). ``needles`` are lowercase substrings at least
    one of which must occur in the lowercased text for the rule to run at all (a
    cheap pre-filter; empty means always run). ``type`` is the placeholder type.
    """

    name: str
    type: str
    pattern: re.Pattern[str]
    replace: Replacer
    needles: tuple[str, ...] = ()
    default: bool = True
    doc: str = ""

    @property
    def placeholder(self) -> str:
        return f"[{self.type}]"


# --------------------------------------------------------------------------- helpers


def _entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    n = len(s)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _classes(s: str) -> int:
    """How many of lower / upper / digit / symbol occur in ``s``."""
    lower = upper = digit = other = False
    for ch in s:
        if ch.islower():
            lower = True
        elif ch.isupper():
            upper = True
        elif ch.isdigit():
            digit = True
        else:
            other = True
    return lower + upper + digit + other


_has_digit = re.compile(r"[0-9]").search
_has_alpha = re.compile(r"[A-Za-z]").search

_PLACEHOLDER_WORDS = frozenset(
    {
        "null", "none", "nil", "true", "false", "undefined", "redacted", "removed", "hidden",
        "changeme", "example", "placeholder", "required", "optional", "string", "secret", "password",
        "your_api_key", "your-api-key", "api_key", "token",
    }
)  # fmt: skip
_DATE_OR_NUMBER = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO date / datetime
    r"|[0-9][0-9.,:_/+-]*"  # numbers, versions, times, ranges
)


_OUR_PLACEHOLDER = re.compile(r"\[(?:" + "|".join(re.escape(t) for t in PLACEHOLDER_TYPES) + r")\]")


def _is_placeholder_value(v: str) -> bool:
    low = v.lower().strip(".")
    return (
        low in _PLACEHOLDER_WORDS
        or _OUR_PLACEHOLDER.search(v) is not None
        or len(set(low)) <= 2  # xxxxxxxx, ********, abababab
        or low.startswith(("your", "my_", "my-", "example", "dummy", "fake", "test_", "sample"))
    )


def _looks_token(v: str, min_len: int = 16) -> bool:
    """High-entropy, token-shaped: long, letters *and* digits (or 3+ char classes), varied.

    Thresholds: random base62/hex of length >= 16 scores >= ~3.3 bits/char; English
    words with a number suffix ("authentication2") rarely exceed 3.2 at that length.
    """
    if len(v) < min_len or _is_placeholder_value(v) or _DATE_OR_NUMBER.fullmatch(v):
        return False
    if not ((_has_digit(v) and _has_alpha(v)) or (_classes(v) >= 3 and len(v) >= 20)):
        return False
    return _entropy(v) >= 3.0


# --------------------------------------------------------------------------- url_credential

_URL_CRED = re.compile(
    # scheme://userinfo@  ; userinfo may itself contain an unencoded '@' (greedy to the last one
    # before the host), but never whitespace, '/', '?', '#', brackets or quotes.
    r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{1,30}://)([^\s/?#\[\]<>\"'`@]+(?:@[^\s/?#\[\]<>\"'`@]+)*)@(?=[A-Za-z0-9\[])"
)


def _r_url_credential(m: re.Match[str], r: Redactor) -> str | None:
    """``scheme://user:pass@host`` -> ``scheme://[url-credential]@host``. Host, port, path stay.

    The whole userinfo is masked (usernames are often identities or tokens too).
    Userinfo *without* a colon is masked only when it looks like a token
    (``https://<token>@github.com``); ``ssh://git@github.com`` is kept.
    False negatives: credentials passed as query parameters (``?token=...``) are left
    to ``keyword_secret``; schemeless ``user:pass@host`` is not matched.
    """
    userinfo = m.group(2)
    if ":" in userinfo or _looks_token(userinfo, 16):
        return f"{m.group(1)}[url-credential]@"
    return None


# --------------------------------------------------------------------------- private_key

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
    r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"  # a complete block
    r"|(?:[ \t]*\r?\n?[ \t]*[A-Za-z0-9+/=:,-]{8,})*)"  # or a truncated one: eat the base64 lines
)


def _r_private_key(m: re.Match[str], r: Redactor) -> str | None:
    """PEM / OpenSSH / PGP private key blocks -> ``[credential]``, header to footer.

    A block cut off by truncation (no END line) is masked through its last base64
    line. Public keys and certificates are not matched (they are not secrets).
    """
    return "[credential]"


# --------------------------------------------------------------------------- provider_key

# Patterns only; no real or realistic key literal appears anywhere in this file.
_PROVIDER_ALTS = (
    r"(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA|APKA)[A-Z0-9]{16}",  # AWS key ids
    r"gh[pousr]_[A-Za-z0-9]{36,251}",  # GitHub classic tokens
    r"github_pat_[A-Za-z0-9_]{22,251}",  # GitHub fine-grained PATs
    r"glpat-[A-Za-z0-9_-]{20,}",  # GitLab PATs
    r"xox[abposr]-[A-Za-z0-9-]{10,}",  # Slack tokens
    r"xapp-[0-9]-[A-Za-z0-9-]{10,}",  # Slack app tokens
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}",  # Stripe secret / restricted keys
    r"sk-[A-Za-z0-9_-]{20,}",  # OpenAI / Anthropic style (sk-..., sk-proj-..., sk-ant-...)
    r"AIza[0-9A-Za-z_-]{35}",  # Google API keys
    r"hf_[A-Za-z0-9]{30,}",  # Hugging Face
    r"npm_[A-Za-z0-9]{36}",  # npm
    r"pypi-[A-Za-z0-9_-]{50,}",  # PyPI upload tokens
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",  # SendGrid
    r"(?:AC|SK)[0-9a-f]{32}",  # Twilio account / API key sids
)
_PROVIDER = re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(_PROVIDER_ALTS) + r")(?![A-Za-z0-9_-])")
_FIXED_FORMAT_PREFIXES = ("A3T", "AKIA", "ASIA", "ABIA", "ACCA", "AGPA", "AIDA", "AIPA", "ANPA", "ANVA", "AROA", "APKA")


def _r_provider_key(m: re.Match[str], r: Redactor) -> str | None:
    """Known provider key formats -> ``[credential]``.

    Every format except AWS key ids must also contain a digit, which rejects prose
    such as ``sk-learn-compatible-estimators`` (real keys of these lengths contain
    a digit with probability > 99.8%). False negatives: providers not listed, and
    AWS *secret* keys (40 chars of base64 with no prefix) unless a keyword is
    next to them (see ``keyword_secret``). False positives: 34-char ``AC``/``SK``
    + 32 lowercase hex strings that are not Twilio sids.
    """
    s = m.group(0)
    if s.startswith(_FIXED_FORMAT_PREFIXES) or _has_digit(s):
        return "[credential]"
    return None


# --------------------------------------------------------------------------- jwt

_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*(?![A-Za-z0-9_-])")


def _r_jwt(m: re.Match[str], r: Redactor) -> str | None:
    """JWT-shaped strings (``eyJ`` = base64url of ``{"``, then two more dot-separated
    base64url segments) -> ``[credential]``. Unsigned tokens (empty signature) are
    included. False negatives: JWEs and tokens whose header JSON starts with
    whitespace. False positives: practically none.
    """
    return "[credential]"


# --------------------------------------------------------------------------- auth_header

_AUTH = re.compile(
    r"(?i)(\bbearer[ \t]+|\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:(?:bearer|basic|token|digest)[ \t]+)?)"
    r"([A-Za-z0-9\-._~+/]+=*)"
)


def _r_auth_header(m: re.Match[str], r: Redactor) -> str | None:
    """``Bearer <token>`` anywhere, and ``Authorization: [Basic|Token|Bearer] <value>``
    -> the scheme is kept, the value becomes ``[credential]``.

    After a bare ``Bearer`` the value must look like a token (>= 12 chars, letters
    and digits), so prose ("Bearer tokens", "bearer authentication") is kept. After
    an explicit ``Authorization:`` any 8+ char value that is not a placeholder is
    masked (Basic credentials are base64 and may lack digits). Env-var references
    (``$TOKEN``, ``${KEY}``, ``<token>``) never match.
    """
    prefix, value = m.group(1), m.group(2)
    if prefix.lower().startswith("authorization"):
        ok = len(value) >= 8 and not _is_placeholder_value(value) and not _DATE_OR_NUMBER.fullmatch(value)
    else:
        ok = (
            len(value) >= 12
            and bool(_has_digit(value))
            and bool(_has_alpha(value))
            and not _is_placeholder_value(value)
        )
    return f"{prefix}[credential]" if ok else None


# --------------------------------------------------------------------------- keyword_secret

_SECRET_WORDS = ("password", "passwd", "pwd", "passphrase", "secret", "token", "credentials", "credential", "key")
_KEYWORD = re.compile(
    # Anchored on the keyword itself (fast); the full identifier it ends is recovered by
    # scanning back in the replacer, so "monkey" / "max_tokens" can be rejected there.
    r"(?i)(?P<word>" + "|".join(_SECRET_WORDS) + r")(?![A-Za-z0-9])"
    r"(?P<sep>[\"']?[ \t]{0,3}(?::=|=>|[:=])[ \t]{0,3}[\"']?|[ \t]+(?:is|was)[ \t]+[\"']?)"
    r"(?P<val>[^\s\"'`,;&<>\[\](){}]{6,})"
)
_IDENT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_PASSWORD_PARTS = frozenset({"password", "passwd", "pwd", "passphrase"})
_SECRET_PARTS = frozenset(
    {"secret", "token", "credential", "credentials", "apikey", "secretkey", "privatekey", "accesskey"}
)
_KEY_QUALIFIERS = frozenset(
    {
        "api", "access", "secret", "private", "signing", "encryption", "master", "client", "license",
        "auth", "account", "service", "app", "admin", "session", "webhook", "ssh", "gpg", "pgp", "subscription",
    }
)  # fmt: skip
_CAMEL_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


def _key_kind(key: str) -> str | None:
    """'password' / 'secret' if the identifier names a credential, else None.

    The identifier is split on ``_ . -`` and camelCase; its *last* part decides:
    ``db_password``, ``clientSecret``, ``X-Auth-Token``, ``AWS_SECRET_ACCESS_KEY``
    qualify; ``monkey``, ``max_tokens``, ``sort_key``, ``keyboard`` do not. A bare
    ``key`` part needs a qualifier before it (``api``, ``secret``, ``private``...).
    """
    parts = [p.lower() for p in _CAMEL_PARTS.findall(key)]
    if not parts:
        return None
    last = parts[-1]
    if last in _PASSWORD_PARTS:
        return "password"
    if last in _SECRET_PARTS:
        return "secret"
    if last == "key" and len(parts) >= 2 and parts[-2] in _KEY_QUALIFIERS:
        return "secret"
    return None


def _secret_value_ok(kind: str, value: str) -> bool:
    if kind == "password":
        # Passwords are often short and low-entropy, so accept any 6+ char value that has a
        # digit or symbol, or is long; reject placeholders, plain words ("required"), paths
        # (``pwd: /home/x``) and numbers/dates.
        if _is_placeholder_value(value) or _DATE_OR_NUMBER.fullmatch(value) or value.startswith(("/", "~", "./", "$")):
            return False
        return len(value) >= 6 and (bool(_has_digit(value)) or _classes(value) >= 3 or len(value) >= 12)
    return _looks_token(value, 16)


def _r_keyword_secret(m: re.Match[str], r: Redactor) -> str | None:
    """``<identifier naming a secret> <: = := => is> <value>`` -> value becomes ``[credential]``.

    Covers env files, YAML/JSON, HTTP headers and prose: ``api_key=...``,
    ``"client_secret": "..."``, ``X-Auth-Token: ...``, ``the token is ...``.
    Values next to token/secret/credential/<qualified>-key names must be >= 16 chars,
    contain letters and digits (or 3+ char classes at >= 20 chars) and have >= 3.0
    bits/char of entropy. Values next to password names only need 6+ chars with a
    digit or symbol (or 12+ chars), since passwords are often weak.
    False negatives: secrets far from any keyword, short or low-entropy tokens,
    keyword-less CLI flags (``-p hunter2``), ``--password hunter2`` (space only).
    False positives: high-entropy ids under secret-ish names (``session_token: <uuid>``
    is masked, deliberately).
    """
    text, start = m.string, m.start()
    i = start
    while i > 0 and start - i < 64 and text[i - 1] in _IDENT_CHARS:
        i -= 1
    kind = _key_kind(text[i : m.end("word")])
    if kind is None or not _secret_value_ok(kind, m.group("val")):
        return None
    return f"{m.group('word')}{m.group('sep')}[credential]"


# --------------------------------------------------------------------------- email

# Same shape as toolkit._EMAIL_RE, so record-level masking and exports agree.
_EMAIL = re.compile(r"(?<![\w.+%-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")


def _r_email(m: re.Match[str], r: Redactor) -> str | None:
    """``local@domain.tld`` -> ``[email]``, unless the domain (or a parent) is allow-listed.

    The allowlist matches whole labels: ``agentvillage.org`` allows
    ``help@agentvillage.org`` and ``x@mail.agentvillage.org``, but not
    ``x@notagentvillage.org`` or ``x@agentvillage.org.evil.com``.
    False negatives: obfuscated addresses ("bob at example dot com"), addresses
    without a TLD (``root@localhost``). False positives: ``name@2x.png``-style
    asset names whose "TLD" is alphabetic (e.g. ``icon@retina.png``).
    VCS service users (``git@github.com:org/repo.git``, ``ssh://hg@host``) are kept:
    they are not people and repo remotes are common research evidence.
    """
    if r.email_allowed(m.group(1)):
        return None
    if m.group(0).split("@", 1)[0].lower() in _VCS_USERS:
        return None
    return "[email]"


_VCS_USERS = frozenset({"git", "hg", "svn"})


# --------------------------------------------------------------------------- phone

_PHONE = re.compile(
    r"(?<![\w/.:#=@+-])(?:"
    # international: '+', then 8-15 digits with at most two separators between digits
    r"\+[1-9](?:[ .()-]{0,2}[0-9]){7,14}"
    # North American 3-3-4: optional leading 1, separators required. Any digits are accepted
    # (not just valid NANP area codes/exchanges) to match toolkit.Scrubber, which masks
    # placeholder-style numbers such as 555-123-4567 too.
    r"|(?:1[ .-])?(?:\([0-9]{3}\)[ .-]?|[0-9]{3}[ .-])[0-9]{3}[ .-][0-9]{4}"
    r")(?![\w-]|\.[0-9])"
)


def _r_phone(m: re.Match[str], r: Redactor) -> str | None:
    """International numbers written with a leading ``+`` (8-15 digits, any common
    separators) and North American numbers in 3-3-4 form *with* separators ->
    ``[phone]``.

    Bare digit runs (``4155550134``) are never matched: in agent logs they are far
    more often ids, counts or timestamps. Dates (``2026-01-15``), versions
    (``1.234.5``), times and thousands (``21,596``) do not fit either shape.
    False negatives: unformatted national numbers, non-NANP national formats
    without ``+`` (``020 7946 0958``). False positives: separator-formatted 3-3-4
    numeric codes that are not phone numbers (``100-200-3000``, ``123 456 7890``).
    """
    return "[phone]"


# --------------------------------------------------------------------------- ip

_IP = re.compile(
    r"(?<![0-9A-Za-z.:])(?:"
    r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9A-Za-z]|\.[0-9])"
    r"|(?=[0-9A-Fa-f:]*::|(?:[0-9A-Fa-f]{1,4}:){7})(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])"
    r")"
)
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
        "::1/128", "fc00::/7", "fe80::/10",
    )
)  # fmt: skip


def _r_ip(m: re.Match[str], r: Redactor) -> str | None:
    """Private (RFC 1918, IPv6 ULA), loopback and link-local addresses -> ``[ip]``.

    Off by default: such addresses rarely identify anyone and four-part version
    strings collide with them (``10.2.3.4`` as a version is masked when enabled).
    Public addresses are never touched. Ports and CIDR suffixes are kept.
    """
    try:
        addr = ipaddress.ip_address(m.group(0))
    except ValueError:
        return None
    return "[ip]" if any(addr in net for net in _PRIVATE_NETS) else None


# --------------------------------------------------------------------------- registry

RULES: dict[str, Rule] = {
    rule.name: rule
    for rule in (
        Rule("url_credential", "url-credential", _URL_CRED, _r_url_credential, ("://",), True, _r_url_credential.__doc__ or ""),
        Rule("private_key", "credential", _PRIVATE_KEY, _r_private_key, ("private key",), True, _r_private_key.__doc__ or ""),
        Rule("provider_key", "credential", _PROVIDER, _r_provider_key, (), True, _r_provider_key.__doc__ or ""),
        Rule("jwt", "credential", _JWT, _r_jwt, ("eyj",), True, _r_jwt.__doc__ or ""),
        Rule("auth_header", "credential", _AUTH, _r_auth_header, ("bearer", "authorization"), True, _r_auth_header.__doc__ or ""),
        Rule("keyword_secret", "credential", _KEYWORD, _r_keyword_secret, ("pass", "pwd", "secret", "token", "credential", "key"), True, _r_keyword_secret.__doc__ or ""),
        Rule("email", "email", _EMAIL, _r_email, ("@",), True, _r_email.__doc__ or ""),
        Rule("phone", "phone", _PHONE, _r_phone, (), True, _r_phone.__doc__ or ""),
        Rule("ip", "ip", _IP, _r_ip, (), False, _r_ip.__doc__ or ""),
    )
}  # fmt: skip
ALL_RULES: tuple[str, ...] = tuple(RULES)
DEFAULT_RULES: tuple[str, ...] = tuple(n for n, rule in RULES.items() if rule.default)
GROUPS: dict[str, tuple[str, ...]] = {
    "default": DEFAULT_RULES,
    "all": ALL_RULES,
    "strict": ALL_RULES,
    "credential": ("url_credential", "private_key", "provider_key", "jwt", "auth_header", "keyword_secret"),
}


def _expand(spec: str | Rule | Iterable[str | Rule] | None) -> list[Rule]:
    if spec is None:
        spec = DEFAULT_RULES
    if isinstance(spec, (str, Rule)):
        spec = [spec]
    out: list[Rule] = []
    for item in spec:
        if isinstance(item, Rule):
            out.append(item)
            continue
        for name in [p.strip() for p in str(item).split(",") if p.strip()]:
            if name in GROUPS:
                out.extend(RULES[n] for n in GROUPS[name])
            elif name in RULES:
                out.append(RULES[name])
            else:
                known = ", ".join([*RULES, *GROUPS])
                raise ValueError(f"unknown redaction rule {name!r}; known rules and groups: {known}")
    return out


def _normalise_domains(domains: Iterable[str] | str | None) -> tuple[str, ...]:
    if domains is None:
        return ()
    if isinstance(domains, str):
        domains = domains.split(",")
    return tuple(sorted({d.strip().lower().lstrip("@.") for d in domains if d and d.strip().lstrip("@.")}))


# --------------------------------------------------------------------------- Redactor


@dataclass
class Redactor:
    """A configured set of rules.

    ``rules``: rule names, group names (``default``, ``all``, ``credential``), comma
    lists of those, or custom ``Rule`` objects. None = ``default``. ``disable``
    removes rules after expansion. Built-in rules always run in their canonical
    order (see the module docstring); custom rules run after them.
    ``allow_email_domains``: domains (and their subdomains) whose addresses are kept.
    """

    rules: Any = None
    allow_email_domains: Any = ()
    disable: Any = ()
    active: tuple[Rule, ...] = field(init=False)

    def __post_init__(self) -> None:
        chosen = _expand(self.rules)
        dropped = {r.name for r in _expand(self.disable)} if self.disable else set()
        builtin_order = {name: i for i, name in enumerate(RULES)}
        seen: set[str] = set()
        builtins, custom = [], []
        for rule in chosen:
            if rule.name in dropped or rule.name in seen:
                continue
            seen.add(rule.name)
            (builtins if RULES.get(rule.name) is rule else custom).append(rule)
        builtins.sort(key=lambda r: builtin_order[r.name])
        self.active = tuple(builtins + custom)
        self.allow_email_domains = _normalise_domains(self.allow_email_domains)
        self._has_keyword_rule = any(r.name == "keyword_secret" for r in self.active)

    # -- configuration -------------------------------------------------------

    @classmethod
    def strict(cls, allow_email_domains: Iterable[str] = ()) -> Redactor:
        """Every rule, including ``ip``. ``check()`` rescans exports with this."""
        return cls("all", allow_email_domains=allow_email_domains)

    @property
    def rule_names(self) -> list[str]:
        return [r.name for r in self.active]

    def config(self) -> dict[str, Any]:
        """What this redactor does, for manifests (no values, just the policy)."""
        return {"rules": self.rule_names, "allow_email_domains": list(self.allow_email_domains)}

    def describe(self) -> list[dict[str, str]]:
        return [{"name": r.name, "placeholder": r.placeholder, "doc": " ".join(r.doc.split())} for r in self.active]

    def email_allowed(self, domain: str) -> bool:
        d = domain.lower()
        return any(d == a or d.endswith("." + a) for a in self.allow_email_domains)

    # -- text ----------------------------------------------------------------

    def redact(self, text: str | None) -> tuple[str, Counter[str]]:
        """Return ``(redacted_text, Counter{placeholder_type: n})``. Never returns matched values."""
        counts: Counter[str] = Counter()
        if not text:
            return text or "", counts
        low = text.lower()
        for rule in self.active:
            if rule.needles and not any(n in low for n in rule.needles):
                continue
            n_before = sum(counts.values())
            text = rule.pattern.sub(self._sub(rule, counts), text)
            if sum(counts.values()) != n_before:
                low = text.lower()
        return text, counts

    def __call__(self, text: str | None) -> str:
        return self.redact(text)[0]

    def _sub(self, rule: Rule, counts: Counter[str]) -> Callable[[re.Match[str]], str]:
        def sub(m: re.Match[str]) -> str:
            out = rule.replace(m, self)
            if out is None:
                return m.group(0)
            counts[rule.type] += 1
            return out

        return sub

    # -- structured values ---------------------------------------------------

    def redact_field(self, key: str | None, value: str) -> tuple[str, Counter[str]]:
        """Redact one string field. If ``keyword_secret`` is active and ``key`` names a
        secret (``api_key``, ``password``...), a qualifying value is replaced whole."""
        if self._has_keyword_rule and key and value:
            kind = _key_kind(key)
            if (
                kind is not None
                and _secret_value_ok(kind, value.strip())
                and not any(c.isspace() for c in value.strip())
            ):
                return "[credential]", Counter({"credential": 1})
        return self.redact(value)

    def redact_value(self, value: Any, key: str | None = None) -> tuple[Any, Counter[str]]:
        """Redact every string inside a JSON-like value (str, dict, list; other types pass
        through). ``key`` is the field name the value sits under, if any."""
        counts: Counter[str] = Counter()

        def walk(v: Any, k: str | None) -> Any:
            if isinstance(v, str):
                out, c = self.redact_field(k, v)
                counts.update(c)
                return out
            if isinstance(v, dict):
                return {dk: walk(dv, str(dk)) for dk, dv in v.items()}
            if isinstance(v, (list, tuple)):
                return [walk(x, k) for x in v]
            return v

        return walk(value, key), counts

    def redact_obj(self, obj: Any, *, skip_keys: Iterable[str] = ()) -> tuple[Any, Counter[str]]:
        """``redact_value`` for a record: top-level keys in ``skip_keys`` are copied
        unchanged (identity fields such as ``event_id``)."""
        if not isinstance(obj, dict):
            return self.redact_value(obj)
        skip = frozenset(skip_keys)
        counts: Counter[str] = Counter()
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if k in skip:
                out[k] = v
                continue
            out[k], c = self.redact_value(v, str(k))
            counts.update(c)
        return out, counts

    def scan_obj(self, obj: Any) -> Iterator[tuple[str, Counter[str]]]:
        """Yield ``(json_path, Counter)`` for every string in ``obj`` that this
        redactor would change. Values are never yielded."""

        def walk(v: Any, path: str, key: str | None) -> Iterator[tuple[str, Counter[str]]]:
            if isinstance(v, str):
                _, c = self.redact_field(key, v)
                if c:
                    yield path or "$", c
            elif isinstance(v, dict):
                for k, val in v.items():
                    yield from walk(val, f"{path}.{k}" if path else str(k), str(k))
            elif isinstance(v, (list, tuple)):
                for i, x in enumerate(v):
                    yield from walk(x, f"{path}[{i}]", key)

        yield from walk(obj, "", None)


# --------------------------------------------------------------------------- one-call masking


@lru_cache(maxsize=32)
def _cached(domains: tuple[str, ...], rules: tuple[str, ...]) -> Redactor:
    return Redactor(list(rules), allow_email_domains=domains)


def mask_text(
    text: str | None,
    allow_email_domains: Iterable[str] | str | None = (),
    rules: Iterable[str] | str | None = None,
) -> str:
    """Mask ``text`` with the default rules (or ``rules``); None becomes ``""``.

    Drop-in for record-level masking: ``toolkit.Scrubber.__call__`` can return
    ``mask_text(text, self.allow)`` when enabled. Pass ``rules=("email", "phone")``
    to reproduce the old email+phone-only behaviour exactly; the default also
    masks credentials and credentialed URLs. Redactors are cached per config.
    """
    if not text:
        return ""
    names = ("default",) if rules is None else ((rules,) if isinstance(rules, str) else tuple(rules))
    return _cached(_normalise_domains(allow_email_domains), names).redact(text)[0]


def counts_dict(counts: Counter[str]) -> dict[str, int]:
    """A Counter as a plain, key-sorted dict (stable JSON)."""
    return {k: counts[k] for k in sorted(counts) if counts[k]}
