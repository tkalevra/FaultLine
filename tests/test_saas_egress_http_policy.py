"""Egress policy: SSRF reachability and http-vs-https TRANSPORT are two different things.

WHAT THIS PINS, AND WHY IT EXISTS
=================================
``saas.llm_config.validate_egress_url`` used to reject a tenant endpoint when EITHER

  (1) it resolved to private / loopback / link-local / cloud-metadata / reserved space, OR
  (2) it was plain ``http://`` to a PUBLIC address,

with the operator's GLOBAL ``SAAS_LLM_ALLOWED_EGRESS`` env as the only escape from either.
Only (1) is SSRF. (2) is a TLS policy choice wearing an SSRF costume: a public address cannot
be used to pivot into our network, so blocking it bought no security and instead put the
operator in the loop for a decision belonging to the tenant — on 2026-07-31 it meant hand-
editing a prod env file to onboard one tenant's public http endpoint.

So the two were split. These tests are the fence around the split:

  * the SSRF half must NOT have moved. Every private/loopback/link-local/metadata/reserved
    answer is still refused ABSOLUTELY, on EVERY resolved address, with the operator-only
    allowlist as the sole exception (that allowlist is how pre-prod reaches its LAN brain —
    breaking it breaks the dev loop).
  * the transport half must be OPEN: ``http://`` to a public address now works with NO
    allowlist entry at all.

Anything that makes an "http + public" case fail again is the regression this file exists to
catch, and anything that makes a private/reserved case PASS is a security bug.

NO NETWORK, NO DNS. ``socket.getaddrinfo`` is monkeypatched for hostname cases so the suite
is deterministic and cannot be turned green or red by a resolver. IP literals need no
resolution and are used wherever possible.

Run: python3 tools/fltest.py --bug EGRESSPOLICY --test tests/test_saas_egress_http_policy.py
     (tests/ is gitignored → git add -f)
"""

import contextlib

import pytest

from saas import llm_config as C


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    """Default posture for every test: a PLAIN tenant, no operator allowlist entries.

    The allowlist tests set it explicitly. Anything that passes here passes for a real
    customer with no special-casing anywhere.
    """
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    C.invalidate_config_cache()
    yield
    C.invalidate_config_cache()


def _dns(monkeypatch, mapping):
    """Deterministic getaddrinfo: {host: [ip, ...]} — ``None`` means NXDOMAIN."""
    def fake(host, *a, **k):
        if host not in mapping:
            raise AssertionError(f"test resolved an unexpected host: {host!r}")
        ips = mapping[host]
        if ips is None:
            import socket as _s
            raise _s.gaierror("NXDOMAIN")
        return [(0, 1, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr(C.socket, "getaddrinfo", fake)


# ════════════════════════════════════════════════════════════════════════════════════
# THE RELAXATION — http:// to a PUBLIC address, with NO allowlist entry
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://93.184.216.34:8080/v1",     # public IPv4 literal
    "http://8.8.8.8/v1",
    "http://1.1.1.1",
    "http://[2606:4700:4700::1111]/v1",  # public IPv6 literal
])
def test_http_to_public_address_is_allowed_without_any_allowlist(url):
    """THE ASK: 'a public IP shouldn't require fuck-all to get working beyond tenant CLI.'

    No SAAS_LLM_ALLOWED_EGRESS entry, no operator involvement, no env edit on prod.
    """
    cls = C.validate_egress_url(url)
    assert cls["cleartext"] is True
    assert cls["allowlisted"] is False, "a public http endpoint must not need the allowlist"


def test_http_to_public_hostname_is_allowed(monkeypatch):
    _dns(monkeypatch, {"brain.acme.example": ["93.184.216.34"]})
    assert C.validate_egress_url("http://brain.acme.example:8080/v1")["cleartext"] is True


@pytest.mark.parametrize("url", [
    "https://93.184.216.34/v1",
    "https://8.8.8.8",
    "https://[2606:4700:4700::1111]/v1",
])
def test_https_to_public_still_allowed_and_not_flagged_cleartext(url):
    cls = C.validate_egress_url(url)
    assert cls["cleartext"] is False


def test_url_is_cleartext_helper_matches_the_validator():
    """The console disclaimer and the save-time log both key on this helper, so it must not
    drift from the validator. A REJECTED url is not 'cleartext' — it is not an endpoint."""
    assert C.url_is_cleartext("http://8.8.8.8/v1") is True
    assert C.url_is_cleartext("https://8.8.8.8/v1") is False
    assert C.url_is_cleartext("http://127.0.0.1/v1") is False    # blocked → not an endpoint
    assert C.url_is_cleartext("ftp://8.8.8.8/v1") is False
    assert C.url_is_cleartext("") is False


