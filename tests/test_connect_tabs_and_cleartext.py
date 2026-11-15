"""Tenant console — connect-tab scoping, the key control step 4 points at, and the cleartext
disclaimer on BOTH endpoint fields.

WHY THIS FILE EXISTS. Two defects shipped together and are the same class of bug:

  1. TAB BLEED. `renderConnectPresenter()` did an unconditional `classList.remove('hidden')` on
     BOTH the account-key block and the standalone-client callout ("nothing is gated by the
     picker"), so the Claude-Desktop copy rendered on the OpenWebUI tab. Not a stray paragraph —
     a container that was never scoped.
  2. A DEAD REFERENCE. Step 4 said "generate one below". The block it named was REMOVED from this
     page in `2c1be320` (moved to Seats) and the JS kept a `if (kb)`-guarded reference to the id,
     so the guard hid the breakage. An instruction pointing at a control that is not there is
     worse than no instruction.

Both are only visible by RENDERING. A static grep proves neither: the text was present in the
HTML in both the broken and the fixed build — what changed is which container it lives in and
when that container is un-hidden.

⚠️ jsdom IS NOT A BROWSER. It runs the real `tenant.html`, the real `feature_copy.js` and the
real `tenant.js` painters against the REAL server-side copy (`saas.onboarding`), but it does not
lay out and does not apply CSS. It proves the painter puts this content in this container and
takes it away again; it does not prove pixels. The static assertions below hold with or without
it and never skip.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_connect.js"


def _read(name: str) -> str:
    return (DASH / name).read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════════════════
# RENDERED — the real page, the real painters, the real server copy
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def rendered():
    node = shutil.which("node")
    if not node or not HARNESS.exists():
        pytest.skip("node/jsdom harness unavailable — static assertions below still hold")
    from saas.onboarding import client_config_templates
    payload = json.dumps(client_config_templates("https://faultline.example.com:8002"))
    env = dict(os.environ)
    env["NODE_PATH"] = str(HARNESS.parent / "node_modules")
    try:
        out = subprocess.run([node, str(HARNESS), payload], capture_output=True,
                             text=True, timeout=120, cwd=str(ROOT), env=env)
    except Exception as exc:                      # pragma: no cover
        pytest.skip(f"jsdom render failed to launch: {exc}")
    if out.returncode != 0:
        pytest.fail(f"jsdom render failed:\n{out.stderr[-4000:]}")
    return json.loads(out.stdout)


# ── BUG 1: the tab bleed ─────────────────────────────────────────────────────────────

def test_claude_desktop_block_is_absent_from_the_openwebui_tab(rendered):
    """The reported bug, in the words the owner read on screen."""
    owui = rendered["tabs"]["openwebui"]["visible_text"]
    assert "Standalone clients" not in owui
    assert "Connect Claude Desktop" not in owui
    assert ".mcpb" not in owui
    assert rendered["tabs"]["openwebui"]["userconn_hidden"] is True


def test_claude_desktop_block_is_present_on_the_claude_desktop_tab(rendered):
    """The other half. Scoping the container must not DELETE the content — that would have been
    the wrong fix, and it would have taken the .mcpb pointer down with it."""
    cd = rendered["tabs"]["claude_desktop"]
    assert cd["userconn_hidden"] is False
    assert "Standalone clients" in cd["visible_text"]
    assert ".mcpb" in cd["visible_text"]


def test_the_generic_tab_does_not_inherit_the_claude_desktop_block_either(rendered):
    """The root cause was an unconditional un-hide, so it bled onto EVERY tab, not just the one
    that was reported. Fixing only the reported tab would leave this red."""
    g = rendered["tabs"]["generic"]
    assert g["userconn_hidden"] is True
    assert "Standalone clients" not in g["visible_text"]


# ── BUG 2: the dead reference ────────────────────────────────────────────────────────

@pytest.mark.parametrize("client", ["openwebui", "generic"])
def test_the_key_control_step_4_names_actually_exists_on_the_page(rendered, client):
    """The instruction names a control; this asserts that control is present, reachable on this
    tab and WIRED — the build before 4e5995e9 failed all three while the sentence still shipped.

    2026-08-03: the control it names CHANGED. It used to be a second "＋ new connection key"
    MINT button living on Get started, while the same key's list / rotate / revoke / last-used
    lived on Seats — so the page named one home and minted into another. Get started now carries
    a POINTER ("Seats → Connection key") and the instruction in saas/onboarding.py names that.
    The test still asks the same question: does the sentence name something real and wired?"""
    t = rendered["tabs"][client]
    assert t["genkey_present"] is True, "no key control on the page at all"
    assert t["genkey_reachable"] is True, "the key control exists but is not on this tab"
    assert t["genkey_wired"] is True, "the key control has no click handler"
    assert "Connection key" in t["genkey_label"]
    # ...and the instruction and the control must be reading the same name.
    assert "Seats \u2192 Connection key" in t["visible_text"]


@pytest.mark.parametrize("client", ["openwebui", "generic"])
def test_get_started_points_at_the_key_control_instead_of_duplicating_it(rendered, client):
    """OWNER: "GET STARTED verbiage is weird and DUPLICATES the control instead of linking to it."

    Two halves, and BOTH have to hold or the pointer is a lie:
      1. no rival MINT control on this tab, and
      2. the destination the copy names is a real element on the page (the third dead reference
         this file exists to prevent).
    """
    t = rendered["tabs"][client]
    assert t["mint_control_on_tab"] is False, (
        "Get started has grown a key-minting control again — the key's list/rotate/revoke live "
        "on Seats, so a key minted here lands in a table this tab cannot show"
    )
    assert t["keys_destination_exists"] is True, (
        "the copy points at Seats \u2192 Connection key but #seats-keys-panel does not exist"
    )


def test_a_token_client_is_not_offered_an_account_key_control(rendered):
    """Governance: a seat that cannot use a thing must not be shown a control for it. An flk_
    account key is meaningless to Claude Desktop — it authenticates as a per-user flu_ token."""
    cd = rendered["tabs"]["claude_desktop"]
    assert cd["keyblock_hidden"] is True
    assert cd["genkey_reachable"] is False


def test_no_tab_tells_the_reader_to_generate_a_key_below_a_control_that_is_not_there():
    """The literal dead-reference string, killed at its ONE source (saas/onboarding.py)."""
    from saas.onboarding import client_config_templates
    for cid, c in client_config_templates("https://x.example").items():
        blob = " ".join(c["instructions"])
        assert "generate one below" not in blob, cid
        assert "generate one above" not in blob, cid


def test_the_claude_desktop_tab_points_at_where_the_builder_ACTUALLY_is():
    """Same defect class, and this sentence has now been wrong TWICE: first "Go to Seats" (the
    builder was never there), then "at the top of this page" (true until the builder was moved
    into this tab). It must name the builder's current home."""
    from saas.onboarding import client_config_templates
    steps = " ".join(client_config_templates("https://x.example")["claude_desktop"]["instructions"])
    assert "on this tab" in steps
    assert "Go to Seats" not in steps
    assert "top of this page" not in steps


