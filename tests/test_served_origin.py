"""``saas/acl.py::served_origin`` — ONE request-aware origin helper, and EVERY customer-facing
link builder that consumes it (gauntlet checkout-origin-authority, round 4).

The round-3 critic found that ``tenant_checkout_routes._request_origin`` had the allowlist while
``user_console.me_agent_chat_checkout`` built Stripe return urls from ``str(request.base_url)``
— the raw Host — as did the password-reset link builders and both connection-info pages, and the
operator ``/stripe/checkout`` accepted body-supplied return urls unchecked. One door with a lock
and four without is no lock. This file drives EVERY caller with a REAL Starlette ``Request``
carrying a forged ``Host`` (and, where a provider is involved, a recorder standing in for
``stripe.checkout.Session.create``) and asserts that none of them emits a non-served origin.

The full audit table (constraint 13 — every write path) is in
``DEV/review/gauntlet/checkout-origin-authority/round_4/REPORT.md``.

Also pinned here (subordinate findings from the same verdict):
  * an UNKNOWN host is never a served face — ``host_is_served_face`` does NOT inherit the face
    gate's ``internal`` fail-safe (which ``_gate_blocks`` never blocks);
  * explicit ``SAAS_TENANT_BASE_DOMAINS`` REPLACES the auto-derive (it used to be unioned, which
    made the documented public-suffix remedy inert); ``app.example.co.uk`` no longer derives
    ``co.uk`` at all;
  * ``:08002`` is echoed as ``:8002``; ``normalize_origin`` refuses ``?x=1`` / ``#frag`` /
    ``user:pw@`` / ``:abc`` instead of passing them silently.

Names are RFC 2606 / RFC 6761 reserved. No DB is touched: the SMTP operator source is pinned at
``saas.smtp_config.resolve_config``.

Run (NOT raw pytest):
    python3 tools/fltest.py --bug CO4 --test tests/test_served_origin.py
"""

import inspect

import pytest
from starlette.requests import Request

from saas import acl
from saas import onboarding
from saas import payments as _payments
from saas import payments_stripe
from saas import plans
from saas import smtp_config
from saas import stripe_routes
from saas import tenant_checkout_routes as tcr
from saas import tenant_console
from saas import user_console as uc
from src.mcp import host_rules

_OPERATOR_ENVS = ("SAAS_MCP_PUBLIC_URL", "SAAS_PUBLIC_BASE_URL", "MCP_PUBLIC_URL")
_FACE_ENVS = ("SAAS_TENANT_HOSTS", "SAAS_ADMIN_HOSTS", "SAAS_TENANT_BASE_DOMAINS")

FORGED = "evil.example"
SERVED = "app.example.test"


# ════════════════════════════════════════════════════════════════════════════════════
# A REAL Starlette Request (the callers read request.headers / request.url / request.base_url)
# ════════════════════════════════════════════════════════════════════════════════════

def real_request(host=FORGED, xfh=None, proto=None, scheme="http", path="/x"):
    headers = [(b"host", host.encode())]
    if xfh:
        headers.append((b"x-forwarded-host", xfh.encode()))
    if proto:
        headers.append((b"x-forwarded-proto", proto.encode()))
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "scheme": scheme, "headers": headers,
             "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 41234), "http_version": "1.1"}
    return Request(scope)


@pytest.fixture
def env(monkeypatch):
    for var in _OPERATOR_ENVS + _FACE_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(smtp_config, "resolve_config", lambda: None)
    return monkeypatch


def _crit(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if "CRIT" in ln or "[saas-acl]" in ln]


