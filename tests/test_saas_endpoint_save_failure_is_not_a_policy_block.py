"""A STORAGE fault must never be dressed as a POLICY block — and a saved endpoint must never
be described as an empty form.

THE MEASURED DEFECT (2026-08-03, driving the REAL ``PUT /me/llm-endpoint`` for a brand-new
self-serve tenant against the local control-plane Postgres, ``DEV/review/_egress_journey_driver.py``):

    llm_config.cleartext_endpoint  host=142.198.45.51  scope=seat     <- the guard ALLOWED it
    PUT /me/llm-endpoint -> 500 {"detail": "failed to save config"}   <- and then this

The URL was a PUBLIC ``http://`` address; the egress guard passed it and logged the cleartext
notice one line earlier. What failed was the WRITE — the deployment had not applied migration
202 (``public.seat_llm_config``, 2026-07-31), so every self-serve individual's endpoint save hit
``UndefinedTable``. Two things made that a product outage rather than a deploy footnote:

  * the route's ``except Exception`` returned "failed to save config" with **no log line at
    all**, so the operator could not see the cause; and
  * to the tenant, an unexplained failure to add an endpoint is indistinguishable from being
    FORBIDDEN to add one — which is exactly how it was reported ("new tenants cannot add their
    own insecure endpoints").

The sibling defect is on the TEST button: ``_brain_resolve`` returns a bare ``None`` for four
different states, and all four rendered as "no routable brain config (set base_url + model
first)" — told to someone looking at the base_url and model they had just saved. That is also
the SAVE-vs-TEST divergence: the save path reports an ``EgressBlocked`` precisely, the test path
funnels the same verdict through ``None`` and renames it.

WHAT IS PINNED HERE
  1. a persistence fault raises ``ConfigStorageError`` (not a bare Exception) and is LOGGED;
  2. all three save doors answer it as OURS (503 + a sentence saying so), never "failed to
     save config", and never as a rejection of the endpoint;
  3. ``explain_unresolvable`` names the real state using the SAME ``validate_egress_url`` the
     save ran, so the two doors cannot report different verdicts on one URL;
  4. the SSRF half is UNTOUCHED — private/loopback/metadata still blocked absolutely, on every
     resolved address, and a tenant still cannot self-allowlist.

⚠️ (4) is not decoration. The whole point of the http/public split is that relaxing TRANSPORT
must not relax REACHABILITY; if these go red, the guard was weakened, not improved.

tests/ is gitignored (git add -f).
Run: python3 tools/fltest.py --bug EGRESSBLOCK \
        --test tests/test_saas_endpoint_save_failure_is_not_a_policy_block.py
"""

import pytest
from fastapi import HTTPException

from saas import llm_config as lc


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    """No operator allowlist anywhere in this file — a public endpoint must need none."""
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. A WRITE FAULT IS ITS OWN TYPE, AND IT IS LOUD
# ══════════════════════════════════════════════════════════════════════════════════════

class _ExplodingCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise RuntimeError('relation "seat_llm_config" does not exist')


class _ExplodingConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _ExplodingCursor()

    def commit(self):
        pass


@pytest.fixture
def broken_storage(monkeypatch):
    """The control plane accepts a connection and then fails the INSERT — migration-202 shape."""
    crits = []
    monkeypatch.setattr(lc.db, "connection", lambda *a, **k: _ExplodingConn())
    monkeypatch.setattr(lc, "_log_crit", lambda event, **kw: crits.append((event, kw)))
    monkeypatch.setattr(lc, "_log_warn", lambda event, **kw: None)
    monkeypatch.setattr(lc._crypto, "encrypt", lambda v: "ct")
    return crits


PUBLIC_HTTP = {"base_url": "http://142.198.45.51:8080", "model": "qwen3.5-9b",
               "backend_type": "openai"}


def test_a_failed_seat_write_raises_config_storage_error_not_a_bare_exception(broken_storage):
    with pytest.raises(lc.ConfigStorageError):
        lc.upsert_seat_config("11111111-1111-4111-8111-111111111111",
                              "22222222-2222-4222-8222-222222222222", dict(PUBLIC_HTTP))


def test_a_failed_account_write_raises_config_storage_error(broken_storage):
    with pytest.raises(lc.ConfigStorageError):
        lc.upsert_config("22222222-2222-4222-8222-222222222222", dict(PUBLIC_HTTP))


def test_the_write_fault_is_logged_loudly_with_the_cause_class(broken_storage):
    with pytest.raises(lc.ConfigStorageError):
        lc.upsert_seat_config("11111111-1111-4111-8111-111111111111",
                              "22222222-2222-4222-8222-222222222222", dict(PUBLIC_HTTP))
    assert broken_storage, "a dropped endpoint write produced NO log line — the original defect"
    event, kw = broken_storage[-1]
    assert event == "llm_config.upsert_seat_config_failed"
    assert kw.get("error_type") == "RuntimeError"
    assert kw.get("scope") == "seat"