# ── the page header must be UNIVERSAL: no client gets a flagship slot above the tabs ─────

def test_the_page_header_is_identical_on_every_tab(rendered):
    """Everything above the client tab strip applies to all three clients — the heading, the
    API-key starter PDF, and the ONE read-only MCP endpoint (`/tenant/connection-info` returns a
    single `mcp_endpoint` shared by every client). If switching tabs changes the header,
    something client-specific has leaked back into it."""
    h = rendered["header"]
    assert h["openwebui"] == h["claude_desktop"] == h["generic"], \
        "the header changed with the tab — a client-specific block is above the strip"
    # ...and it is the universal set, not an empty shell.
    assert "Your MCP endpoint" in h["openwebui"]
    assert "What are you connecting?" in h["openwebui"]


def test_no_client_specific_control_sits_above_the_tab_strip(rendered):
    """The owner's ruling: a flagship slot for ONE client above the tabs is the same bias the
    tab body had, one level up. Presence in the header is the thing to assert — hiding it there
    would still be the wrong container."""
    assert rendered["header"]["mcpb_ids_in_header"] == [], \
        "the .mcpb builder (or part of it) is back above the tab strip"


def test_the_mcpb_builder_lives_on_the_claude_desktop_tab_and_works_there(rendered):
    """Moved, not orphaned: the seat picker and the build button must both be reachable ON this
    tab and the button still wired to its handler."""
    cd = rendered["tabs"]["claude_desktop"]
    assert cd["mcpb_hidden"] is False
    assert cd["seat_picker_reachable"] is True
    assert cd["mcpb_build_reachable"] is True
    assert cd["mcpb_build_wired"] is True
    # the whole path, on one tab: steps → builder → what to do with the download
    txt = cd["visible_text"]
    assert "Build & download .mcpb" in txt
    assert "Install" in txt