# ════════════════════════════════════════════════════════════════════════════════════
# THE SSRF HALF — NOT relaxed. Every one of these must stay blocked, forever.
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    # cloud metadata — the credential-theft target this guard exists for
    "http://169.254.169.254/latest/meta-data/",
    "https://169.254.169.254/latest/meta-data/",
    "http://[fd00:ec2::254]/latest/meta-data/",
    # loopback — our own unauthenticated api:8000 / redis / qdrant / postgres
    "http://127.0.0.1:11434/v1",
    "https://127.0.0.1:8000/ingest",
    "http://127.0.0.53:8000/",
    "http://0.0.0.0:8000/",
    # RFC1918 — the inward pivot
    "http://10.0.0.5:1234/v1",
    "https://10.0.0.5:1234/v1",
    "http://172.16.5.5/v1",
    "http://192.168.1.10/v1",
    "https://192.168.1.10/v1",
    # other non-globally-routable IPv4
    "http://169.254.10.9/v1",       # link-local
    "http://100.64.0.7/v1",         # RFC 6598 CGNAT / Shared Address Space
    "http://198.18.0.1/v1",         # benchmarking
    "http://203.0.113.10/v1",       # TEST-NET-3
    "http://240.0.0.1/v1",          # reserved 240/4
    "http://224.0.0.1/v1",          # multicast
    # obfuscated loopback — the resolver normalizes these; the guard must judge the RESULT
    "http://2130706433/v1",         # decimal
    "http://0x7f000001/v1",         # hex
    "http://127.1/v1",              # short form
])
def test_ssrf_targets_still_blocked_regardless_of_scheme(url):
    """https is not a licence and http is not the crime — REACHABILITY is what is judged."""
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url(url)


def test_cgnat_shared_address_space_blocked_on_every_python():
    """REGRESSION PIN — 100.64.0.0/10 is where the stdlib MOVED under us.

    On the python:3.11 runtime image ``is_private`` is True for 100.64/10; on 3.13+ it is
    False (measured on 3.14: is_private=False, is_global=False). A guard resting on
    ``is_private`` alone therefore silently OPENS carrier/cloud-internal space the day the
    base image moves. ``_ip_is_blocked`` also requires ``is_global``, which is False on both.

    If this ever goes green-to-red, do not "fix" it by deleting the case.
    """
    import ipaddress
    assert C._ip_is_blocked(ipaddress.ip_address("100.64.0.7")) is True
    assert C._ip_is_blocked(ipaddress.ip_address("100.127.255.254")) is True
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("http://100.64.0.7/v1")


# ── IPv6 ────────────────────────────────────────────────────────────────────────────
# Same reasoning end to end, plus the transition formats that let a v4 destination be
# written as a v6 literal. `::ffff:10.0.0.5` IS 10.0.0.5 and is judged as 10.0.0.5.

@pytest.mark.parametrize("url,why", [
    ("http://[::1]:1234/v1",                 "IPv6 loopback"),
    ("http://[::]:1234/v1",                  "IPv6 unspecified"),
    ("http://[fd12:3456:789a::1]/v1",        "ULA fc00::/7"),
    ("http://[fc00::1]/v1",                  "ULA fc00::/7 low half"),
    ("http://[fe80::1]/v1",                  "link-local fe80::/10"),
    ("http://[fec0::1]/v1",                  "site-local fec0::/10 (RFC 3879 — stdlib misses it)"),
    ("http://[ff02::1]/v1",                  "multicast"),
    ("http://[::ffff:127.0.0.1]/v1",         "v4-mapped loopback"),
    ("http://[::ffff:10.0.0.5]/v1",          "v4-mapped RFC1918"),
    ("http://[::ffff:169.254.169.254]/v1",   "v4-mapped cloud metadata"),
    ("http://[2002:c0a8:0101::1]/v1",        "6to4 wrapping 192.168.1.1"),
    ("http://[2002:0a00:0001::1]/v1",        "6to4 wrapping 10.0.0.1"),
    ("http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/", "Teredo"),
    ("http://[64:ff9b::a00:1]/v1",           "NAT64 well-known prefix wrapping 10.0.0.1"),
])
def test_ipv6_internal_and_transition_formats_blocked(url, why):
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url(url)


