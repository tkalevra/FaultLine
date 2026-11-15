"""Stripe return ORIGIN authority — ``saas/tenant_checkout_routes.py::_request_origin`` /
``_return_urls`` (gauntlet checkout-origin-authority, 2026-09-14).

What is pinned, bar by bar:

  CO1 — authority order: operator ``SAAS_MCP_PUBLIC_URL`` > allowlisted request host > refuse.
        The operator value is normalized by the SAME ``saas/acl.py::normalize_origin`` that
        ``acl._face_origins`` applies for CORS, and the allowlist is parsed by the SAME
        ``src/mcp/host_rules`` code ``src/mcp/http_server.py`` uses for the face gate — one truth,
        proven here by driving both through the shipped functions rather than re-implementing them.
  CO2 — ``Host: app.example.test:8002`` with ``app.example.test`` allowlisted keeps ``:8002``
        (RFC 9110 §7.2: ``Host = uri-host [ ":" port ]``); ``X-Forwarded-Proto`` limited to
        http/https.
  CO3 — a host not in the allowlist → ``''``; an EMPTY allowlist with no operator origin → ``''``
        and a ``_log_crit`` naming ``SAAS_MCP_PUBLIC_URL`` / ``SAAS_TENANT_HOSTS`` (fail-closed).
  CO4 — revert-red: with HEAD 3f250759's ``_request_origin`` restored, the port test and the
        allowlist tests go red (HEAD had no allowlist and its regex rejected ``:``).
  ROUND 3 (critic gap: the adapter had its OWN authority) — ONE resolver,
        ``saas/acl.py::resolve_public_origin``, is consulted by BOTH ``_request_origin`` and
        ``payments_stripe._public_origin``: ``SAAS_MCP_PUBLIC_URL`` > ``SAAS_PUBLIC_BASE_URL``
        (legacy alias, deprecation ``_log_crit`` every time it resolves) > SMTP
        ``reset_link_base_url`` origin (explicit last operator source). The FULL path is driven:
        ``_return_urls`` → ``(None, None)`` → the REAL ``create_checkout`` → ``invalid_return_url``
        (no relative url, no session). Port range 1–65535; ``*.SAAS_TENANT_BASE_DOMAINS`` (and the
        auto-derived tenant base) accepted as served faces, via the lifted face-gate derivation.

The request is a MINIMAL fake (headers mapping + ``url.scheme``) — the function reads nothing
else, and a real Starlette ``Request`` would only add plumbing the assertions do not touch. Env
is monkeypatched per test; the module reads the allowlist at CALL time so no reload is needed.

Names are RFC 2606 / RFC 6761 reserved (``.example`` / ``.test``) — no real host appears here.

Run (NOT raw pytest — a raw run is invisible to the owner's dashboard):
    python3 tools/fltest.py --bug CO4 --test tests/test_tenant_checkout_origin.py
"""

import types

import pytest

from saas import acl
from saas import payments_stripe
from saas import smtp_config
from saas import tenant_checkout_routes as tcr
from src.mcp import host_rules

_OPERATOR_ENVS = ("SAAS_MCP_PUBLIC_URL", "SAAS_PUBLIC_BASE_URL")
_FACE_ENVS = ("SAAS_TENANT_HOSTS", "SAAS_ADMIN_HOSTS", "SAAS_TENANT_BASE_DOMAINS")


# ════════════════════════════════════════════════════════════════════════════════════
# The minimal fake request
# ════════════════════════════════════════════════════════════════════════════════════

class _Headers(dict):
    """Starlette's ``Headers`` is case-insensitive; the function reads lowercase keys."""

    def get(self, key, default=None):  # noqa: D401 - dict-compatible
        return super().get(key.lower(), default)


def _req(headers=None, scheme="http"):
    h = _Headers({k.lower(): v for k, v in (headers or {}).items()})
    return types.SimpleNamespace(headers=h, url=types.SimpleNamespace(scheme=scheme))


@pytest.fixture
def env(monkeypatch):
    """Start every test from NO operator source (both envs unset, SMTP config absent — patched at
    ``saas.smtp_config.resolve_config`` so no DB is touched) and EMPTY face configuration, then let
    the test set what it needs. ``conftest.py`` deliberately leaves the host lists unset."""
    for var in _OPERATOR_ENVS + _FACE_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(smtp_config, "resolve_config", lambda: None)
    return monkeypatch