@pytest.mark.parametrize("client", ["openwebui", "generic"])
def test_the_mcpb_builder_is_absent_from_the_other_two_tabs(rendered, client):
    t = rendered["tabs"][client]
    assert t["mcpb_hidden"] is True
    assert t["mcpb_build_reachable"] is False
    assert ".mcpb" not in t["visible_text"]


def test_the_builder_is_gated_on_a_DECLARED_CAPABILITY_not_a_hardcoded_client_id():
    """`install_bundle` is declared server-side (one source, saas/onboarding.py) so a second
    bundle-installing client would just declare it. A `=== 'claude_desktop'` literal in the
    console would be the enumerate-the-world pattern this codebase forbids."""
    from saas.onboarding import client_config_templates
    t = client_config_templates("https://x.example")
    assert t["claude_desktop"].get("install_bundle") == "mcpb"
    assert "install_bundle" not in t["openwebui"] and "install_bundle" not in t["generic"]
    src = _read("tenant.js")
    gate = src[src.index("const mb = $('cp-mcpb')"):]
    gate = gate[:gate.index("\n}")]
    assert "install_bundle === 'mcpb'" in gate
    assert "claude_desktop" not in gate, "the builder gate hardcodes a client id"


def test_the_standalone_callout_no_longer_points_at_the_old_header_slot():
    """It said "use Connect Claude Desktop at the top of this page". That control is now on this
    tab, directly above the callout."""
    html = _read("tenant.html")
    block = html[html.index('id="cp-userconn"'):]
    block = block[:block.index("</div>", block.index("</p>"))]
    assert "top of this page" not in block
    assert "above" in block and "Seats" in block


def test_the_individual_console_gives_no_client_a_flagship_slot_either():
    """Same presumption, checked on the other console. Its ONLY Claude-Desktop-specific block
    (`claude-config-extra`) is already inside `#connect-body` and gated to that client; nothing
    client-specific sits above its picker."""
    html = _read("user.html")
    head = html[html.index("Connect your client"):html.index('id="connect-picker"')]
    for token in ("Claude", "mcpb", "OpenWebUI", "claude_desktop"):
        assert token not in head, f"{token!r} is above the individual console's tab strip"
    body_at = html.index('id="connect-body"')
    assert html.index('id="claude-config-extra"') > body_at, "CD block escaped the tab body"
    assert "S.connectClient !== 'claude_desktop'" in _read("user.js")


# ── the credential model the copy has to get right (verified against the AUTH CODE) ──