class _Recorder:
    """The REAL Stripe adapter with placeholder test credentials and Session.create replaced."""

    def __init__(self, monkeypatch):
        import stripe
        self.seen = {}
        self.provider = payments_stripe.StripePaymentProvider(config={
            "provider": "stripe", "mode": "test", "configured": True, "currency": "CAD",
            "enabled_methods": ["card"], "api_key": "pk_test_XXX_placeholder",
            "api_secret": "sk_test_XXX_placeholder", "webhook_secret": "whsec_XXX_placeholder"})

        class _S:
            id = "cs_test_r4"
            url = "https://checkout.stripe.com/c/cs_test_r4"

        def _create(**kw):
            self.seen.update(kw)
            return _S()
        monkeypatch.setattr(stripe.checkout.Session, "create", _create)
        monkeypatch.setattr(_payments, "get_payment_provider", lambda: self.provider)

    def assert_no_session(self):
        assert self.seen == {}, f"a Stripe session was created: {self.seen}"

    def assert_origin(self, origin):
        for k in ("success_url", "cancel_url"):
            assert self.seen[k].startswith(origin + "/"), (k, self.seen[k])
            assert FORGED not in self.seen[k]


def _sanity_real_request_reflects_the_forged_host():
    """The premise of every test below: a real Request's base_url IS the raw Host."""
    r = real_request()
    assert str(r.base_url) == f"http://{FORGED}/"


def test_premise_a_real_request_base_url_is_the_raw_host():
    _sanity_real_request_reflects_the_forged_host()


# ════════════════════════════════════════════════════════════════════════════════════
# served_origin itself
# ════════════════════════════════════════════════════════════════════════════════════

def test_served_origin_operator_first_then_served_face_then_refuse(env, capsys):
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test")
    assert acl.served_origin(real_request(xfh=FORGED)) == "https://console.example.test"
    env.delenv("SAAS_MCP_PUBLIC_URL")
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    assert acl.served_origin(real_request(host=f"{SERVED}:8002", proto="https")) \
        == f"https://{SERVED}:8002"
    assert acl.served_origin(real_request(host=FORGED)) == ""
    assert acl.served_origin(real_request(host=SERVED, xfh=FORGED)) == ""   # XFH read first
    assert any("SAAS_MCP_PUBLIC_URL" in ln and "SAAS_TENANT_HOSTS" in ln for ln in _crit(capsys))


def test_an_unknown_host_is_never_a_served_face_even_though_the_gate_calls_it_internal(env):
    """``http_server._classify_face`` → "internal" for an unknown host and ``_gate_blocks`` never
    blocks it (fail-SAFE for serving). ``host_is_served_face`` must NOT inherit that: a link to an
    unknown host is a link to whoever controls it (fail-CLOSED for linking)."""
    import src.mcp.http_server as h
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    with env.context() as m:
        m.setattr(h, "SAAS_TENANT_HOSTS", frozenset({SERVED}))
        m.setattr(h, "SAAS_ADMIN_HOSTS", frozenset())
        m.setattr(h, "SAAS_VOLENTI_HOSTS", frozenset())
        m.setattr(h, "SAAS_TENANT_BASE_DOMAINS", frozenset({"example.test"}))
        assert h._classify_face(real_request(host="unknown.example")) == "internal"
    assert host_rules.host_is_served_face("unknown.example", acl.served_hosts(),
                                          acl.served_base_domains()) is False
    assert acl.served_origin(real_request(host="unknown.example")) == ""
    # and with NOTHING configured, nothing is served
    env.delenv("SAAS_TENANT_HOSTS")
    assert host_rules.host_is_served_face(SERVED, frozenset(), frozenset()) is False
    assert acl.served_origin(real_request(host=SERVED)) == ""


def test_padded_port_is_canonicalized_not_echoed(env):
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    assert acl.served_origin(real_request(host=f"{SERVED}:08002", proto="https")) \
        == f"https://{SERVED}:8002"
    assert acl.normalize_origin("https://h.example:08002") == "https://h.example:8002"


@pytest.mark.parametrize("junk", [
    "https://h.example?x=1", "https://h.example#frag", "https://user:pw@h.example",
    "https://h.example:abc", "https://h.example:0", "https://h.example:65536",
    "https://h.example/dashboard", "https://h.exa mple", "https://h.example:8002/",
])
def test_normalize_origin_refuses_query_fragment_userinfo_and_bad_ports(junk):
    got = acl.normalize_origin(junk)
    # the ONE tolerated form is a trailing slash on a bare origin (outer whitespace is trimmed)
    if junk.endswith(":8002/"):
        assert got == "https://h.example:8002"
    else:
        assert got is None, (junk, got)