def _set_smtp_base(monkeypatch, reset_link_base_url):
    """Make the SMTP config carry a ``reset_link_base_url`` — the operator's third source."""
    monkeypatch.setattr(smtp_config, "resolve_config",
                        lambda: {"host": "mail.example.test", "from_address": "no-reply@example.test",
                                 "reset_link_base_url": reset_link_base_url})


def _crit_lines(capsys):
    """CRIT lines from EITHER emitter on this path: the route (``[tenant-checkout-CRIT]``) and
    the shared resolver in acl (``[saas-acl]``)."""
    return [ln for ln in capsys.readouterr().err.splitlines()
            if "[tenant-checkout-CRIT]" in ln or "[saas-acl]" in ln]


# ════════════════════════════════════════════════════════════════════════════════════
# CO1 — authority order
# ════════════════════════════════════════════════════════════════════════════════════

def test_operator_origin_wins_over_every_header(env):
    """``SAAS_MCP_PUBLIC_URL`` is authoritative: a forged X-Forwarded-Host AND an allowlisted Host
    are both ignored when it is set — the operator's value is the only thing returned."""
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test")
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    r = _req({"x-forwarded-host": "evil.example", "host": "app.example.test:8002",
              "x-forwarded-proto": "http"})
    assert tcr._request_origin(r) == "https://console.example.test"


def test_operator_origin_is_normalized_by_the_same_acl_normalizer(env):
    """The value is normalized EXACTLY as ``acl._face_origins`` normalizes it for CORS — lowercase,
    trailing slash stripped — so the CORS face and the Stripe return origin are one string."""
    env.setenv("SAAS_MCP_PUBLIC_URL", "HTTPS://Console.Example.Test/")
    got = tcr._request_origin(_req({"host": "whatever.example"}))
    assert got == acl.normalize_origin("HTTPS://Console.Example.Test/") == "https://console.example.test"
    # and it is the same string acl derives as a face origin from that env
    assert got in acl._face_origins()


def test_operator_origin_keeps_its_own_port(env):
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test:8002")
    assert tcr._request_origin(_req({"host": "evil.example"})) == "https://console.example.test:8002"


def test_operator_origin_that_is_not_an_origin_is_ignored_loudly_and_falls_to_the_allowlist(env, capsys):
    """A set-but-junk operator value (a path, no scheme) must not be returned verbatim — it is
    logged and treated as unset, and the request host is then still allowlist-gated."""
    env.setenv("SAAS_MCP_PUBLIC_URL", "console.example.test/dashboard")   # no scheme, has a path
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "app.example.test"}, scheme="https")) == "https://app.example.test"
    crit = _crit_lines(capsys)
    assert any("SAAS_MCP_PUBLIC_URL" in ln and "not a web origin" in ln for ln in crit), crit
    # …and with NO allowlist the junk value does not rescue the request host either
    env.delenv("SAAS_TENANT_HOSTS")
    assert tcr._request_origin(_req({"host": "app.example.test"})) == ""


def test_allowlisted_request_host_is_used_only_when_no_operator_origin(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "app.example.test"}, scheme="https")) == "https://app.example.test"


def test_admin_hosts_are_part_of_the_allowlist_too(env):
    env.setenv("SAAS_ADMIN_HOSTS", "fcontrol.example.test")
    assert tcr._request_origin(_req({"host": "fcontrol.example.test:8002"}, scheme="https")) \
        == "https://fcontrol.example.test:8002"


def test_allowlist_is_parsed_by_the_face_gates_own_parser(env):
    """One parser, imported twice: the checkout module's allowlist resolution must be byte-identical
    to ``src/mcp/host_rules.parse_host_list`` over the same env — including the parser's own
    :port strip, case fold and trailing-dot fold — and ``http_server`` must be bound to the SAME
    functions (not a copy)."""
    raw = "App.Example.Test:443, fcontrol.example.test., ,mcp.example.test"
    env.setenv("SAAS_TENANT_HOSTS", raw)
    env.setenv("SAAS_ADMIN_HOSTS", "")
    assert tcr._allowlisted_hosts() == host_rules.parse_host_list(raw)
    assert tcr._allowlisted_hosts() == frozenset({"app.example.test", "fcontrol.example.test",
                                                  "mcp.example.test"})
    import src.mcp.http_server as h
    assert h._parse_host_list is host_rules.parse_host_list
    assert h._strip_host_port is host_rules.strip_host_port
    assert tcr._parse_host_list is host_rules.parse_host_list
    assert tcr._strip_host_port is host_rules.strip_host_port


