"""Issue #222 — the tenant console "test & calibrate" gate on seats with no individual login.

THE WOUND (measured on prod 2026-10-06, seat "Alicia", Example University): the seat
card's Endpoint Health card offered "▸ test & calibrate", which POSTs
``/tenant/seats/{uid}/brain-health/recalibrate`` — a route that resolves the seat's brain
binding through its ``individual_users`` login row (``tenant_console.get_managed_individual``).
A plain tenant-account seat NEVER has one, so the route 404s BY CONSTRUCTION
("no individual login for this seat") — the console advertised a structurally broken action.
The account-level Test (``POST /tenant/llm-config/test``) is the working path.

THE FIX (frontend gate, saas/dashboard/tenant.js only): ``seatBrainHealthRow()`` stamps the
seat summary's ``has_login`` onto the health-card body (``dataset.scHasLogin`` — a stamp that
survives the post-recalibrate card rebuild because the row is re-built from the freshest
/tenant/seats payload), and ``renderSeatBrainHealth()`` reads it: no login → the button stays
VISIBLE but disabled, with a title and an inline hint naming the account-level alternative.
``clear history`` needs no login and stays enabled in both branches.

NULL-HANDLING MATCHES THE EXISTING CONSUMERS. Every other ``s.has_login`` consumer in
tenant.js reads plain truthiness (``if (s.has_login)`` / ``s.has_login ? … : 'no login'``), so
falsy — false, null, absent — reads as "no login". The backend serializer
(``tenant_console.tenant_list_seats``) always sets ``has_login = bool(info)``, so a live
payload carries a real boolean; absent/null is the older-payload/fixture case and fails SAFE
to disabled, matching the consumers.

⚠️ jsdom IS NOT A BROWSER. The rendered assertions run the real ``tenant.html`` and the real
``tenant.js`` painters; they prove which elements exist with what disabled state, titles and
hint text — not pixels or hover tooltips.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_seat_brain_gate.js"
TENANT_JS = DASH / "tenant.js"

# Four rows of the truth table: login / no login / null / ABSENT KEY — the serializer always
# sends a boolean, so the last two pin the FAIL-SAFE direction for older payloads/fixtures.
SEATS = [
    {"user_id": "89f903d2-aaaa-4aaa-8bbb-000000000001", "status": "active", "label": None,
     "seat_type": "standard", "is_base": True, "has_login": True},
    {"user_id": "89f903d2-bbbb-4aaa-8bbb-000000000002", "status": "active", "label": None,
     "seat_type": "standard", "is_base": True, "has_login": False},
    {"user_id": "89f903d2-cccc-4aaa-8bbb-000000000003", "status": "active", "label": None,
     "seat_type": "standard", "is_base": True, "has_login": None},
    {"user_id": "89f903d2-dddd-4aaa-8bbb-000000000004", "status": "active", "label": None,
     "seat_type": "standard", "is_base": True},
]
WITH_LOGIN, NO_LOGIN, NULL_LOGIN, ABSENT_LOGIN = (s["user_id"] for s in SEATS)

ENABLED_TITLE = ("Make real timed calls to this seat's endpoint "
                 "and re-pick its latency profile")
HINT_SUBSTR = "account-level"


@pytest.fixture(scope="module")
def cards():
    node = shutil.which("node")
    if not node or not HARNESS.exists():
        pytest.skip("node/jsdom harness unavailable — static assertions below still hold")
    env = dict(os.environ)
    env["NODE_PATH"] = str(HARNESS.parent / "node_modules")
    try:
        out = subprocess.run([node, str(HARNESS), json.dumps(SEATS)],
                             capture_output=True, text=True, timeout=120,
                             cwd=str(ROOT), env=env)
    except Exception as exc:                      # pragma: no cover
        pytest.skip(f"jsdom render failed to launch: {exc}")
    if out.returncode != 0:                       # pragma: no cover
        pytest.skip(f"jsdom render failed: {out.stderr[-2000:]}")
    parsed = json.loads(out.stdout)
    assert parsed["api_calls_on_disabled_click"] == 0, (
        "the disabled recalibrate button fired its handler — a real browser never runs a "
        "disabled button's activation; jsdom must agree")
    return parsed["cards"]


# ════════════════════════════════════════════════════════════════════════════════════
# THE TRUTH TABLE, as rendered
# ════════════════════════════════════════════════════════════════════════════════════

def test_with_login_renders_exactly_as_before(cards):
    """FROZEN BAR: has_login=true → button unchanged (enabled, original title, no hint)."""
    c = cards[WITH_LOGIN]
    assert c["stamp"] == "1"
    assert c["recal_present"] is True
    assert c["recal_disabled"] is False
    assert c["recal_title"] == ENABLED_TITLE
    assert c["hint_present"] is False
    assert c["clear_disabled"] is False


def test_without_login_renders_disabled_with_the_alternative_named(cards):
    """has_login=false → visible but DISABLED, title + inline hint name the account-level Test."""
    c = cards[NO_LOGIN]
    assert c["stamp"] == "0"
    assert c["recal_present"] is True          # visible, never hidden
    assert c["recal_disabled"] is True
    assert HINT_SUBSTR in c["recal_title"]
    assert c["hint_present"] is True
    assert HINT_SUBSTR in c["hint_text"]
    # clear history needs no login — it must stay usable on exactly the seats where
    # recalibrate is gated (the health readings themselves are login-independent).
    assert c["clear_disabled"] is False


def test_null_login_fails_safe_to_disabled(cards):
    """has_login=null → DISABLED. Matches the consumers' falsy reading; never silently enabled."""
    c = cards[NULL_LOGIN]
    assert c["stamp"] == "0"
    assert c["recal_disabled"] is True
    assert HINT_SUBSTR in c["hint_text"]


def test_absent_login_key_fails_safe_to_disabled(cards):
    """Payload WITHOUT the key at all → DISABLED (fail-safe), same as the consumers."""
    c = cards[ABSENT_LOGIN]
    assert c["stamp"] == "0"
    assert c["recal_disabled"] is True
    assert HINT_SUBSTR in c["hint_text"]


# ════════════════════════════════════════════════════════════════════════════════════
# STATIC — hold even without node/jsdom
# ════════════════════════════════════════════════════════════════════════════════════

def test_stamp_reads_the_serializer_field_not_a_rival_name():
    """The stamp must read ``s.has_login`` — the exact field tenant_list_seats sets.

    The serializer (saas/tenant_console.py::tenant_list_seats) enriches EVERY seat row with
    ``has_login = bool(login_map.get(user_id))``. If a future edit renames either side, this
    pin goes red before the gate silently reads a field the payload never carries.
    """
    src = TENANT_JS.read_text(encoding="utf-8")
    assert "body.dataset.scHasLogin = s.has_login ? '1' : '0';" in src


def test_gate_blocks_the_route_the_seat_can_never_satisfy():
    """The gate keys on the stamped flag and only the recalibrate control, never `clear`."""
    src = TENANT_JS.read_text(encoding="utf-8")
    assert "const seatHasLogin = body.dataset.scHasLogin === '1';" in src
    assert "recal.disabled = true;" in src
    # No inline handlers introduced — the dashboard CSP is script-src 'self' (Pitfall: CSP).
    assert "onclick=" not in src.split("function seatBrainHealthRow")[1].split("function mkTip")[0]