def test_ipv6_site_local_is_blocked_by_us_not_by_the_stdlib():
    """fec0::/10 is deprecated (RFC 3879) but still routes INSIDE a network, and neither
    ``is_private`` nor ``is_reserved`` nor ``is_global``-false catches it on 3.11 or 3.14 —
    measured. ``_ip_is_blocked`` blocks it explicitly. This asserts BOTH halves so a future
    stdlib change cannot make the explicit clause look redundant and get deleted."""
    import ipaddress
    a = ipaddress.ip_address("fec0::1")
    assert a.is_private is False and a.is_reserved is False and a.is_global is True, \
        "stdlib changed — re-derive the site-local rule before touching _ip_is_blocked"
    assert C._ip_is_blocked(a) is True


def test_ipv6_ula_via_hostname_blocked(monkeypatch):
    _dns(monkeypatch, {"v6.internal.example": ["fd00:dead:beef::1"]})
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("https://v6.internal.example/v1")


# ── multi-record / rebinding / fail-closed ──────────────────────────────────────────

def test_multi_record_host_with_one_private_answer_is_rejected(monkeypatch):
    """A hostname holds SEVERAL A/AAAA records and can change them later (DNS rebinding).
    One private answer among public ones is a smuggle, not a fallback — ANY non-public
    answer rejects the WHOLE url, in either record order."""
    _dns(monkeypatch, {"rebind.example": ["8.8.8.8", "10.1.2.3"]})
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("http://rebind.example/v1")
    _dns(monkeypatch, {"rebind.example": ["10.1.2.3", "8.8.8.8"]})
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("http://rebind.example/v1")
    # ...and the metadata address hiding behind a public one.
    _dns(monkeypatch, {"rebind.example": ["8.8.8.8", "169.254.169.254"]})
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("http://rebind.example/v1")


def test_multi_record_all_public_over_http_is_allowed(monkeypatch):
    _dns(monkeypatch, {"cdn.example": ["8.8.8.8", "1.1.1.1"]})
    assert C.validate_egress_url("http://cdn.example/v1")["cleartext"] is True


def test_mixed_v4_v6_records_one_internal_is_rejected(monkeypatch):
    _dns(monkeypatch, {"dual.example": ["93.184.216.34", "fd00::1"]})
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("https://dual.example/v1")


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "http:///v1",              # no host
    "ftp://8.8.8.8/v1",        # scheme
    "file:///etc/passwd",
    "gopher://8.8.8.8/",
    "//8.8.8.8/v1",            # scheme-relative → no scheme
])
def test_ambiguity_fails_closed(url):
    """Fail-OPEN on the SSRF half would be the actual vulnerability."""
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url(url)


def test_unresolvable_host_fails_closed(monkeypatch):
    """A name we cannot vet is denied — never 'allowed because we could not check'."""
    _dns(monkeypatch, {"nope.invalid": None})
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("http://nope.invalid/v1")


def test_resolver_exception_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise OSError("resolver on fire")
    monkeypatch.setattr(C.socket, "getaddrinfo", boom)
    with pytest.raises(C.EgressBlocked):
        C.validate_egress_url("https://whatever.example/v1")


# ════════════════════════════════════════════════════════════════════════════════════
# OPERATOR ESCAPE HATCH — the ONLY way to an internal address. Must keep working.
# ════════════════════════════════════════════════════════════════════════════════════

def test_operator_allowlist_still_reaches_a_lan_brain(monkeypatch):
    """This is how pre-prod reaches its LAN brain (192.0.2.20:1234, LM Studio).
    Breaking it breaks the dev loop, so it is pinned by address, not in principle."""
    monkeypatch.setenv("SAAS_LLM_ALLOWED_EGRESS", "10.0.0.0/8,192.0.2.0/24")
    for url in ("http://10.0.0.5:11434/v1", "http://192.0.2.20:1234", "https://10.0.0.5/v1"):
        cls = C.validate_egress_url(url)
        assert cls["allowlisted"] is True


def test_allowlist_is_scoped_not_a_blanket_unlock(monkeypatch):
    monkeypatch.setenv("SAAS_LLM_ALLOWED_EGRESS", "192.0.2.0/24")
    for url in ("http://192.168.1.10/v1",         # different RFC1918 subnet
                "http://10.0.0.5/v1",
                "http://127.0.0.1:1234/v1",
                "http://169.254.169.254/"):       # metadata is never implied
        with pytest.raises(C.EgressBlocked):
            C.validate_egress_url(url)