# ════════════════════════════════════════════════════════════════════════════════════
# CO2 — the port survives; proto stays whitelisted
# ════════════════════════════════════════════════════════════════════════════════════

def test_allowlisted_host_with_port_8002_keeps_the_port(env):
    """THE WOUND: HEAD's ``_HOST_RE`` rejected ``:`` so pre-prod's ``:8002`` console produced ``''``
    → a RELATIVE return url → the customer landed on checkout.stripe.com. RFC 9110 §7.2:
    ``Host = uri-host [ ":" port ]`` — the port is part of the Host value and part of the origin."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    r = _req({"host": "app.example.test:8002", "x-forwarded-proto": "https"})
    assert tcr._request_origin(r) == "https://app.example.test:8002"


def test_x_forwarded_host_with_port_is_preferred_over_host(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    r = _req({"x-forwarded-host": "app.example.test:8002", "host": "127.0.0.1:8000",
              "x-forwarded-proto": "https"})
    assert tcr._request_origin(r) == "https://app.example.test:8002"


def test_host_case_and_fqdn_trailing_dot_are_canonicalized_not_refused(env):
    """A browser may send ``App.Example.Test.:8002`` (case-insensitive host, FQDN trailing dot).
    The allowlist match is on the canonical form and the origin is rebuilt from it, so the
    customer still gets a valid absolute url — not a shape refusal."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    r = _req({"host": "App.Example.Test.:8002", "x-forwarded-proto": "https"})
    assert tcr._request_origin(r) == "https://app.example.test:8002"


@pytest.mark.parametrize("proto,expected", [
    ("https", "https"), ("http", "http"), ("HTTPS", "https"),
    ("javascript", "https"), ("data", "https"), ("ftp", "https"), ("", "https"),
])
def test_forwarded_proto_is_limited_to_http_or_https(env, proto, expected):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    hdrs = {"host": "app.example.test:8002"}
    if proto:
        hdrs["x-forwarded-proto"] = proto
    assert tcr._request_origin(_req(hdrs, scheme="https")) == f"{expected}://app.example.test:8002"


def test_request_scheme_is_the_proto_fallback(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "app.example.test"}, scheme="http")) == "http://app.example.test"


# ════════════════════════════════════════════════════════════════════════════════════
# CO3 — forgery refused, fail-closed when unconfigured
# ════════════════════════════════════════════════════════════════════════════════════

def test_forged_x_forwarded_host_yields_no_request_derived_origin(env, capsys):
    """OWASP WSTG-INPV-17: when a Host check exists, the attacker supplies X-Forwarded-Host.
    The allowlist gates BOTH headers, so a forged forwarded host resolves to nothing."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    r = _req({"x-forwarded-host": "evil.example", "host": "app.example.test",
              "x-forwarded-proto": "https"})
    assert tcr._request_origin(r) == ""
    assert any("refus" in ln.lower() and "SAAS_MCP_PUBLIC_URL" in ln for ln in _crit_lines(capsys))


def test_unlisted_host_header_is_refused(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "evil.example"})) == ""


@pytest.mark.parametrize("bad", [
    "evil.example",                  # not listed at all
    "app.example.test.evil.example",  # allowlisted host as a SUBDOMAIN of the attacker's
    "notexample.test",                # auto-derived base "example.test" as a suffix WITHOUT a dot
    "app.example.test:8002/evil",     # path after an allowlisted host:port (strip would match!)
    "app.example.test@evil.example",  # userinfo trick
    "app.example.test:8002:9",        # two ports
    "app.example.test:abc",           # non-numeric port
    "app.example.test:123456",        # port too long
    "app.example.test, evil.example",  # only the first hop is read, and it has a trailing comma-space
    " ",                              # blank
])
def test_shape_and_allowlist_together_refuse_these(env, bad):
    """The allowlist matches the PORT-STRIPPED form, so the ``_HOST_RE`` shape gate is what stops
    a path / userinfo / second-port suffix riding in behind an allowlisted host. Both gates are
    required; each of these fails at least one of them."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": bad})) == "", bad


