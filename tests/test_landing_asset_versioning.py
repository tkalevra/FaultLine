"""LANDING ASSET CONTENT-HASH CACHE BUSTING — the 24h-stale-auth-card pins.

WHY THIS FILE EXISTS. Hit live 2026-11-02: the marketing site's assets are served with
long-lived caches (``public, max-age=86400, stale-while-revalidate`` at the edge), and the
HTML referenced ``landing.js`` with NO version query (index.html carried a hand-bumped
``?v=73`` counter; access.html carried nothing at all). After a prod deploy, Cloudflare
kept serving the OLD landing.js for its full TTL, so the new auth flow was invisible to
visitors for up to 24h — the HTML revalidated, but it validated against a body whose
asset URLs never change.

THE FIX IS AT THE HTML, NOT THE HEADERS. The long asset TTLs are correct once every URL
is fingerprinted, so ``src/mcp/http_server.py`` rewrites every LOCAL .js/.css reference in
a served landing page to ``path?v=<sha256[:12] of the file's bytes>``, computed from the
actual file at serve time and cached per (file, mtime) — never hashed per request. The
on-disk HTML is never modified, and the assets' own cache headers are left exactly as
they were.

TWO LAYERS ARE PINNED:

  UNIT      the pure helpers (``_landing_asset_version`` / ``_landing_version_asset_refs``)
            against throwaway files in ``tmp_path`` — hash shape, mtime cache, byte-change
            invalidation, non-local/missing/escaping references untouched, idempotence.
            These run everywhere (no DB, no server).

  SERVED    the real app via TestClient: the root route, /index.html, /access.html (and a
            subdirectory-asset page) serve content-hash-versioned markup, ``?v=`` is
            ignored downstream when fetching the asset bytes, the HTML shell revalidates
            by the SERVED-BODY etag (304 on match, 200 on stale), and HEAD stays
            body-less. The offline gate is neutralized at its own seam
            (``saas.platform_settings.offline_state`` — a per-request module-attribute
            read) because this environment has no control-plane DB and the gate
            fail-CLOSEDs to 503; the gate's real behaviour is pinned by the DSN-gated
            suites (test_saas_host_surface_gate et al.), not here.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re

import pytest

import src.mcp.http_server as h

REPO = pathlib.Path(__file__).resolve().parents[1]
LANDING = REPO / "saas" / "landing"


def _hash12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════════════════════════
# UNIT — the version helper
# ══════════════════════════════════════════════════════════════════════════════════════

def test_version_is_sha256_prefix_of_file_bytes(tmp_path):
    f = tmp_path / "asset.js"
    f.write_bytes(b"console.log('the bytes')")
    assert h._landing_asset_version(str(f)) == _hash12(f.read_bytes())
    assert len(h._landing_asset_version(str(f))) == 12


def test_version_changes_when_file_bytes_change(tmp_path):
    f = tmp_path / "landing.js"
    f.write_bytes(b"export const authFlow = 'old'")
    v_old = h._landing_asset_version(str(f))
    f.write_bytes(b"export const authFlow = 'new'")
    # Pin the mtime too: the cache is keyed by mtime, so the test must survive a
    # filesystem whose write already bumped it — the explicit utime makes it
    # deterministic on coarse-mtime systems as well.
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    v_new = h._landing_asset_version(str(f))
    assert v_new != v_old
    assert v_new == _hash12(f.read_bytes())


def test_version_is_cached_until_mtime_changes(tmp_path, monkeypatch):
    f = tmp_path / "asset.css"
    f.write_bytes(b"body { color: rebeccapurple }")
    calls = {"n": 0}
    real_sha256 = h.hashlib.sha256

    def counting_sha256(*args, **kwargs):
        calls["n"] += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(h.hashlib, "sha256", counting_sha256)

    v1 = h._landing_asset_version(str(f))
    assert h._landing_asset_version(str(f)) == v1
    assert h._landing_asset_version(str(f)) == v1
    assert calls["n"] == 1          # one hash per (file, mtime) — never per request

    st = os.stat(f)
    f.write_bytes(b"body { color: penguin }")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    v2 = h._landing_asset_version(str(f))
    assert v2 != v1
    assert calls["n"] == 2          # a changed mtime re-hashes exactly once
    assert h._landing_asset_version(str(f)) == v2
    assert calls["n"] == 2


def test_unreadable_file_yields_empty_version(tmp_path):
    assert h._landing_asset_version(str(tmp_path / "does-not-exist.js")) == ""


# ══════════════════════════════════════════════════════════════════════════════════════
# UNIT — the reference rewriter
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def mini_site(tmp_path):
    """A throwaway landing root: index.html + assets, incl. a subdirectory."""
    (tmp_path / "landing.js").write_bytes(b"// landing bundle")
    (tmp_path / "landing.css").write_bytes(b"/* landing styles */")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "wiki-shell.js").write_bytes(b"// wiki shell")
    (tmp_path / "index.html").write_text(
        "<html><head>\n"
        '  <link rel="stylesheet" href="landing.css">\n'
        '  <script src="landing.js?v=73"></script>\n'
        '  <script src="shared/wiki-shell.js" defer></script>\n'
        '  <link rel="icon" href="favicon.svg">\n'
        '  <script src="https://cdn.example.com/remote.js"></script>\n'
        '  <script src="//cdn.example.com/protocol-relative.js"></script>\n'
        '  <a href="#pricing">down</a> <a href="missing.js">gone</a>\n'
        '  <img src="../escape.js" alt="">\n'
        "</head><body></body></html>\n",
        encoding="utf-8")
    return tmp_path


def test_rewrite_versions_every_local_js_and_css_ref(mini_site):
    out = h._landing_version_asset_refs(
        (mini_site / "index.html").read_text(encoding="utf-8"), str(mini_site))
    assert f'href="landing.css?v={_hash12((mini_site / "landing.css").read_bytes())}"' in out
    assert f'src="landing.js?v={_hash12((mini_site / "landing.js").read_bytes())}"' in out
    assert (f'src="shared/wiki-shell.js?v='
            f'{_hash12((mini_site / "shared" / "wiki-shell.js").read_bytes())}"') in out
    assert '?v=73' not in out        # the hand-bumped counter is replaced, not kept


def test_rewrite_leaves_non_local_refs_untouched(mini_site):
    out = h._landing_version_asset_refs(
        (mini_site / "index.html").read_text(encoding="utf-8"), str(mini_site))
    assert 'src="https://cdn.example.com/remote.js"' in out
    assert 'src="//cdn.example.com/protocol-relative.js"' in out
    assert 'href="favicon.svg"' in out            # not .js/.css
    assert 'href="#pricing"' in out               # fragment, not a file
    assert 'href="missing.js"' in out             # does not exist on disk
    assert 'src="../escape.js"' in out            # escapes the landing root


def test_rewrite_is_idempotent(mini_site):
    text = (mini_site / "index.html").read_text(encoding="utf-8")
    once = h._landing_version_asset_refs(text, str(mini_site))
    twice = h._landing_version_asset_refs(once, str(mini_site))
    assert once == twice


def test_rewrite_handles_single_quoted_and_spaced_attributes(tmp_path):
    (tmp_path / "a.js").write_bytes(b"//a")
    out = h._landing_version_asset_refs(
        "<script src = 'a.js' ></script>", str(tmp_path))
    assert out == f"<script src = 'a.js?v={_hash12(b'//a')}' ></script>"


def test_rewrite_ignores_prefix_named_attributes(tmp_path):
    (tmp_path / "a.js").write_bytes(b"//a")
    html = ('<div data-src="a.js" data-href="a.js" srcset="a.js 1x" '

            'onload-src="a.js"></div>')
    out = h._landing_version_asset_refs(html, str(tmp_path))
    # The pin: the rewriter only ever touches bare src=/href= attributes — anything
    # else passes through verbatim (a future data-src must never be half-rewritten).
    assert out == html


# ══════════════════════════════════════════════════════════════════════════════════════
# SERVED — the real app (offline gate neutralized at its own seam; see module docstring)
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from saas import platform_settings as _platform_settings
    monkeypatch.setattr(_platform_settings, "offline_state",
                        lambda: {"offline": False, "message": ""})
    return TestClient(h.app)


def test_served_landing_index_carries_correct_content_hash(client):
    r = client.get("/")
    assert r.status_code == 200
    expected = _hash12((LANDING / "landing.js").read_bytes())
    assert f"/landing.js?v={expected}" in r.text
    # the hand-bumped counter this replaces must be gone from the SERVED page
    assert "?v=73" not in r.text


def test_served_root_and_index_html_are_byte_identical(client):
    root = client.get("/")
    indexed = client.get("/index.html")
    assert root.status_code == indexed.status_code == 200
    assert root.content == indexed.content


def test_served_access_page_rewrites_every_local_asset(client):
    r = client.get("/access.html")
    assert r.status_code == 200
    js = _hash12((LANDING / "landing.js").read_bytes())
    css = _hash12((LANDING / "landing.css").read_bytes())
    assert f"landing.js?v={js}" in r.text
    assert f"landing.css?v={css}" in r.text
    # and NO unversioned local asset ref survives on the served page
    for bare in ('src="landing.js"', 'href="landing.css"'):
        assert bare not in r.text


def test_served_body_is_disk_file_plus_version_queries_only(client):
    # The rewrite must be the ONLY mutation: serve-time output == the module's own
    # rewrite of the on-disk file, byte for byte, for BOTH served page kinds.
    for page in ("index.html", "access.html"):
        r = client.get(f"/{page}")
        assert r.status_code == 200
        expected = h._landing_version_asset_refs(
            (LANDING / page).read_text(encoding="utf-8"), str(LANDING))
        assert r.text == expected, page


def test_version_query_is_ignored_downstream(client):
    # The contract that makes ?v= safe: the query is invisible to asset resolution, so
    # ?v=<anything> — old hash, garbage — serves the CURRENT bytes.
    bare = client.get("/landing.js")
    assert bare.status_code == 200
    for junk in ("?v=73", "?v=whatever-old", "?v=" + "0" * 12):
        r = client.get(f"/landing.js{junk}")
        assert r.status_code == 200
        assert r.content == bare.content
    css = client.get("/landing.css")
    assert client.get("/landing.css?v=also-ignored").content == css.content


def test_served_subdirectory_assets_are_versioned(client):
    r = client.get("/wiki.html")
    assert r.status_code == 200
    shell = _hash12((LANDING / "shared" / "wiki-shell.js").read_bytes())
    assert f"shared/wiki-shell.js?v={shell}" in r.text


def test_served_html_revalidates_by_served_body_etag(client):
    r1 = client.get("/access.html")
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag
    r304 = client.get("/access.html", headers={"if-none-match": etag})
    assert r304.status_code == 304
    assert r304.content == b""
    assert r304.headers.get("etag") == etag
    # a stale validator (e.g. from before an asset edit re-hashed the refs) → full 200
    r200 = client.get("/access.html", headers={"if-none-match": '"stale-etag"'})
    assert r200.status_code == 200
    assert "?v=" in r200.text


def test_served_head_has_no_body_but_keeps_content_length(client):
    r = client.head("/access.html")
    assert r.status_code == 200
    assert r.content == b""
    get_len = client.get("/access.html").headers.get("content-length")
    assert r.headers.get("content-length") == get_len


def test_html_shell_stays_no_cache_and_asset_headers_are_untouched(client):
    # The shell keeps the pre-existing revalidate contract; the ASSETS' cache headers
    # are deliberately left exactly as the static layer served them before this change
    # (the long edge TTL is correct once URLs are versioned — do not "fix" it here).
    html = client.get("/access.html")
    assert html.headers.get("cache-control") == "no-cache"
    assert client.get("/landing.js").headers.get("cache-control") is None
    assert client.get("/landing.css").headers.get("cache-control") is None