def test_the_write_fault_log_carries_no_secret(broken_storage):
    with pytest.raises(lc.ConfigStorageError):
        lc.upsert_seat_config("11111111-1111-4111-8111-111111111111",
                              "22222222-2222-4222-8222-222222222222",
                              dict(PUBLIC_HTTP, api_key="sk-super-secret-value"))
    blob = repr(broken_storage)
    assert "sk-super-secret-value" not in blob
    assert "ct" not in blob.replace("error_type", "").replace("ConfigStorage", "")


def test_a_storage_fault_is_NOT_an_egress_block(broken_storage):
    """The two must be distinguishable by TYPE, or a caller re-tells one as the other."""
    assert not issubclass(lc.ConfigStorageError, lc.EgressBlocked)
    assert not issubclass(lc.EgressBlocked, lc.ConfigStorageError)


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. EVERY SAVE DOOR SAYS "OURS", NEVER "failed to save config"
# ══════════════════════════════════════════════════════════════════════════════════════

_INDIVIDUAL = {"id": "ind-1", "user_id": "11111111-1111-4111-8111-111111111111",
               "account_id": "22222222-2222-4222-8222-222222222222",
               "tenant_context": "none", "can_control_endpoint": True}


def _body():
    from saas.llm_admin import LLMConfigRequest
    return LLMConfigRequest(**PUBLIC_HTTP)


def _assert_ours(exc: HTTPException):
    assert exc.status_code == 503, f"expected 503 (ours, retryable), got {exc.status_code}"
    detail = str(exc.detail).lower()
    assert "failed to save config" not in detail, "the opaque wall is back"
    assert "our side" in detail, "the message must say the fault is OURS"
    assert "not with your endpoint" in detail, "it must clear the tenant's endpoint of blame"