def test_openwebui_takes_the_account_key_and_claude_desktop_takes_the_per_user_token():
    """Pinned against src/mcp/server.py::bind_tenant, not against the copy:

      * flk_ → `_resolve_principal` returns "account:<id>"; bind_tenant REQUIRES a claimed
        X-OpenWebUI-User-Id (400 without one) and auto-seats it under the licensed cap. That is
        exactly "one org-wide key, every signed-in user becomes a seat by UUID".
      * flu_ → the principal IS the bound UUID; any DIFFERENT claimed id is a 403 spoof. One
        token, one seat.
    """
    from saas.onboarding import client_config_templates
    t = client_config_templates("https://x.example")
    assert t["openwebui"]["credential"] == "connection_key"
    assert t["generic"]["credential"] == "connection_key"
    assert t["claude_desktop"]["credential"] == "user_token"
    # The copy must NAME the type, not say "your connection key" and leave it ambiguous —
    # "connection key" was doing the work of two distinct credential classes.
    owui = " ".join(t["openwebui"]["instructions"])
    assert "flk_" in owui and "ACCOUNT key" in owui
    assert "BY UUID" in owui, "the auto-seat-by-UUID half is the thing OpenWebUI users need"
    cd = " ".join(t["claude_desktop"]["instructions"])
    assert "flu_" in cd and "not the account connection key" in cd


def test_bind_tenant_still_requires_a_claimed_uuid_for_an_account_principal():
    """If this ever stops being true, the OpenWebUI copy ("users auto-appear as seats by UUID")
    becomes a lie and must change with it."""
    src = (ROOT / "src" / "mcp" / "server.py").read_text(encoding="utf-8")
    i = src.index("def bind_tenant(")
    body = src[i:i + 8000]
    assert 'principal.startswith("account:")' in body
    assert "user_id required: no X-OpenWebUI-User-Id claimed" in body
    # Identity-honesty bug 4 (fix/identity-honesty): the spoof 403 now names the rejected
    # claim AND the token's own principal seat and steers to omitting user_id. The claim is
    # interpolated into the message, so pin the two invariant fragments.
    assert "claimed user_id" in body and "does not match" in body
    assert "authenticated principal" in body
    assert "this token is seat" in body, (
        "the 403 body must name the caller's own seat (self-service 403)")


# ════════════════════════════════════════════════════════════════════════════════════
# CLEARTEXT DISCLAIMER — both endpoint fields, one implementation, linked to the wiki
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_embedding_endpoint_gets_the_warning_too(rendered):
    """The embedding URL is a SECOND separately-configurable endpoint with its OWN key, and its
    payload is the text being embedded — memory content. It had no warning at all."""
    e = rendered["cleartext"]["embedding"]
    assert e["hidden"] is False
    assert "Not encrypted." in e["text"]
    assert "memory content" in e["text"], "the embedding variant must name what actually crosses"
    assert "every message FaultLine exchanges" not in e["text"], "that is the CHAT wording"


def test_the_chat_wording_is_unchanged_byte_for_byte(rendered):
    """The owner reviewed and approved this sentence. Parameterising the payload clause must not
    have moved a comma of it."""
    APPROVED = (
        "Not encrypted. http:// sends the API key you store here, and every message FaultLine "
        "exchanges with this endpoint, across the internet unencrypted and readable in transit. "
        "Anyone on the network path — your ISP, any network between here and there, anyone on "
        "the same Wi-Fi — can read the conversation content and capture the key. It will still "
        "be saved and it will still work — but use https:// instead if this endpoint supports it."
    )
    text = rendered["cleartext"]["chat"]["text"]
    assert text.startswith(APPROVED), f"the approved wording drifted:\n{text}"
    # The only permitted addition is the help link.
    assert text[len(APPROVED):].strip() == "Learn more ▸"


def test_https_clears_both_notices(rendered):
    assert rendered["cleartext"]["https_clears"] == {"chat": True, "embedding": True}


def test_both_notices_deep_link_into_the_help_wiki(rendered):
    """A warning that is a dead end is the complaint. Same link convention as the brain-health
    banners (class `bb-help`, absolute /dashboard/help/ hash, `#page:anchor` for a section)."""
    chat = rendered["cleartext"]["chat"]["help_href"]
    emb = rendered["cleartext"]["embedding"]["help_href"]
    assert chat == "/dashboard/help/#unencrypted-endpoint"
    assert emb == "/dashboard/help/#unencrypted-endpoint:ct-both-endpoints"