def test_a_tenant_cannot_self_allowlist():
    """The allowlist is read from the OPERATOR's process env and from nowhere else — there is
    no per-tenant, per-request or per-payload input into it. Pinned structurally: the parser
    takes no arguments and reads exactly one env var."""
    import inspect
    assert list(inspect.signature(C._allowed_networks).parameters) == []
    src = inspect.getsource(C._allowed_networks)
    assert "SAAS_LLM_ALLOWED_EGRESS" in src and src.count("os.environ") == 1


# ════════════════════════════════════════════════════════════════════════════════════
# OPERATOR VISIBILITY — logged ONCE, at save. Not per request.
# ════════════════════════════════════════════════════════════════════════════════════

class _FakeCur:
    def execute(self, *a, **k):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def saved(monkeypatch):
    """Capture WARN lines emitted by a save, with the DB and crypto stubbed out."""
    from saas import crypto as _crypto
    from saas import db as _db

    @contextlib.contextmanager
    def _connection():
        yield _FakeConn()

    monkeypatch.setattr(_db, "connection", _connection)
    monkeypatch.setattr(_crypto, "encrypt", lambda p: "CT:" + str(p))
    lines: list = []
    monkeypatch.setattr(C, "_log_warn", lambda event, **kw: lines.append((event, kw)))
    return lines


def test_cleartext_endpoint_logged_once_per_save(saved):
    C.upsert_config("acct-1", {"base_url": "http://8.8.8.8:8080", "model": "m"})
    hits = [kw for ev, kw in saved if ev == "llm_config.cleartext_endpoint"]
    assert len(hits) == 1, "exactly one operator line per cleartext URL saved"
    assert hits[0]["host"] == "8.8.8.8"
    assert hits[0]["account_id"] == "acct-1"
    assert hits[0]["scope"] == "account"


def test_seat_save_logs_with_the_seat_identity(saved):
    C.upsert_seat_config("user-9", "acct-1", {"base_url": "http://8.8.8.8", "model": "m"})
    hits = [kw for ev, kw in saved if ev == "llm_config.cleartext_endpoint"]
    assert len(hits) == 1
    assert hits[0]["scope"] == "seat" and hits[0]["user_id"] == "user-9"


def test_https_save_logs_nothing(saved):
    C.upsert_config("acct-1", {"base_url": "https://8.8.8.8", "model": "m"})
    assert [ev for ev, _ in saved if ev == "llm_config.cleartext_endpoint"] == []


def test_the_log_never_carries_a_secret(saved):
    C.upsert_config("acct-1", {"base_url": "http://8.8.8.8/v1?token=SUPERSECRET",
                               "api_key": "sk-do-not-log-me", "model": "m"})
    blob = repr(saved)
    assert "sk-do-not-log-me" not in blob
    assert "SUPERSECRET" not in blob, "host only — never the path or query string"
    assert "CT:" not in blob, "never the ciphertext either"


def test_resolve_path_does_not_log_per_request(monkeypatch, saved):
    """The hot path runs behind a 5s cache on EVERY llm call. Logging there would be spam and
    the signal would be unreadable, so the save is the only seam that emits."""
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import crypto as _crypto
    monkeypatch.setattr(_crypto, "decrypt", lambda ct: "k")
    row = ("openai", "http://8.8.8.8", "m", "", None, None,
           None, None, None, None, None, None, None)
    C._build_config("acct-1", row)
    C._build_config("acct-1", row)
    assert [ev for ev, _ in saved if ev == "llm_config.cleartext_endpoint"] == []


def test_a_blocked_url_never_reaches_the_database(saved):
    """Validation runs BEFORE encryption and before any write — a rejected save must not have
    encrypted-and-discarded a credential, nor written a partial row."""
    with pytest.raises(C.EgressBlocked):
        C.upsert_config("acct-1", {"base_url": "http://169.254.169.254",
                                   "api_key": "sk-secret", "model": "m"})
    assert saved == []


