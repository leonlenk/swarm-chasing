"""Redaction engine: each rule's true positives and tricky negatives, allowlist,
toggles, idempotence and speed.

Every fake secret is assembled at runtime from pieces, so no source line contains
anything a secret scanner would take for a real provider token.
"""

from __future__ import annotations

import base64
import json
import random
import string
import time

import pytest

from swarm_mcp.redact import ALL_RULES, DEFAULT_RULES, RULES, Redactor, mask_text


def b64url(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def rand(alphabet: str, n: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


ALNUM = string.ascii_letters + string.digits
HEX = "0123456789abcdef"

# fake secrets, built from pieces
AWS_ID = "AK" + "IA" + "X" * 16
GITHUB = "gh" + "p_" + rand(ALNUM, 36, 1) + "7"
GITHUB_PAT = "github" + "_pat_" + rand(ALNUM, 30, 2) + "1"
SLACK = "xo" + "xb-" + "1234567890-" + rand(ALNUM, 24, 3)
OPENAI_LIKE = "s" + "k-" + "proj-" + rand(ALNUM, 32, 4) + "9"
STRIPE_LIKE = "sk" + "_live_" + rand(ALNUM, 24, 5) + "2"
GOOGLE_LIKE = "AI" + "za" + rand(ALNUM, 34, 6) + "3"
JWT = b64url({"alg": "HS256", "typ": "JWT"}) + "." + b64url({"sub": "1234", "iat": 1}) + "." + rand(ALNUM + "-_", 43, 7)
OPAQUE = rand(ALNUM, 30, 8) + "4"
HEXKEY = rand(HEX, 40, 9)
PK_HEAD = "-----BEGIN RSA " + "PRIV" + "ATE KEY-----"
PK_TAIL = "-----END RSA " + "PRIV" + "ATE KEY-----"
PEM = PK_HEAD + "\n" + "\n".join(rand(ALNUM + "+/", 64, i) for i in range(3)) + "\n" + PK_TAIL


@pytest.fixture
def r() -> Redactor:
    return Redactor()


def red(r: Redactor, text: str) -> str:
    return r.redact(text)[0]


# --------------------------------------------------------------------------- email


def test_email_masked_with_counts(r):
    out, c = r.redact("mail bob.smith@gmail.com and a+tag@sub.example.co.uk now")
    assert out == "mail [email] and [email] now"
    assert c == {"email": 2}


@pytest.mark.parametrize("text", ["root@localhost", "@handle", "foo@bar", "ping me @bob.", "the @ sign"])
def test_email_negatives(r, text):
    assert red(r, text) == text


def test_email_allowlist_matches_whole_labels():
    r = Redactor(allow_email_domains=["AgentVillage.org", "@example.net"])
    text = (
        "help@agentvillage.org x@mail.agentvillage.org y@notagentvillage.org z@agentvillage.org.evil.com w@example.net"
    )
    out, c = r.redact(text)
    assert out == "help@agentvillage.org x@mail.agentvillage.org [email] [email] w@example.net"
    assert c == {"email": 2}
    assert Redactor().redact("help@agentvillage.org")[0] == "[email]"  # no allowlist by default


@pytest.mark.parametrize(
    "text, expected",
    [
        ("連絡はbob@example.comまで", "連絡は[email]まで"),
        ("jöhn@example.com", "[email]"),
        ("émail bob@example.comé", "émail [email]é"),
        ("иван@example.com", "[email]"),
        ("mail: josé.garcía@example.org!", "mail: [email]!"),
        ("메일bob@example.com입니다", "메일[email]입니다"),
    ],
)
def test_email_next_to_non_ascii_letters(r, text, expected):
    """Regression: the boundaries used Unicode \\w, so an address touching a non-ASCII letter was kept."""
    assert red(r, text) == expected


def test_vcs_remotes_are_not_emails(r):
    text = "clone git@github.com:org/repo.git or ssh://git@github.com/org/repo"
    assert red(r, text) == text


def test_dict_keys_are_redacted_without_collisions(r):
    carol, dan = "carol" + "@" + "example.com", "dan" + "@" + "example.com"
    out, c = r.redact_value({"meta": {carol: "reacted", dan: "liked", "count": 2}, 3: "x"})
    assert out == {"meta": {"[email]": "reacted", "[email] (2)": "liked", "count": 2}, 3: "x"}
    assert c == {"email": 2}
    out, c = r.redact_obj({"event_id": "keep", carol: 1}, skip_keys=["event_id"])
    assert out == {"event_id": "keep", "[email]": 1} and c == {"email": 1}


# --------------------------------------------------------------------------- phone


@pytest.mark.parametrize(
    "number",
    ["+1 415 555 0134", "(415) 555-0199", "415-555-0134", "415.555.0134", "415 555 0134", "1-800-555-0199",
     "+44 20 7946 0958", "+4915112345678", "+33 1 23 45 67 89", "+1 (415) 555-0134", "555-123-4567"],
)  # fmt: skip
def test_phone_positives(r, number):
    out, c = r.redact(f"call {number} today")
    assert out == "call [phone] today" and c == {"phone": 1}


@pytest.mark.parametrize(
    "text",
    ["version 1.234.5 shipped", "on 2026-01-15 at 13:45:10", "21,596 stations", "id 4155550134",
     "build 10.0.19041.1", "ISBN 978-3-16-148410-0", "+5 points", "score 1+2345678901",
     "2026-01-15T13:00:00Z", "ratio 3.14159265"],
)  # fmt: skip
def test_phone_negatives(r, text):
    assert red(r, text) == text


@pytest.mark.parametrize(
    "text, expected",
    [("電話+81 90 1234 5678です", "電話[phone]です"), ("電話555-867-5309です", "電話[phone]です"),
     ("Tél. +33 1 23 45 67 89é", "Tél. [phone]é"), ("teléfono 415-555-0134ñ", "teléfono [phone]ñ")],
)  # fmt: skip
def test_phone_next_to_non_ascii_letters(r, text, expected):
    assert red(r, text) == expected


def test_two_phones_in_a_row(r):
    assert red(r, "+1 415 555 0134 / (415) 555-0199.") == "[phone] / [phone]."


# Same cases as village_tools/swarmtrace/tests/test_format.py (the phone patterns are twins).
@pytest.mark.parametrize(
    "raw, want",
    [  # a letter, "_" or "-word" right after the number: the whole number goes, no digits left behind
     ("+44 20 7946 0958x", "[phone]x"), ("+44 20 7946 0958café", "[phone]café"), ("+44 20 7946 0958_", "[phone]_"),
     ("+44 20 7946 0958-ish", "[phone]-ish"), ("415-555-0134x", "[phone]x"), ("1-415-555-0134-ish", "[phone]-ish"),
     ("+33 6 12 34 56 78x", "[phone]x"),
     # formats that used to slip through
     ("1-415-555-0134", "[phone]"), ("1.415.555.0134", "[phone]"), ("+33 6 12 34 56 78", "[phone]"),
     ("+81-3-1234-5678", "[phone]"), ("+4915112345678", "[phone]"), ("+44 (0)20 7946 0958", "[phone]"),
     ("1 (415) 555-0134", "[phone]"), ("tel:+14155550134", "tel:[phone]"), ("phone=415-555-0134", "phone=[phone]"),
     ("+33 6 12 34 56 78", "[phone]"), ("415 555 0134", "[phone]")],
)  # fmt: skip
def test_phone_takes_the_whole_number(r, raw, want):
    out, c = r.redact(f"call {raw} now")
    assert out == f"call {want} now" and c == {"phone": 1}


@pytest.mark.parametrize(
    "text",
    ["2026-10-04", "10/04/2026", "2026.10.04", "12:34:56", "12.30", "1.2.3", "v3.10.12", "2.0.0-rc1",
     "550e8400-e29b-41d4-a716-446655440000", "deadbeefcafe1234", "4155550134", "14155550134", "1,234,567.89",
     "75.126.1.1", "192.168.1.10", "10.0.19041.1", "ISBN 978-3-16-148410-0", "score 1+2345678901", "+3.14159265",
     "+100.000000", "2026-10-04T12:34:56+05:30", "123-456-78901", "1234-567-8901", "ev_123-456-7890", "#123-456-7890",
     "415-555-0134.5", "123-456-7890ab1", "4111 1111 1111 1111", "+1 2345 6789 0123 4567", "415\n555\n0134"],
)  # fmt: skip
def test_phone_leaves_non_phones(r, text):
    assert r.redact(text) == (text, {})


# --------------------------------------------------------------------------- credentials


@pytest.mark.parametrize(
    "secret", [AWS_ID, GITHUB, GITHUB_PAT, SLACK, OPENAI_LIKE, STRIPE_LIKE, GOOGLE_LIKE, JWT],
    ids=["aws", "github", "github_pat", "slack", "sk-", "stripe", "google", "jwt"],
)  # fmt: skip
def test_provider_keys_and_jwt(r, secret):
    out, c = r.redact(f"use {secret}, then stop")
    assert out == "use [credential], then stop" and c == {"credential": 1}
    assert secret not in out


def test_private_key_block_complete_and_truncated(r):
    assert red(r, f"key:\n{PEM}\nafter") == "key:\n[credential]\nafter"
    cut = PEM.rsplit("\n", 2)[0]  # no END line, as after truncation
    out = red(r, cut + "\n…[truncated]")
    assert "[credential]" in out and "BEGIN" not in out and rand(ALNUM + "+/", 64, 1) not in out


def test_bearer_and_authorization(r):
    assert red(r, f"curl -H 'Authorization: Bearer {OPAQUE}'") == "curl -H 'Authorization: Bearer [credential]'"
    assert red(r, f"Bearer {OPAQUE}") == "Bearer [credential]"
    basic = base64.b64encode(b"user:" + b"pass" + b"word").decode()
    assert red(r, f"Authorization: Basic {basic}") == "Authorization: Basic [credential]"


@pytest.mark.parametrize(
    "text",
    ["Bearer tokens are used here", "use a bearer token", "Bearer $TOKEN", "Bearer ${API_KEY}",
     "Authorization: Bearer <token>", "sk-learn-compatible-estimator-interface", "the task-runner-for-all-workers"],
)  # fmt: skip
def test_credential_negatives(r, text):
    assert red(r, text) == text


@pytest.mark.parametrize(
    "template",
    ["api_key={v}", 'export OPENAI_API_KEY="{v}"', '"client_secret": "{v}"', "X-Auth-Token: {v}",
     "AWS_SECRET_ACCESS_KEY={v}", "secretKey: {v}", "the token is {v}", "access_token => '{v}'"],
)  # fmt: skip
def test_keyword_secrets(r, template):
    out, c = r.redact(template.format(v=HEXKEY))
    assert HEXKEY not in out and c == {"credential": 1}
    assert out == template.format(v="[credential]")


def test_password_values(r):
    pw = "hunter" + "22"
    assert red(r, f"password: {pw}") == "password: [credential]"
    assert red(r, "db_passwd=correcthorsebatterystaple") == "db_passwd=[credential]"


@pytest.mark.parametrize(
    "text",
    ["max_tokens: 4096", "token count: 123456789", f"monkey: {HEXKEY}", f"sort_key: {HEXKEY}",
     "Password: required", "pwd: /home/user/work", "password: ********", "api_key=YOUR_API_KEY_HERE",
     f"commit {HEXKEY}", "uuid 16b4ab90-1234-4cde-8f00-0123456789ab", "token: 2026-01-15T13:00:00Z",
     "keyboard: mechanical-switches-x", "api_key: abc123"],
)  # fmt: skip
def test_keyword_negatives(r, text):
    """Hex ids, uuids, counts, dates and placeholders survive; short values are below the bar."""
    assert red(r, text) == text


def test_secret_named_json_field_is_masked_whole(r):
    rec = {"api_key": OPAQUE, "password": "hunter" + "22", "text": "nothing here", "n": 3, "tags": [f"x {AWS_ID}"]}
    out, c = r.redact_obj(rec)
    assert out == {
        "api_key": "[credential]",
        "password": "[credential]",
        "text": "nothing here",
        "n": 3,
        "tags": ["x [credential]"],
    }
    assert c == {"credential": 3}


# --------------------------------------------------------------------------- url credentials


@pytest.mark.parametrize(
    "text, expected",
    [
        ("postgres://admin:pa/ssw0rd@db.internal:5432/x", "postgres://[url-credential]@db.internal:5432/x"),
        ("mysql://root:a?b#c@10.0.0.1/db", "mysql://[url-credential]@10.0.0.1/db"),
        ("amqp://svc:p@ss/w0rd@mq.example.com/vhost", "amqp://[url-credential]@mq.example.com/vhost"),
        # host:port, then an '@' in the path: not userinfo
        ("http://localhost:8000/users/bob@example.com", "http://localhost:8000/users/[email]"),
        ("https://example.com:443/a?to=bob@example.org", "https://example.com:443/a?to=[email]"),
        ("ftp://bob:pw@files.example.com/a@b", "ftp://[url-credential]@b"),  # over-masks, never leaks
    ],
)
def test_url_credentials_with_slash_in_password(r, text, expected):
    """Regression: userinfo stopped at '/', '?' or '#', so part of the password leaked."""
    out = red(r, text)
    assert out == expected
    assert "ssw0rd" not in out and "a?b#c" not in out and "w0rd" not in out


def test_url_credentials_mask_only_userinfo(r):
    out, c = r.redact("see https://alice:s3cretpw@example.com/path?q=1 now")
    assert out == "see https://[url-credential]@example.com/path?q=1 now" and c == {"url-credential": 1}
    # an unencoded '@' in the password must not leak its tail
    out = red(r, "postgres://user:pa@ss@db.internal:5432/app")
    assert out == "postgres://[url-credential]@db.internal:5432/app"
    assert red(r, f"https://{OPAQUE}@github.com/org/repo") == "https://[url-credential]@github.com/org/repo"


@pytest.mark.parametrize(
    "url",
    ["https://example.com/a@b", "ssh://git@github.com/x", "https://x.com/@user",
     "https://en.wikipedia.org/wiki/Foo:Bar", "http://localhost:8080/path", "https://example.com/?q=a:b"],
)  # fmt: skip
def test_normal_urls_untouched(r, url):
    assert red(r, url) == url


# --------------------------------------------------------------------------- ip


def test_ip_rule_is_optional():
    text = "hosts 10.0.0.5 192.168.1.20:8080 127.0.0.1 172.16.4.2 ::1 fe80::1"
    assert Redactor().redact(text)[0] == text
    out, c = Redactor(["default", "ip"]).redact(text)
    assert out == "hosts [ip] [ip]:8080 [ip] [ip] [ip] [ip]" and c == {"ip": 6}


@pytest.mark.parametrize(
    "text", ["8.8.8.8", "172.32.0.1", "10.0.19041.1", "300.1.1.1", "12:30:45", "aa:bb:cc:dd:ee:ff", "2001:db8::1"]
)
def test_ip_negatives(text):
    assert Redactor(["ip"]).redact(text)[0] == text


# --------------------------------------------------------------------------- config


def test_rules_are_toggleable():
    text = f"bob@example.com +1 415 555 0134 {AWS_ID}"
    assert Redactor(disable=["phone"]).redact(text)[0] == "[email] +1 415 555 0134 [credential]"
    assert Redactor(["email"]).redact(text)[0] == f"[email] +1 415 555 0134 {AWS_ID}"
    assert Redactor("credential").redact(text)[0] == "bob@example.com +1 415 555 0134 [credential]"
    assert Redactor("email,phone").rule_names == ["email", "phone"]
    assert Redactor("all").rule_names == list(ALL_RULES) and "ip" not in DEFAULT_RULES
    with pytest.raises(ValueError, match="unknown redaction rule"):
        Redactor(["emails"])


def test_rule_order_is_canonical():
    # url_credential must run before email even if listed after it
    r = Redactor(["email", "url_credential"])
    assert r.rule_names == ["url_credential", "email"]
    assert r.redact("ftp://bob:pw@files.example.com")[0] == "ftp://[url-credential]@files.example.com"


def test_every_rule_documents_tradeoffs():
    for rule in RULES.values():
        assert rule.doc.strip(), rule.name
        assert rule.placeholder in {"[email]", "[phone]", "[credential]", "[url-credential]", "[ip]"}


def test_mask_text():
    assert mask_text(None) == "" and mask_text("") == ""
    assert mask_text("help@agentvillage.org bob@x.com", ["agentvillage.org"]) == "help@agentvillage.org [email]"
    assert mask_text(f"k {AWS_ID}", ()) == "k [credential]"
    assert mask_text(f"k {AWS_ID} bob@x.com", (), rules=("email", "phone")) == f"k {AWS_ID} [email]"


# --------------------------------------------------------------------------- idempotence and speed

CORPUS = "\n".join(
    [
        "Contact bob.smith@gmail.com or call +1 415 555 0134 / (415) 555-0199.",
        f"keys {AWS_ID} {GITHUB} {SLACK} {OPENAI_LIKE} {STRIPE_LIKE} {GOOGLE_LIKE} {GITHUB_PAT}",
        f"jwt {JWT}; Authorization: Bearer {OPAQUE}; api_key={HEXKEY}; password: hunter22",
        "db postgres://user:pa@ss@db.internal:5432/app and https://alice:pw@example.com/x",
        PEM,
        "hosts 10.0.0.5 and ::1; version 1.234.5 on 2026-01-15; commit " + rand(HEX, 40, 99),
    ]
)


@pytest.mark.parametrize("rules", ["default", "all", "credential", "email,phone"])
def test_idempotent(rules):
    r = Redactor(rules)
    once, c1 = r.redact(CORPUS)
    twice, c2 = r.redact(once)
    assert twice == once and not c2 and c1
    obj_once, _ = r.redact_obj({"text": CORPUS, "api_key": OPAQUE})
    obj_twice, c3 = r.redact_obj(obj_once)
    assert obj_twice == obj_once and not c3


def test_no_value_survives_strict_redaction():
    out, c = Redactor.strict().redact(CORPUS)
    for secret in (AWS_ID, GITHUB, SLACK, OPENAI_LIKE, STRIPE_LIKE, GOOGLE_LIKE, GITHUB_PAT, JWT, OPAQUE, HEXKEY,
                   "bob.smith@gmail.com", "555 0134", "pa@ss", "10.0.0.5", "hunter22"):  # fmt: skip
        assert secret not in out, secret
    assert set(c) == {"email", "phone", "credential", "url-credential", "ip"}


def test_large_input_is_fast():
    rng = random.Random(0)
    words = "the agents discussed a plan to ship version 1.2.3 on 2026-01-15 with 21,596 users token key secret".split()
    para = (
        " ".join(rng.choice(words) for _ in range(200))
        + f" mail bob@example.com call +1 415 555 0134 api_key={HEXKEY}.\n"
    )
    big = para * 1500  # ~2 MB
    t = time.perf_counter()
    out, c = Redactor.strict().redact(big)
    assert time.perf_counter() - t < 5
    assert c == {"email": 1500, "phone": 1500, "credential": 1500}


@pytest.mark.parametrize(
    "blob",
    ["a" * 300_000, "1" * 300_000, "a@" * 150_000, "1." * 150_000, "a:" * 150_000, "+1 " * 100_000,
     "eyJ" * 100_000, "key=" * 75_000, "http://" + "a@" * 100_000, "x@" + "ab." * 100_000 + "1",
     PK_HEAD + "A" * 300_000, "password: " * 30_000],
    ids=lambda b: repr(b[:12]),
)  # fmt: skip
def test_pathological_inputs_stay_linear(blob):
    t = time.perf_counter()
    Redactor.strict().redact(blob)
    assert time.perf_counter() - t < 2


def test_many_private_key_headers_stay_linear(r):
    """Regression: an unbounded lazy body made 20k BEGIN headers without footers take ~24 s."""
    text = (PK_HEAD + " x\n") * 20000
    t0 = time.perf_counter()
    out, c = r.redact(text)
    assert time.perf_counter() - t0 < 2.0 and c == {"credential": 20000}
    two = red(r, f"a {PEM} b {PEM} c")
    assert two == "a [credential] b [credential] c"