def test_sibling_subdomain_of_the_tenant_base_is_served_hence_accepted(env):
    """ROUND-3 POLICY, stated: ``evilapp.example.test`` was in round 1's refused list. Under the
    served-face rule it is a SUBDOMAIN of the auto-derived tenant base ``example.test`` — exactly
    what ``http_server._classify_face`` serves as the tenant face (a per-tenant console) — so it
    is now accepted. Inside the deployment's own registrable domain the only party who can point
    it anywhere is the operator's DNS; an attacker needs a label boundary they do not have."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "evilapp.example.test"}, scheme="https")) \
        == "https://evilapp.example.test"


def test_empty_allowlist_and_no_operator_origin_is_fail_closed_and_names_the_fix(env, capsys):
    """THE FAIL-CLOSED CASE: an unconfigured deployment trusts NO request host. The log names the
    two env vars to set — it is a config defect, surfaced, not a silent relative url."""
    r = _req({"host": "app.example.test:8002", "x-forwarded-proto": "https"})
    assert tcr._request_origin(r) == ""
    crit = _crit_lines(capsys)
    assert crit, "expected a _log_crit naming the env vars"
    assert any("SAAS_MCP_PUBLIC_URL" in ln and "SAAS_TENANT_HOSTS" in ln for ln in crit), crit


def test_allowlist_env_present_but_blank_is_still_empty(env):
    env.setenv("SAAS_TENANT_HOSTS", " , ,")
    env.setenv("SAAS_ADMIN_HOSTS", "")
    assert tcr._request_origin(_req({"host": "app.example.test"})) == ""


def test_missing_host_header_entirely_is_refused(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({})) == ""


def test_a_raising_request_is_refused_not_propagated(env):
    """Money path: a broken request object must yield ``''`` (→ adapter default + crit log), never
    an exception out of the subscribe route."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")

    class _Boom:
        def get(self, *_a, **_k):
            raise RuntimeError("headers exploded")

    r = types.SimpleNamespace(headers=_Boom(), url=types.SimpleNamespace(scheme="https"))
    assert tcr._request_origin(r) == ""


# ════════════════════════════════════════════════════════════════════════════════════
# _return_urls — never a relative url when either source resolves
# ════════════════════════════════════════════════════════════════════════════════════

def _assert_absolute_pair(success, cancel, origin):
    for url in (success, cancel):
        assert url is not None
        assert url.startswith(origin + "/"), url
        assert not url.startswith("/"), "relative url reached the adapter"
    assert success == origin + tcr._RETURN_SUCCESS
    assert cancel == origin + tcr._RETURN_CANCEL
    assert "{CHECKOUT_SESSION_ID}" in success   # Stripe's literal placeholder, verbatim


def test_return_urls_are_absolute_from_the_operator_origin(env):
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test")
    s, c = tcr._return_urls(_req({"x-forwarded-host": "evil.example"}))
    _assert_absolute_pair(s, c, "https://console.example.test")