def test_operator_env_with_junk_shape_is_refused_loudly_not_passed(env, capsys):
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test/?x=1")
    assert acl.resolve_public_origin() == (None, "")
    assert any("not a web origin" in ln for ln in _crit(capsys))


# ════════════════════════════════════════════════════════════════════════════════════
# Public-suffix auto-derive and REPLACE semantics
# ════════════════════════════════════════════════════════════════════════════════════

def test_public_suffix_host_derives_no_base_so_evil_co_uk_is_refused(env):
    """``SAAS_TENANT_HOSTS=app.example.co.uk`` used to derive ``co.uk`` (everything under a
    public suffix = everybody). Now it derives NOTHING: the explicit host serves, ``evil.co.uk``
    does not."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.co.uk")
    assert acl.served_base_domains() == frozenset()
    assert host_rules.auto_derived_base("app.example.co.uk") == ""
    assert acl.served_origin(real_request(host="app.example.co.uk", proto="https")) \
        == "https://app.example.co.uk"
    assert acl.served_origin(real_request(host="evil.co.uk", proto="https")) == ""
    assert acl.served_origin(real_request(host="acme.example.co.uk", proto="https")) == ""


@pytest.mark.parametrize("label", sorted(host_rules.PUBLIC_SUFFIX_SECOND_LABELS))
def test_every_public_suffix_second_label_is_refused_as_a_base(label):
    assert host_rules.auto_derived_base(f"app.example.{label}.zz") == ""


def test_explicit_base_domains_replace_the_auto_derive(env):
    """The documented remedy for a public-suffix host is now EFFECTIVE: set the env and ONLY it
    applies — the naive auto-derive is not unioned back in."""
    env.setenv("SAAS_TENANT_HOSTS", "app.example.co.uk, app.other.example")
    env.setenv("SAAS_TENANT_BASE_DOMAINS", "example.co.uk")
    assert acl.served_base_domains() == frozenset({"example.co.uk"})
    assert acl.served_origin(real_request(host="acme.example.co.uk", proto="https")) \
        == "https://acme.example.co.uk"
    assert acl.served_origin(real_request(host="evil.co.uk", proto="https")) == ""
    # other.example was auto-derivable but the explicit env REPLACES, so it is NOT a base now
    assert acl.served_origin(real_request(host="x.other.example", proto="https")) == ""
    assert acl.served_origin(real_request(host="app.other.example", proto="https")) \
        == "https://app.other.example"          # the explicit host itself still serves
    # http_server derives through the same function with the same semantics
    import src.mcp.http_server as h
    assert h._derive_tenant_base_domains() == frozenset({"example.co.uk"})


# ════════════════════════════════════════════════════════════════════════════════════
# EVERY caller — forged Host, real Request, none may emit a non-served origin
# ════════════════════════════════════════════════════════════════════════════════════

# 1. tenant subscribe route → _return_urls → tenant_checkout.begin_subscription (963) → adapter
def test_caller_tenant_subscribe_return_urls(env):
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    assert tcr._return_urls(real_request(host=FORGED, proto="https")) == (None, None)
    assert tcr._return_urls(real_request(host=SERVED, xfh=FORGED, proto="https")) == (None, None)
    s, c = tcr._return_urls(real_request(host=f"{SERVED}:8002", proto="https"))
    assert s.startswith(f"https://{SERVED}:8002/") and c.startswith(f"https://{SERVED}:8002/")
    assert tcr._request_origin is not None and "served_origin" in inspect.getsource(tcr._request_origin)


# 2. user_console.me_agent_chat_checkout (2439) — THE round-3 gap
def _arm_agent_chat(monkeypatch):
    from saas import agent_entitlement as _ae
    monkeypatch.setattr(_ae, "reconcile_seat_addons_from_stripe", lambda **kw: None)
    monkeypatch.setattr(_ae, "seat_addon_state",
                        lambda a, u: {"readable": True, "enabled": False, "eligible": True,
                                      "seat_type": "agent"})
    monkeypatch.setattr(uc, "_entitlements_addon_price", lambda b: 25.0)
    monkeypatch.setattr(plans, "agent_addon_price_id", lambda b: "price_test_addon")


def test_caller_agent_chat_checkout_forged_host_is_refused_by_the_adapter(env, capsys):
    """Forged Host, no operator source, faces configured → origin '' → urls None → the REAL
    adapter refuses ``invalid_return_url``. Before round 4: ``https://evil.example/dashboard/
    user.html?agent_chat=success&session_id={CHECKOUT_SESSION_ID}`` went to Stripe."""
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    _arm_agent_chat(env)
    rec = _Recorder(env)
    ind = {"account_id": "00000000-0000-0000-0000-0000000000aa",
           "user_id": "00000000-0000-0000-0000-0000000000bb", "email": "x@example.test"}
    out = uc.me_agent_chat_checkout(uc.AgentChatCheckoutRequest(bucket=1),
                                    real_request(host=FORGED, proto="https"), ind)
    assert out["status"] == "invalid_return_url", out
    assert out["charged"] is False and out["entitlement_changed"] is False
    rec.assert_no_session()
    assert "request.base_url" not in inspect.getsource(uc.me_agent_chat_checkout).replace(
        "``str(request.base_url)``", "")


def test_caller_agent_chat_checkout_served_and_operator_origins_reach_stripe(env):
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    _arm_agent_chat(env)
    rec = _Recorder(env)
    ind = {"account_id": "00000000-0000-0000-0000-0000000000aa",
           "user_id": "00000000-0000-0000-0000-0000000000bb", "email": "x@example.test"}
    out = uc.me_agent_chat_checkout(uc.AgentChatCheckoutRequest(bucket=1),
                                    real_request(host=f"{SERVED}:8002", proto="https"), ind)
    assert out["status"] == "checkout_created"
    rec.assert_origin(f"https://{SERVED}:8002")
    assert "{CHECKOUT_SESSION_ID}" in rec.seen["success_url"]
    # operator origin beats a forged header outright
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test")
    rec2 = _Recorder(env)
    out = uc.me_agent_chat_checkout(uc.AgentChatCheckoutRequest(bucket=1),
                                    real_request(host=FORGED, xfh=FORGED), ind)
    assert out["status"] == "checkout_created"
    rec2.assert_origin("https://console.example.test")


# 3. stripe_routes.begin_checkout (104-108) — BODY-supplied urls, client-controlled
def _body(success, cancel):
    return stripe_routes.CheckoutRequest(
        account_id="00000000-0000-0000-0000-0000000000aa",
        user_id="00000000-0000-0000-0000-0000000000bb", plan_id="pro", amount=49.0,
        price_id="price_test_op", success_url=success, cancel_url=cancel)


def test_caller_operator_checkout_body_urls_must_match_the_served_origin(env):
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    rec = _Recorder(env)
    # forged body urls, served request host → refused with the adapter's own shape
    out = stripe_routes.begin_checkout(
        _body(f"https://{FORGED}/ok?session_id={{CHECKOUT_SESSION_ID}}", f"https://{FORGED}/no"),
        real_request(host=SERVED, proto="https"), True)
    assert out["status"] == "invalid_return_url" and out["charged"] is False
    assert out["session_id"] is None
    rec.assert_no_session()
    # ONE forged url among two is still refused
    out = stripe_routes.begin_checkout(
        _body(f"https://{SERVED}/ok", f"https://{FORGED}/no"),
        real_request(host=SERVED, proto="https"), True)
    assert out["status"] == "invalid_return_url"
    rec.assert_no_session()
    # a lookalike origin (served host as a prefix of the attacker's) is refused — origin equality
    out = stripe_routes.begin_checkout(
        _body(f"https://{SERVED}.{FORGED}/ok", f"https://{SERVED}.{FORGED}/no"),
        real_request(host=SERVED, proto="https"), True)
    assert out["status"] == "invalid_return_url"
    rec.assert_no_session()
    # body urls on the served origin (port included) → accepted, passed through verbatim
    out = stripe_routes.begin_checkout(
        _body(f"https://{SERVED}:8002/ok?session_id={{CHECKOUT_SESSION_ID}}",
              f"https://{SERVED}:8002/no"),
        real_request(host=f"{SERVED}:8002", proto="https"), True)
    assert out["status"] == "checkout_created", out
    assert rec.seen["success_url"] == f"https://{SERVED}:8002/ok?session_id={{CHECKOUT_SESSION_ID}}"


def test_caller_operator_checkout_body_urls_with_no_served_origin_are_refused(env):
    """Nothing configured, forged Host, body urls on that same forged host — the request host
    and the body agree with each other and NEITHER is ours. Refused."""
    rec = _Recorder(env)
    out = stripe_routes.begin_checkout(
        _body(f"https://{FORGED}/ok", f"https://{FORGED}/no"), real_request(host=FORGED), True)
    assert out["status"] == "invalid_return_url"
    rec.assert_no_session()


def test_caller_operator_checkout_omitted_body_urls_use_the_operator_default_or_refuse(env):
    rec = _Recorder(env)
    out = stripe_routes.begin_checkout(_body(None, None), real_request(host=FORGED), True)
    assert out["status"] == "invalid_return_url"          # adapter default: no operator source
    rec.assert_no_session()
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test")
    rec = _Recorder(env)
    out = stripe_routes.begin_checkout(_body(None, None), real_request(host=FORGED), True)
    assert out["status"] == "checkout_created"
    rec.assert_origin("https://console.example.test")


# 4. plans.py (547) — passes NO urls: the adapter default = operator sources only, no request
def test_caller_plans_upgrade_has_no_request_and_the_adapter_default_is_operator_only(env):
    src = inspect.getsource(plans)
    call = src[src.index("provider.create_checkout("):]
    call = call[:call.index(")")]
    assert "success_url" not in call and "cancel_url" not in call
    rec = _Recorder(env)
    out = rec.provider.create_checkout(account_id="a", user_id="u", plan_id="pro", amount=1.0,
                                       price_id="price_x")
    assert out["status"] == "invalid_return_url"
    rec.assert_no_session()
    env.setenv("SAAS_PUBLIC_BASE_URL", "https://legacy.example.test")   # legacy alias still works
    out = rec.provider.create_checkout(account_id="a", user_id="u", plan_id="pro", amount=1.0,
                                       price_id="price_x")
    assert out["status"] == "checkout_created"
    rec.assert_origin("https://legacy.example.test")


# 5. user_console reset-link builders (1843 / 1862) + _dashboard_reset_link — WSTG-INPV-17
def test_caller_reset_links_never_carry_the_forged_host(env, capsys):
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    r = real_request(host=FORGED, proto="https")
    assert uc._reset_link_for(r, "tok", None) == ""
    assert uc._approver_dashboard_base(r, None, "tenant.html") == ""
    assert uc._dashboard_reset_link(r, "tok", None) == ""
    assert any("reset link NOT built" in ln or "approver link NOT built" in ln for ln in _crit(capsys))
    # served host → built on it, port kept
    r = real_request(host=f"{SERVED}:8002", proto="https")
    assert uc._reset_link_for(r, "tok", None) == f"https://{SERVED}:8002/dashboard/user.html?token=tok"
    assert uc._dashboard_reset_link(r, "tok", None) == f"https://{SERVED}:8002/dashboard/tenant.html?token=tok"
    # the operator's SMTP reset base still wins outright (a full page url, operator-set)
    assert uc._reset_link_for(real_request(host=FORGED), "tok",
                              "https://console.example.test/dashboard/user.html?src=mail") \
        == "https://console.example.test/dashboard/user.html?src=mail&token=tok"
    assert uc._approver_dashboard_base(real_request(host=FORGED), "https://console.example.test/dashboard/user.html", "tenant.html") \
        == "https://console.example.test/dashboard/tenant.html"
    # the canonical env is consulted BEFORE any request host
    env.setenv("SAAS_MCP_PUBLIC_URL", "https://console.example.test")
    assert uc._reset_link_for(real_request(host=FORGED), "tok", None) \
        == "https://console.example.test/dashboard/user.html?token=tok"


def test_caller_reset_email_paths_do_not_send_when_no_link_can_be_built(env):
    """The four call sites skip the send on ''. Pinned structurally: every ``_reset_link_for`` /
    ``_dashboard_reset_link`` / ``_approver_dashboard_base`` call in user_console + tenant_console
    is followed by an ``if not link`` guard before any ``send_``."""
    import re
    for mod in (uc, tenant_console):
        src = inspect.getsource(mod)
        for m in re.finditer(r"\n(\s+)link = (?:_user_console\.)?(_reset_link_for|_dashboard_reset_link|"
                             r"_approver_dashboard_base)\(", src):
            tail = src[m.end():m.end() + 400]
            assert "if not link" in tail.split("send_")[0], (mod.__name__, m.group(2), tail[:120])


# 6. user_console.me_connection_info (2715) and tenant_console.tenant_connection_info (4067)
def test_caller_connection_info_pages_never_reflect_the_forged_host(env):
    ind = {"user_id": "00000000-0000-0000-0000-0000000000bb", "email": "x@example.test"}
    out = uc.me_connection_info(real_request(host=FORGED, proto="https"), ind)
    assert FORGED not in out["mcp_endpoint"] and FORGED not in out["base_url"]
    assert out["public_url_configured"] is False and out.get("public_url_note")
    assert out["base_url"] == onboarding.public_mcp_url()      # the documented placeholder
    env.setenv("SAAS_TENANT_HOSTS", SERVED)
    out = uc.me_connection_info(real_request(host=f"{SERVED}:8002", proto="https"), ind)
    assert out["mcp_endpoint"] == f"https://{SERVED}:8002/mcp"
    out = uc.me_connection_info(real_request(host=FORGED, xfh=FORGED, proto="https"), ind)
    assert FORGED not in out["mcp_endpoint"]


def test_caller_tenant_connection_info_never_reflects_the_forged_host(env, monkeypatch):
    monkeypatch.setattr(onboarding, "client_config_templates", lambda base: {"base": base})
    out = tenant_console.tenant_connection_info(real_request(host=FORGED, proto="https"),
                                                "00000000-0000-0000-0000-0000000000aa")
    assert FORGED not in out["mcp_endpoint"] and FORGED not in str(out["clients"])
    assert out["public_url_configured"] is False
    env.setenv("SAAS_ADMIN_HOSTS", "fcontrol.example.test")
    out = tenant_console.tenant_connection_info(
        real_request(host="fcontrol.example.test:8002", proto="https"),
        "00000000-0000-0000-0000-0000000000aa")
    assert out["mcp_endpoint"] == "https://fcontrol.example.test:8002/mcp"


# ════════════════════════════════════════════════════════════════════════════════════
# The census: no raw request.base_url origin builder survives in saas/
# ════════════════════════════════════════════════════════════════════════════════════

def test_no_raw_base_url_origin_builder_survives_in_saas():
    """``str(request.base_url)`` as an ORIGIN SOURCE is the defect class. Every occurrence in
    ``saas/`` must now be inside a comment or docstring, never executable."""
    import ast
    import pathlib
    root = pathlib.Path(acl.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "base_url" \
                    and isinstance(node.value, ast.Name) and node.value.id == "request":
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders


def test_every_create_checkout_caller_is_accounted_for():
    """The round-3 pin enumerated TWO callers and missed two. This walks the tree: every
    ``.create_checkout(`` call in saas/ must be one of the four audited sites."""
    import pathlib
    import re
    root = pathlib.Path(acl.__file__).parent
    found = set()
    for path in sorted(root.glob("*.py")):
        for m in re.finditer(r"\.create_checkout\(", path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8")[:m.start()].count("\n") + 1
            src_line = path.read_text(encoding="utf-8").splitlines()[line - 1]
            if src_line.lstrip().startswith(("#", '"', "'")) or "TypeError" in src_line:
                continue
            found.add(path.name)
    assert found == {"stripe_routes.py", "tenant_checkout.py", "plans.py", "user_console.py"}, found