# ════════════════════════════════════════════════════════════════════════════════════
# SAAS_MODE OFF — byte-for-byte unchanged
# ════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_resolves_no_brain_so_no_policy_applies(monkeypatch):
    """With SAAS off the resolvers short-circuit before any egress decision, so nothing in
    this policy — old or new — can reach a public deployment's behavior."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("SAAS_MODE", "0")
    C.invalidate_config_cache()
    assert C.resolve_account_llm_config("acct-1") is None
    assert C.resolve_seat_llm_config("user-1", "acct-1") is None


def test_saas_off_inference_path_never_egress_checks(monkeypatch):
    """``_tenant_egress_blocked`` gates ONLY a per-tenant BYO endpoint. The operator's own
    global env brain may legitimately be a LAN host and is NEVER checked — checking it would
    break the core deployment."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("SAAS_MODE", "0")
    from src.api import llm_calls
    assert llm_calls._tenant_egress_blocked() is False


# ════════════════════════════════════════════════════════════════════════════════════
# THE CONSOLE DISCLAIMER — the copy exists, is shared, and is CSP-legal
# ════════════════════════════════════════════════════════════════════════════════════
#
# The render itself is proven in a browser DOM (jsdom) rather than here; these are the
# static invariants that a Python suite can hold, and they are exactly the ones this
# codebase has broken before: an inline script (CSP → blank page), a second copy of the
# copy (drift), and a warning nothing invokes.

_DASH = "saas/dashboard/"


def _read(name):
    import pathlib
    return pathlib.Path(_DASH + name).read_text(encoding="utf-8")


def test_disclaimer_markup_exists_under_the_base_url_field_in_both_consoles():
    for page in ("user.html", "tenant.html"):
        html = _read(page)
        assert 'id="llm-cleartext-note"' in html, f"{page} has no disclaimer element"
        # It must FOLLOW the input, or it is not "below the field".
        assert html.index('id="llm-baseurl"') < html.index('id="llm-cleartext-note"'), page


def test_both_consoles_invoke_it():
    """A warning nothing calls is this project's most repeated bug.

    The bind now carries an optional third arg (the payload variant: 'chat' | 'embedding'),
    because the tenant console has a SECOND endpoint field. Match the call loosely on the two
    ids so this pins "it is invoked" and not the argument list. The embedding binding and the
    per-variant wording are pinned in tests/test_connect_tabs_and_cleartext.py.
    """
    import re
    for script in ("user.js", "tenant.js"):
        src = _read(script)
        assert re.search(r"FL_CLEARTEXT\.bind\('llm-baseurl', 'llm-cleartext-note'", src), script
        assert "bindCleartextNotice()" in src, script


def test_the_disclaimer_copy_has_exactly_one_home():
    """Three copies of the MCP tool descriptions drifted and shipped an impoverished one to
    every OpenWebUI model. One implementation, two consoles."""
    assert "window.FL_CLEARTEXT" in _read("feature_copy.js")
    for script in ("user.js", "tenant.js"):
        src = _read(script)
        assert "unencrypted and readable in transit" not in src, \
            f"{script} grew its own copy of the warning text — keep it in feature_copy.js"


def test_the_disclaimer_says_the_two_things_that_matter():
    src = _read("feature_copy.js")
    assert "unencrypted and readable in transit" in src   # the transit exposure
    assert "capture the key" in src                       # the credential, specifically
    assert "https://" in src                              # the recommendation


def test_the_disclaimer_does_not_block_the_save():
    """The owner asked for a disclaimer, not a consent wall: no gate, no modal, no
    acknowledgement to defeat, and nothing disabled."""
    src = _read("feature_copy.js")
    # It may only ever read the input and write the notice. Anything that could stop a save —
    # disabling a control, cancelling an event, or putting a dialog in the way — is forbidden.
    for forbidden in ("disabled", "confirm(", "alert(", "preventDefault",
                      "stopPropagation", "requestSubmit", "showModal"):
        assert forbidden not in src, f"FL_CLEARTEXT must not gate the save (found {forbidden!r})"
    # ...and it must not reach for the save button or the form at all.
    for reach in ("btn-llm-save", "llm-save", "querySelector('form", 'getElementsByTagName("form'):
        assert reach not in src, f"FL_CLEARTEXT must not touch the save path (found {reach!r})"


def test_dashboard_stays_csp_clean():
    """script-src 'self' with NO 'unsafe-inline' — an inline-script build renders BLANK."""
    import re
    for page in ("user.html", "tenant.html"):
        html = _read(page)
        inline = [m for m in re.findall(r"<script\b[^>]*>", html) if "src=" not in m]
        assert not inline, f"{page} has an inline <script> — CSP will blank the page"
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", html), f"{page} has an on*= handler"