def test_return_urls_are_absolute_from_an_allowlisted_host_with_port(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    s, c = tcr._return_urls(_req({"host": "app.example.test:8002", "x-forwarded-proto": "https"}))
    _assert_absolute_pair(s, c, "https://app.example.test:8002")


def test_return_urls_are_none_none_when_nothing_resolves(env, capsys):
    """``(None, None)`` is a REFUSAL (the adapter consults the same resolver and answers
    ``invalid_return_url``) — and the log says so in words, names the env to set, and no longer
    claims the customer lands on checkout.stripe.com (that relative default died in 944fbaad)."""
    s, c = tcr._return_urls(_req({"host": "app.example.test:8002"}))
    assert (s, c) == (None, None)
    crit = _crit_lines(capsys)
    assert any("invalid_return_url" in ln and "SAAS_MCP_PUBLIC_URL" in ln for ln in crit), crit
    assert not any("land on checkout.stripe.com" in ln for ln in crit)


def test_return_urls_never_yield_a_relative_url_across_the_resolution_matrix(env):
    """Sweep: for every configuration where EITHER source resolves, both urls are absolute."""
    cases = [
        ({"SAAS_MCP_PUBLIC_URL": "https://console.example.test"}, {"host": "evil.example"}),
        ({"SAAS_MCP_PUBLIC_URL": "https://console.example.test"}, {}),
        ({"SAAS_TENANT_HOSTS": "app.example.test"}, {"host": "app.example.test"}),
        ({"SAAS_TENANT_HOSTS": "app.example.test"}, {"host": "app.example.test:8002"}),
        ({"SAAS_ADMIN_HOSTS": "fcontrol.example.test"}, {"x-forwarded-host": "fcontrol.example.test:8002"}),
        ({"SAAS_MCP_PUBLIC_URL": "https://console.example.test",
          "SAAS_TENANT_HOSTS": "app.example.test"}, {"host": "app.example.test:8002"}),
        ({"SAAS_PUBLIC_BASE_URL": "https://legacy.example.test"}, {"host": "evil.example"}),
        ({"SAAS_TENANT_BASE_DOMAINS": "tenants.example"}, {"host": "acme.tenants.example:8002"}),
    ]
    for cfg, hdrs in cases:
        for var in _OPERATOR_ENVS + _FACE_ENVS:
            env.delenv(var, raising=False)
        for k, v in cfg.items():
            env.setenv(k, v)
        s, c = tcr._return_urls(_req(hdrs, scheme="https"))
        assert s and c, (cfg, hdrs)
        for url in (s, c):
            assert url.startswith("https://"), (cfg, hdrs, url)
            assert "://" in url and not url.startswith("/"), (cfg, hdrs, url)


# ════════════════════════════════════════════════════════════════════════════════════
# ROUND 3 — ONE resolver for both the route and the adapter; legacy alias; SMTP source
# ════════════════════════════════════════════════════════════════════════════════════

def test_resolver_order_mcp_public_url_beats_legacy_beats_smtp(env, capsys):
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://canonical.example.test")
    env.setenv("SAAS_PUBLIC_BASE_URL", "https://legacy.example.test")
    _set_smtp_base(env, "https://smtp.example.test/dashboard/user.html")
    assert acl.resolve_public_origin() == ("https://canonical.example.test", "SAAS_MCP_PUBLIC_URL")
    assert not any("DEPRECATED" in ln for ln in _crit_lines(capsys))   # the alias did not resolve
    env.delenv("SAAS_MCP_PUBLIC_URL")
    assert acl.resolve_public_origin() == ("https://legacy.example.test", "SAAS_PUBLIC_BASE_URL")
    env.delenv("SAAS_PUBLIC_BASE_URL")
    assert acl.resolve_public_origin() == ("https://smtp.example.test", "smtp.reset_link_base_url")
    _set_smtp_base(env, "")
    assert acl.resolve_public_origin() == (None, "")


def test_legacy_alias_resolves_but_never_silently(env, capsys):
    """``SAAS_PUBLIC_BASE_URL`` keeps working (a deployment configured under the old name is not
    broken) but EVERY resolution through it logs a deprecation naming ``SAAS_MCP_PUBLIC_URL``."""
    env.setenv("SAAS_PUBLIC_BASE_URL", "https://legacy.example.test/")
    assert tcr._request_origin(_req({"host": "evil.example"})) == "https://legacy.example.test"
    crit = _crit_lines(capsys)
    assert any("DEPRECATED" in ln and "SAAS_PUBLIC_BASE_URL" in ln and "SAAS_MCP_PUBLIC_URL" in ln
               for ln in crit), crit
    # and the adapter's own door resolves to the SAME string, and logs the SAME deprecation
    assert payments_stripe._public_origin() == "https://legacy.example.test"
    assert any("DEPRECATED" in ln for ln in _crit_lines(capsys))


def test_smtp_reset_link_origin_is_the_explicit_last_operator_source(env):
    """Operator-entered (console SMTP settings), never request-derived; a path after the origin
    (``/dashboard/user.html``) is dropped because an origin has no path."""
    _set_smtp_base(env, "https://console.example.test:8443/dashboard/user.html?x=1")
    assert tcr._request_origin(_req({"x-forwarded-host": "evil.example"})) \
        == "https://console.example.test:8443"
    assert payments_stripe._public_origin() == "https://console.example.test:8443"


def test_smtp_base_that_is_not_a_url_is_ignored(env):
    _set_smtp_base(env, "console.example.test")          # no scheme
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "app.example.test"}, scheme="https")) \
        == "https://app.example.test"
    env.delenv("SAAS_TENANT_HOSTS")
    assert tcr._request_origin(_req({"host": "app.example.test"}, scheme="https")) == ""


