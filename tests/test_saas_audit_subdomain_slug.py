"""Adversarial security audit — subdomain face classification + account-slug impersonation guard.

Covers the two fixes landed during the audit of commits 6eebf9d / 50bc775e:

  1. `_strip_host_port` normalizes the FQDN trailing dot so `evil.faultline.ca.` cannot slip
     past the base-domain match and fall through to the ungated "internal" face (operator-console
     exposure landmine that commit 50bc775e set out to close).
  2. `admin._safe_slug_from_name` guards a derived account slug so it can never *become* a
     reserved-infra / brand subdomain (`www`/`google`.faultline.ca).

This file used to configure the host lists by writing ``os.environ`` inside the helper and then
``importlib.reload``-ing http_server. Both are process-wide: the reload rebuilt ``h.app`` and
re-read every import-time snapshot, so THIS file's `faultline.ca` host config silently became
the config for every module collected after it. The host frozensets are now set per test via
the ``saas_host_faces`` fixture (tests/conftest.py) — same values, same parsing path, no reload
and nothing that outlives the test.
"""

import pytest


# ── 1. subdomain face classification (http_server) ──────────────────────────────────

TENANT_HOSTS = "app.faultline.ca,mcp.faultline.ca"
ADMIN_HOSTS = "faultline-admin.tail1234.ts.net"
VOLENTI_HOSTS = "volenti.ca"


@pytest.fixture
def http_server(saas_host_faces):
    return saas_host_faces(admin=ADMIN_HOSTS, tenant=TENANT_HOSTS, volenti=VOLENTI_HOSTS)


class _Headers(dict):
    def get(self, k, d=None):
        return super().get(k.lower(), d)


class _Req:
    def __init__(self, host=None, xfh=None):
        hd = {}
        if host is not None:
            hd["host"] = host
        if xfh is not None:
            hd["x-forwarded-host"] = xfh
        self.headers = _Headers(hd)


@pytest.mark.parametrize("host", [
    "evil.faultline.ca.",        # FQDN trailing dot
    "faultline.ca.",             # apex trailing dot
    "EVIL.FAULTLINE.CA.",        # case + dot
    "evil.faultline.ca:8443",    # with port
])
def test_trailing_dot_and_port_still_classify_tenant(http_server, host):
    """A trailing-dot / ported host must NOT fall through to the ungated 'internal' face."""
    h = http_server
    assert h._classify_face(_Req(host=host)) == "tenant", (
        f"{host!r} escaped the subdomain gate → {h._classify_face(_Req(host=host))!r}")


def test_admin_host_matches_exactly(http_server):
    h = http_server
    assert h._classify_face(_Req(host="faultline-admin.tail1234.ts.net")) == "admin"
    assert h._classify_face(_Req(host="faultline-admin.tail1234.ts.net.")) == "admin"


def test_suffix_append_trick_is_not_tenant(http_server):
    h = http_server
    # faultline.ca.evil.com must NOT be captured as our tenant face.
    assert h._classify_face(_Req(host="faultline.ca.evil.com")) == "internal"
    assert h._classify_face(_Req(host="evil-faultline.ca")) == "internal"


# ── 2. account-slug impersonation guard (admin) ─────────────────────────────────────

def test_safe_slug_rewrites_reserved_and_brand(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    from saas import admin as _admin
    # reserved infra label
    assert _admin._safe_slug_from_name(None, "www").startswith("t-")
    assert _admin._safe_slug_from_name(None, "Mail").startswith("t-")
    # brand + lookalike
    assert _admin._safe_slug_from_name(None, "Google").startswith("t-")
    assert _admin._safe_slug_from_name(None, "paypa1").startswith("t-")
    # legit customer name is untouched
    assert _admin._safe_slug_from_name(None, "Acme Robotics") == "acme-robotics"
    # explicit operator-provided slug is guarded too
    assert _admin._safe_slug_from_name("operator", None).startswith("t-")