def test_individual_console_save_reports_a_storage_fault_as_ours(monkeypatch):
    from saas import user_console as uc
    monkeypatch.setattr(uc, "_brain_upsert",
                        lambda ind, f: (_ for _ in ()).throw(lc.ConfigStorageError("boom")))
    monkeypatch.setattr(uc, "_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        uc.me_set_llm_endpoint(_body(), individual=_INDIVIDUAL)
    _assert_ours(ei.value)


def test_tenant_console_save_reports_a_storage_fault_as_ours(monkeypatch):
    from saas import tenant_console as tc
    monkeypatch.setattr(tc._llm_config, "upsert_config",
                        lambda a, f: (_ for _ in ()).throw(lc.ConfigStorageError("boom")))
    monkeypatch.setattr(tc, "_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        tc.tenant_set_llm_config(_body(), account_id=_INDIVIDUAL["account_id"])
    _assert_ours(ei.value)


def test_account_key_save_reports_a_storage_fault_as_ours(monkeypatch):
    from saas import llm_admin as la
    monkeypatch.setattr(la._llm_config, "upsert_config",
                        lambda a, f: (_ for _ in ()).throw(lc.ConfigStorageError("boom")))
    monkeypatch.setattr(la, "_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        la.set_llm_config(_body(), resolved=_INDIVIDUAL["account_id"])
    _assert_ours(ei.value)


def test_a_genuinely_blocked_url_is_still_a_400_rejection_not_a_503(monkeypatch):
    """The relaxation must not swallow the SSRF answer — a refusal is still a refusal."""
    from saas import user_console as uc
    from saas.llm_admin import LLMConfigRequest
    monkeypatch.setattr(uc, "_brain_seat_scoped", lambda ind: False)
    monkeypatch.setattr(uc._llm_config, "upsert_config", lc.upsert_config)
    with pytest.raises(HTTPException) as ei:
        uc.me_set_llm_endpoint(
            LLMConfigRequest(base_url="http://169.254.169.254/latest", model="m",
                             backend_type="openai"),
            individual=_INDIVIDUAL)
    assert ei.value.status_code == 400
    assert "rejected" in str(ei.value.detail).lower()


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. A SAVED ENDPOINT IS NEVER DESCRIBED AS AN EMPTY FORM
# ══════════════════════════════════════════════════════════════════════════════════════

def test_nothing_saved_still_says_set_base_url_and_model():
    assert lc.explain_unresolvable(None)[0] == lc.UNRESOLVABLE_NOT_CONFIGURED
    assert lc.explain_unresolvable({})[0] == lc.UNRESOLVABLE_NOT_CONFIGURED
    assert lc.explain_unresolvable(
        {"base_url": "http://142.198.45.51:8080"})[0] == lc.UNRESOLVABLE_NOT_CONFIGURED


def test_a_saved_but_blocked_url_is_named_as_blocked_not_as_unfilled():
    reason, detail = lc.explain_unresolvable(
        {"base_url": "http://10.0.0.5:8080", "model": "m"})
    assert reason == lc.UNRESOLVABLE_EGRESS_BLOCKED
    assert "base_url" in detail


def test_the_explainer_uses_the_same_guard_as_the_save_so_they_cannot_diverge():
    """One URL, one verdict. The save allows a public http endpoint; so must the explainer."""
    saved = {"base_url": "http://142.198.45.51:8080", "model": "m"}
    assert lc.egress_url_ok(saved["base_url"]) is True
    assert lc.explain_unresolvable(saved)[0] != lc.UNRESOLVABLE_EGRESS_BLOCKED


def test_an_undecryptable_key_is_named_as_ours_not_as_the_tenants_url():
    reason, _ = lc.explain_unresolvable(
        {"base_url": "http://142.198.45.51:8080", "model": "m", "api_key_set": True})
    assert reason == lc.UNRESOLVABLE_KEY_UNDECRYPTABLE


def test_the_explainer_never_raises():
    for junk in (None, {}, {"base_url": 5, "model": object()}, {"base_url": "::::"}):
        reason, _ = lc.explain_unresolvable(junk)
        assert reason in (lc.UNRESOLVABLE_NOT_CONFIGURED, lc.UNRESOLVABLE_EGRESS_BLOCKED,
                          lc.UNRESOLVABLE_KEY_UNDECRYPTABLE, lc.UNRESOLVABLE_UNKNOWN)


def test_the_test_button_message_names_the_blocked_url_not_an_unfilled_form(monkeypatch):
    from saas import user_console as uc
    monkeypatch.setattr(uc, "_brain_get_redacted",
                        lambda ind: {"base_url": "http://10.0.0.5:8080", "model": "m"})
    monkeypatch.setattr(uc, "_log", lambda *a, **k: None)
    msg = uc._unresolvable_detail(_INDIVIDUAL).lower()
    assert "set base_url + model first" not in msg, \
        "told a user with a saved base_url and model to go set a base_url and model"
    assert "permitted target" in msg


def test_the_test_button_still_says_set_them_when_nothing_is_saved(monkeypatch):
    from saas import user_console as uc
    monkeypatch.setattr(uc, "_brain_get_redacted", lambda ind: None)
    monkeypatch.setattr(uc, "_log", lambda *a, **k: None)
    assert "set base_url + model first" in uc._unresolvable_detail(_INDIVIDUAL)


def test_the_test_button_detail_is_fail_safe(monkeypatch):
    """A diagnostic must never be the reason the button stops working."""
    from saas import user_console as uc
    monkeypatch.setattr(uc, "_brain_get_redacted",
                        lambda ind: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(uc, "_log", lambda *a, **k: None)
    assert "no routable brain config" in uc._unresolvable_detail(_INDIVIDUAL)


# ══════════════════════════════════════════════════════════════════════════════════════
# 4. THE SSRF HALF IS UNTOUCHED  — if these go red the guard was WEAKENED
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080", "https://127.0.0.1:8080",
    "http://10.0.0.5:1234", "http://192.168.40.20:1234", "http://172.16.0.9:1234",
    "http://169.254.169.254/latest/meta-data/", "https://169.254.169.254/",
    "http://[::1]:8080", "http://[fd00::1]:8080", "http://[fec0::1]:8080",
    "http://100.64.0.1:8080",              # RFC 6598 CGNAT
    "http://[::ffff:10.0.0.5]:8080",       # v4-mapped wrapper
])
def test_internal_targets_still_blocked_absolutely(url):
    with pytest.raises(lc.EgressBlocked):
        lc.validate_egress_url(url)


def test_a_public_http_endpoint_still_needs_no_allowlist():
    cls = lc.validate_egress_url("http://142.198.45.51:8080")
    assert cls["cleartext"] is True and cls["public"] is True and cls["allowlisted"] is False


def test_one_private_answer_among_public_ones_still_rejects_the_whole_host(monkeypatch):
    import socket as _socket
    monkeypatch.setattr(_socket, "getaddrinfo", lambda host, *a, **k: [
        (2, 1, 6, "", ("142.198.45.51", 0)), (2, 1, 6, "", ("10.1.2.3", 0))])
    with pytest.raises(lc.EgressBlocked):
        lc.validate_egress_url("http://rebind.example.com:8080")


def test_a_tenant_still_cannot_self_allowlist():
    """The allowlist is read from the OPERATOR env only — no tenant-supplied path reaches it."""
    import inspect
    src = inspect.getsource(lc._allowed_networks)
    assert "SAAS_LLM_ALLOWED_EGRESS" in src and "os.environ" in src
    assert "SAAS_LLM_ALLOWED_EGRESS" not in inspect.getsource(lc.validate_egress_url) \
        or "os.environ" not in inspect.getsource(lc.validate_egress_url)