def test_route_and_adapter_consult_the_same_resolver(env):
    """Both doors are the ONE function — not two implementations that happen to agree today."""
    import inspect
    assert "resolve_public_origin" in inspect.getsource(payments_stripe._public_origin)
    assert "resolve_public_origin" in inspect.getsource(tcr._request_origin)
    for cfg in ({"SAAS_MCP_PUBLIC_URL": "https://a.example.test"},
                {"SAAS_PUBLIC_BASE_URL": "https://b.example.test:8002"}):
        for var in _OPERATOR_ENVS:
            env.delenv(var, raising=False)
        for k, v in cfg.items():
            env.setenv(k, v)
        assert tcr._request_origin(_req({"host": "evil.example"})) == payments_stripe._public_origin()


def _provider(monkeypatch):
    """The REAL adapter with placeholder test credentials; ``stripe.checkout.Session.create`` is
    replaced by a recorder so a session is never created and the params are observable."""
    from saas.payments_stripe import StripePaymentProvider
    p = StripePaymentProvider(config={"provider": "stripe", "mode": "test", "configured": True,
                                      "currency": "CAD", "enabled_methods": ["card"],
                                      "api_key": "pk_test_XXX_placeholder",
                                      "api_secret": "sk_test_XXX_placeholder",
                                      "webhook_secret": "whsec_XXX_placeholder"})
    import stripe
    seen = {}

    class _FakeSession:
        id = "cs_test_origin"
        url = "https://checkout.stripe.com/c/cs_test_origin"

    def _create(**kw):
        seen.update(kw)
        return _FakeSession()
    monkeypatch.setattr(stripe.checkout.Session, "create", _create)
    return p, seen


def _checkout(p, success_url, cancel_url):
    return p.create_checkout(
        account_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        plan_id="team", amount=750.0, price_id="price_test_origin",
        success_url=success_url, cancel_url=cancel_url)


def test_full_path_forged_host_and_no_operator_source_is_refused_by_the_adapter(env, capsys):
    """THE ROUND-3 GAP, closed: with every operator source unset and a forged host, the route
    hands the adapter ``(None, None)`` and the adapter — consulting the SAME resolver — REFUSES
    with ``invalid_return_url``. No session is created, nothing is charged, and no relative url
    exists anywhere on the path."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    s, c = tcr._return_urls(_req({"x-forwarded-host": "evil.example", "x-forwarded-proto": "https"}))
    assert (s, c) == (None, None)
    p, seen = _provider(env)
    out = _checkout(p, s, c)
    assert out["status"] == "invalid_return_url", out
    assert out["charged"] is False and out["entitlement_changed"] is False
    assert out["checkout_url"] is None and out["session_id"] is None
    assert seen == {}, "a Stripe session must never be created without an absolute return url"
    crit = _crit_lines(capsys)
    assert any("invalid_return_url" in ln for ln in crit), crit


def test_full_path_with_only_the_legacy_alias_builds_absolute_urls_and_logs_deprecation(env, capsys):
    """Only ``SAAS_PUBLIC_BASE_URL`` set: the route resolves it (so the urls it passes are already
    absolute), the adapter would resolve the same, and the deprecation crit is logged."""
    env.setenv("SAAS_PUBLIC_BASE_URL", "https://legacy.example.test")
    s, c = tcr._return_urls(_req({"x-forwarded-host": "evil.example"}))
    _assert_absolute_pair(s, c, "https://legacy.example.test")
    p, seen = _provider(env)
    out = _checkout(p, s, c)
    assert out["status"] == "checkout_created", out
    assert seen["success_url"] == s and seen["cancel_url"] == c
    assert any("DEPRECATED" in ln and "SAAS_MCP_PUBLIC_URL" in ln for ln in _crit_lines(capsys))
    # the adapter's own default (urls omitted) lands on the same origin
    out2 = _checkout(p, None, None)
    assert out2["status"] == "checkout_created"
    assert seen["success_url"].startswith("https://legacy.example.test/")


def test_full_path_allowlisted_port_host_reaches_stripe_with_the_port(env):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    s, c = tcr._return_urls(_req({"host": "app.example.test:8002", "x-forwarded-proto": "https"}))
    p, seen = _provider(env)
    out = _checkout(p, s, c)
    assert out["status"] == "checkout_created"
    assert seen["success_url"].startswith("https://app.example.test:8002/dashboard/subscribe.html")
    assert seen["cancel_url"] == "https://app.example.test:8002" + tcr._RETURN_CANCEL


# ════════════════════════════════════════════════════════════════════════════════════
# ROUND 3 — port range, served-face suffix rule (SAAS_TENANT_BASE_DOMAINS)
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("port,ok", [("1", True), ("8002", True), ("65535", True),
                                     ("0", False), ("65536", False), ("99999", False)])
def test_port_must_be_1_to_65535(env, port, ok):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    got = tcr._request_origin(_req({"host": f"app.example.test:{port}"}, scheme="https"))
    assert got == (f"https://app.example.test:{port}" if ok else ""), (port, got)


def test_subdomain_of_auto_derived_tenant_base_is_a_served_face(env):
    """``SAAS_TENANT_HOSTS=app.example.test`` auto-derives base ``example.test`` (the face gate's
    own rule, lifted to ``host_rules.derive_tenant_base_domains``), so a per-tenant console
    ``acme.example.test`` — which the gate serves as the tenant face — is a valid return origin."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    assert tcr._request_origin(_req({"host": "acme.example.test:8002"}, scheme="https")) \
        == "https://acme.example.test:8002"
    assert tcr._request_origin(_req({"host": "example.test"}, scheme="https")) == "https://example.test"