def test_the_help_page_exists_and_the_anchors_resolve():
    """A deep link to a page that isn't there is the SAME bug as "generate one below"."""
    wiki = (DASH / "help" / "index.html").read_text(encoding="utf-8")
    assert 'id="unencrypted-endpoint"' in wiki
    for anchor in ("ct-what-crosses", "ct-both-endpoints", "ct-compensate", "ct-to-https"):
        assert f'id="{anchor}"' in wiki, anchor


def test_the_help_page_does_not_recommend_the_origin_acl():
    """The origin ACL governs which BROWSER ORIGINS may call FaultLine — inbound. An endpoint
    connection is outbound and is not a browser. Recommending it here teaches the wrong mental
    model; the page names it only to say it does NOT apply."""
    wiki = (DASH / "help" / "index.html").read_text(encoding="utf-8")
    start = wiki.index('id="unencrypted-endpoint"')
    page = wiki[start:wiki.index("</section>", start)]
    assert "does <em>not</em> help here" in page or "not this control" in page
    # the mitigation that DOES apply must be present and concrete
    assert "/32" in page
    assert "egress" in page.lower()
    assert "reverse proxy" in page


def test_the_warning_copy_still_has_exactly_one_home():
    """Three copies of the MCP tool descriptions drifted and shipped an impoverished one to every
    OpenWebUI model. The embedding variant is a PARAMETER, not a second copy."""
    src = _read("feature_copy.js")
    assert src.count("unencrypted and readable in transit") == 1
    for script in ("user.js", "tenant.js"):
        assert "unencrypted and readable in transit" not in _read(script), script


def test_both_endpoint_fields_are_bound_and_repainted_after_the_stored_values_land():
    """Binding while the field is still empty proves nothing: setting `.value` in JS fires no
    input event, so a SAVED http:// endpoint would show no disclaimer until someone typed in it.
    The repaint must come AFTER the embedding fields are populated, not after base_url."""
    src = _read("tenant.js")
    assert "FL_CLEARTEXT.bind('llm-baseurl', 'llm-cleartext-note', 'chat')" in src
    assert "FL_CLEARTEXT.bind('emb-url', 'emb-cleartext-note', 'embedding')" in src
    load = src[src.index("async function loadLlm()"):]
    load = load[:load.index("\n}")]
    assert load.index("$('emb-url').value") < load.index("bindCleartextNotice()"), \
        "the repaint runs before the embedding URL is populated — a stored http:// URL is silent"


def test_the_embedding_url_is_ssrf_guarded_like_the_chat_url():
    """A second egress path with no egress check would be a far more serious finding than a
    missing warning. It is guarded at SAVE, at RESOLVE, and at the call seam — pin all three."""
    cfg = (ROOT / "saas" / "llm_config.py").read_text(encoding="utf-8")
    v = cfg[cfg.index("def _validate_urls("):]
    assert '("base_url", "embedding_url")' in v[:800], "save-time guard skips embedding_url"
    assert "validate_egress_url(emb_url)" in cfg, "resolve-time guard skips embedding_url"
    calls = (ROOT / "src" / "api" / "llm_calls.py").read_text(encoding="utf-8")
    assert "validate_egress_url(emb)" in calls, "call-seam guard skips the embedding URL"


def test_the_disclaimer_markup_follows_each_field_and_stays_csp_clean():
    html = _read("tenant.html")
    for field, note in (("llm-baseurl", "llm-cleartext-note"), ("emb-url", "emb-cleartext-note")):
        assert f'id="{note}"' in html, note
        assert html.index(f'id="{field}"') < html.index(f'id="{note}"'), note
    import re
    for page in ("tenant.html", "user.html"):
        h = _read(page)
        assert not [m for m in re.findall(r"<script\b[^>]*>", h) if "src=" not in m], page
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", h), page
