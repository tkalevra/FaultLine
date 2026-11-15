"""Arbitrary *.faultline.ca subdomains must classify as the TENANT face (not "internal").

THE LANDMINE this pins: `_classify_face` used exact host matching only, so any subdomain not
explicitly in SAAS_{VOLENTI,TENANT,ADMIN}_HOSTS fell through to "internal" — where the surface
gate is a no-op and serves EVERYTHING, including the god-mode operator console. Proven on prod:
`evil-lookalike.faultline.ca/dashboard/index.html` → 200 (operator console) via force-resolve.
Not exploitable only because no wildcard DNS resolves those labels yet — but the moment a wildcard
record is added to make per-tenant subdomains work, every `*.faultline.ca` would expose the console.

The fix: any host that is (or is a subdomain of) a configured tenant base domain — auto-derived as
the last two labels of each SAAS_TENANT_HOSTS entry, overridable via SAAS_TENANT_BASE_DOMAINS —
classifies as TENANT (serves tenant content, 404s the operator console). Admin (a .ts.net MagicDNS
host) and volenti (a different registrable domain) are matched exactly first and never re-captured.

Pure unit tests of the classifier + base-domain derivation — no TestClient / DB.

Every test sets the host frozensets it needs via ``monkeypatch.setattr`` (see ``_prod_like``),
so this file needs NO module-level ``os.environ`` writes — those were process-wide, ran at
import, and stomped the sibling host-gate module's configuration for the whole session.

Run: python3 tools/fltest.py --bug SUBDOMAIN-FACE --test tests/test_saas_subdomain_face_gate.py
"""

from types import SimpleNamespace

from src.mcp import http_server as hs


def _req(host: str):
    return SimpleNamespace(headers={"host": host})


def _prod_like(monkeypatch):
    monkeypatch.setattr(hs, "SAAS_TENANT_HOSTS",
                        frozenset({"faultline.ca", "www.faultline.ca",
                                   "app.faultline.ca", "mcp.faultline.ca"}))
    monkeypatch.setattr(hs, "SAAS_ADMIN_HOSTS", frozenset({"faultline.tail43f80.ts.net"}))
    monkeypatch.setattr(hs, "SAAS_VOLENTI_HOSTS", frozenset({"volenti.ca", "www.volenti.ca"}))
    monkeypatch.setattr(hs, "SAAS_TENANT_BASE_DOMAINS", frozenset({"faultline.ca"}))


def test_arbitrary_subdomain_classifies_tenant_not_internal(monkeypatch):
    _prod_like(monkeypatch)
    # The landmine cases — previously "internal", must now be "tenant".
    assert hs._classify_face(_req("evil-lookalike.faultline.ca")) == "tenant"
    assert hs._classify_face(_req("acme.faultline.ca")) == "tenant"
    assert hs._classify_face(_req("g00gle.faultline.ca")) == "tenant"
    # A deep label too.
    assert hs._classify_face(_req("a.b.faultline.ca")) == "tenant"


def test_explicit_and_foreign_hosts_unchanged(monkeypatch):
    _prod_like(monkeypatch)
    assert hs._classify_face(_req("app.faultline.ca")) == "tenant"
    assert hs._classify_face(_req("faultline.ca")) == "tenant"
    assert hs._classify_face(_req("faultline.tail43f80.ts.net")) == "admin"   # NOT under .faultline.ca
    assert hs._classify_face(_req("volenti.ca")) == "volenti"
    # a genuinely foreign domain stays internal (no over-capture)
    assert hs._classify_face(_req("evil.example.com")) == "internal"
    # a lookalike REGISTRABLE domain (not our zone) is NOT our tenant face
    assert hs._classify_face(_req("faultline.ca.evil.com")) == "internal"


def test_base_domains_autoderive_from_tenant_hosts(monkeypatch):
    monkeypatch.setattr(hs, "SAAS_TENANT_HOSTS",
                        frozenset({"app.faultline.ca", "mcp.faultline.ca"}))
    monkeypatch.delenv("SAAS_TENANT_BASE_DOMAINS", raising=False)
    assert "faultline.ca" in hs._derive_tenant_base_domains()


def test_unconfigured_is_failsafe_internal(monkeypatch):
    monkeypatch.setattr(hs, "SAAS_TENANT_HOSTS", frozenset())
    monkeypatch.setattr(hs, "SAAS_TENANT_BASE_DOMAINS", frozenset())
    monkeypatch.setattr(hs, "SAAS_ADMIN_HOSTS", frozenset())
    monkeypatch.setattr(hs, "SAAS_VOLENTI_HOSTS", frozenset())
    # SAAS unconfigured → no base domains → behavior unchanged (internal, byte-for-byte).
    assert hs._classify_face(_req("anything.faultline.ca")) == "internal"


def test_explicit_env_override_adds_a_base(monkeypatch):
    monkeypatch.setenv("SAAS_TENANT_BASE_DOMAINS", "example.test")
    monkeypatch.setattr(hs, "SAAS_TENANT_HOSTS", frozenset())
    bases = hs._derive_tenant_base_domains()
    assert "example.test" in bases