def test_explicit_tenant_base_domains_are_served_faces(env):
    env.setenv("SAAS_TENANT_BASE_DOMAINS", "tenants.example")
    assert tcr._request_origin(_req({"host": "acme.tenants.example"}, scheme="https")) \
        == "https://acme.tenants.example"
    assert tcr._request_origin(_req({"host": "deep.acme.tenants.example"}, scheme="https")) \
        == "https://deep.acme.tenants.example"


@pytest.mark.parametrize("bad", [
    "evil.example",                    # unrelated
    "example.test.evil.example",       # base as a PREFIX of the attacker's host
    "notexample.test",                 # base as a SUFFIX without the dot boundary
    "tenants.example.evil.example",
])
def test_suffix_rule_requires_a_label_boundary(env, bad):
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test")
    env.setenv("SAAS_TENANT_BASE_DOMAINS", "tenants.example")
    assert tcr._request_origin(_req({"host": bad}, scheme="https")) == "", bad


def test_served_base_domains_match_the_face_gates_derivation(env):
    """Same lifted function, same inputs, same set — and ``http_server`` is bound to it.
    Round 4: an explicit ``SAAS_TENANT_BASE_DOMAINS`` REPLACES the auto-derive (it used to be
    unioned, which made the documented public-suffix remedy inert)."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.test, mcp.other.example")
    env.setenv("SAAS_TENANT_BASE_DOMAINS", "tenants.example")
    expect = host_rules.derive_tenant_base_domains(
        host_rules.parse_host_list("app.example.test, mcp.other.example"), "tenants.example")
    assert tcr._served_base_domains() == expect == frozenset({"tenants.example"})
    env.delenv("SAAS_TENANT_BASE_DOMAINS")
    assert tcr._served_base_domains() == frozenset({"example.test", "other.example"})
    import src.mcp.http_server as h
    assert "derive_tenant_base_domains" in __import__("inspect").getsource(h._derive_tenant_base_domains)


# ════════════════════════════════════════════════════════════════════════════════════
# The lifted parser: http_server's face gate and the checkout path share ONE implementation
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("App.Example.Test:8002", "app.example.test"),
    ("app.example.test.", "app.example.test"),
    ("app.example.test.:443", "app.example.test"),
    ("[::1]:8002", "[::1]"),
    ("", ""),
    ("  ", ""),
])
def test_strip_host_port_contract_is_unchanged_by_the_lift(raw, expected):
    assert host_rules.strip_host_port(raw) == expected


def test_no_second_parser_exists_in_the_checkout_module():
    """Respect-the-shape guard: the checkout module must not carry its own host splitter. If a
    ``rsplit(":"`` / ``.split(":"`` on a host appears here again, it is a copy-pasted parser."""
    import inspect
    src = inspect.getsource(tcr._request_origin) + inspect.getsource(tcr._allowlisted_hosts)
    assert 'rsplit(":"' not in src and 'split(":"' not in src, \
        "host splitting must go through src.mcp.host_rules, not a local rsplit"
