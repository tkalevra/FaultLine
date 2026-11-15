"""Upstream (OpenWebUI-supplied) seat identity — capture, PII handling, and RENDER.

WHY THIS FILE EXISTS. A tenant wires OpenWebUI ONCE with an account key (``flk_``). Every
signed-in OWUI user then auto-provisions as a seat BY UUID, because the only identity the MCP
transport consumed was ``X-OpenWebUI-User-Id``. The Seats page rendered a column of bare
UUIDs — the first question every new tenant asks after connecting, answerable only by
hand-diffing against OpenWebUI's own Admin Panel. This captures the display name/email
OpenWebUI already forwards and shows them beside the seat.

THE HEADERS ARE VERIFIED, NOT ASSUMED. Read from OpenWebUI source (`main`, v0.11.0):
``backend/open_webui/env.py`` names them; ``utils/headers.py::include_user_info_headers``
builds them; ``utils/tools.py::build_tool_server_headers`` ("Shared by MCP and OpenAPI
paths") applies them on the ``type == "openapi"`` branch of ``get_tools()`` — the door
OpenWebUI uses for our REST shorthand. Encoding is asymmetric and that asymmetry is the
subtle part these tests pin: the NAME is ``quote(name, safe=' ')``-encoded, the EMAIL is not.

FOUR PROPERTIES ARE LOAD-BEARING, and each has tests below:
  1. ABSENT IDENTITY RENDERS EXACTLY AS TODAY — plain "unlabeled seat", no gap, no "None".
  2. THE EDITABLE SEAT-LABEL CONTROL IS UNTOUCHED — same glyph, classes, wiring, and prompt
     copy, in BOTH states, still seeded from ``label`` and never from the upstream name.
  3. THE UPSTREAM IDENTITY IS READ-ONLY — a span, no handler, no control, not editable.
  4. SAAS_MODE OFF ⇒ THE LANE IS ABSENT — no header read, no import, no write.

⚠️ jsdom IS NOT A BROWSER. The rendered assertions run the real ``tenant.html`` and the real
``tenant.js`` ``renderSeats()`` painter, but nothing lays out and no CSS is applied. They
prove which elements exist with what text, classes, order and wiring — not pixels. The
subordinate styling is asserted separately and statically against the real stylesheet.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "saas" / "dashboard"
HARNESS = ROOT / "tests" / "dashboard" / "render_seats.js"

# One seat WITH upstream identity, one WITHOUT — the two states the owner asked to see, and
# a third with a tenant-set label to prove the label still wins the title position.
SEATS = [
    {"user_id": "89f903d2-1111-4aaa-8bbb-000000000001", "status": "active", "label": None,
     "seat_type": "standard", "is_base": True,
     "owui_name": "Jenn Marchetti", "owui_email": "jenn.marchetti@example.com"},
    {"user_id": "89f903d2-2222-4aaa-8bbb-000000000002", "status": "active", "label": None,
     "seat_type": "standard", "is_base": True,
     "owui_name": None, "owui_email": None},
    {"user_id": "89f903d2-3333-4aaa-8bbb-000000000003", "status": "active",
     "label": "build-agent", "seat_type": "agent", "is_base": False,
     "owui_name": "Priya Raghunathan",
     "owui_email": "priya.raghunathan@a-rather-long-corporate-domain.example.com"},
]
WITH_ID, NO_ID, LABELLED = (s["user_id"] for s in SEATS)


# ════════════════════════════════════════════════════════════════════════════════════
# RENDERED — the real page, the real painter
# ════════════════════════════════════════════════════════════════════════════════════

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
    return json.loads(out.stdout)["cards"]


def test_identity_renders_to_the_right_of_the_label(cards):
    """The upstream identity sits AFTER the label in the header row.

    DOM order in a left-to-right flex row IS the visual order, which is how "to the right of
    'unlabeled seat'" is provable without laying anything out.
    """
    c = cards[WITH_ID]
    assert c["label_text"] == "unlabeled seat"
    assert c["owui_name_text"] == "Jenn Marchetti"
    assert c["owui_email_text"] == "jenn.marchetti@example.com"
    assert c["order"].index("label") < c["order"].index("owui-name")
    assert c["order"].index("owui-name") < c["order"].index("owui-email")
    # And it reads as one line to a human, label first.
    assert c["header_text"].startswith("unlabeled seat")
    assert c["header_text"].endswith("Jenn Marchettijenn.marchetti@example.com")


def test_absent_identity_renders_exactly_as_today(cards):
    """No upstream identity → plain "unlabeled seat". No gap, no "None", no empty separator.

    This is the owner's own "if available" framing and the single most important negative:
    the overwhelming majority of seats (upstream flag off, JWT-mode, non-OWUI client,
    hand-added seat) land here, and they must be BYTE-IDENTICAL to the pre-feature page.
    """
    c = cards[NO_ID]
    assert c["header_text"] == "unlabeled seat✎"       # label + the edit glyph, nothing else
    assert c["owui_name_text"] is None
    assert c["owui_email_text"] is None
    # No orphan separator dot left behind, and no placeholder element of any kind.
    assert c["order"] == ["label", "edit-button"]
    assert "None" not in c["header_text"] and "null" not in c["header_text"]


def test_editable_seat_label_control_is_untouched_in_both_states(cards):
    """The ✎ "Label for seat …" control is IDENTICAL with and without an upstream identity.

    The owner's constraint verbatim: "leave the 'Label for seat 7c1b8320… (name the agent)'
    alone". Two separate fields with two separate authorities — the label is the TENANT's
    name for the seat, the identity is upstream truth. This asserts the glyph, the classes,
    the wiring, AND the prompt copy the user actually sees, in both states at once.
    """
    a, b = cards[WITH_ID], cards[NO_ID]
    for c in (a, b):
        assert c["edit_present"] is True
        assert c["edit_text"] == "✎"
        assert c["edit_classes"] == "btn xs sc-edit"
        assert c["edit_wired"] is True
        assert c["edit_prompt"]["message"] == "Label for seat 7c1b8320… (name the agent)"
    # Identical, not merely both-present.
    assert a["edit_classes"] == b["edit_classes"]
    assert a["edit_prompt"]["message"] == b["edit_prompt"]["message"]


def test_label_prompt_is_seeded_from_the_label_never_the_upstream_name(cards):
    """Opening the label editor must NOT pre-fill the OWUI name.

    The failure this guards is quiet and permanent: seeding the prompt with upstream truth
    invites an admin to press Enter and COPY it into the tenant-owned label, collapsing the
    two fields into one and making a read-only mirror look like something they edited.
    """
    assert cards[WITH_ID]["edit_prompt"]["seeded"] == ""            # no label set → empty
    assert cards[LABELLED]["edit_prompt"]["seeded"] == "build-agent"  # the label, not "Priya…"


def test_upstream_identity_is_read_only(cards):
    """No click handler, no input/button/select, not contenteditable — a plain span."""
    for uid in (WITH_ID, LABELLED):
        assert cards[uid]["owui_interactive"] is False


def test_tenant_label_still_wins_the_title_position(cards):
    """A seat WITH a label shows the label as its title and the identity beside it."""
    c = cards[LABELLED]
    assert c["label_text"] == "build-agent"
    assert c["label_is_empty_style"] is False        # not the muted/italic "unlabeled" style
    assert c["owui_name_text"] == "Priya Raghunathan"
    assert c["order"].index("label") < c["order"].index("owui-name")


def test_long_email_cannot_break_the_row(cards):
    """A long address is carried by a style that wraps rather than overflowing.

    jsdom does not lay out, so this is asserted where it is actually decided: the class the
    painter attaches, plus the real stylesheet rule for that class (checked statically in
    ``test_identity_styling_is_subordinate_and_wraps``). ``.sc-name-wrap`` is a wrapping flex
    row, so a ``break-all`` child wraps to the next line instead of widening the card.
    """
    c = cards[LABELLED]
    assert len(c["owui_email_text"]) > 50
    assert "sc-login-email" in c["owui_email_classes"]


# ════════════════════════════════════════════════════════════════════════════════════
# STATIC — assertions that hold with or without node, and never skip
# ════════════════════════════════════════════════════════════════════════════════════

def test_identity_styling_is_subordinate_and_wraps():
    """The reused ``.sc-login-email`` rule really is subordinate and really does wrap.

    The painter leans on this existing rule instead of adding a stylesheet rule of its own.
    That reuse is only correct while the rule still says what we think it says — if someone
    restyles ``.sc-login-email`` into something title-sized, this feature silently turns into
    a second title, so the dependency is pinned here rather than left implicit.
    """
    css = (DASH / "style.css").read_text(encoding="utf-8")
    rule = next(ln for ln in css.splitlines() if ln.strip().startswith(".sc-login-email"))
    assert "font-size: 12px" in rule              # smaller than .sc-label's 15px
    assert "var(--muted)" in rule                 # quieter than .sc-label's var(--fg)
    assert "word-break: break-all" in rule        # a long address wraps, never overflows
    # And the container it sits in is a WRAPPING flex row, so wrapping has somewhere to go.
    wrap = next(ln for ln in css.splitlines() if ln.strip().startswith(".sc-name-wrap"))
    assert "flex-wrap: wrap" in wrap


def test_painter_uses_textcontent_never_innerhtml_for_upstream_values():
    """Upstream header values reach the DOM as TEXT, never as markup.

    These strings originate in an HTTP header controlled by another system's users. The
    surrounding meta row legitimately builds ``innerHTML`` from ``esc()``-wrapped values;
    this block must not join it, because a single un-``esc()``-ed interpolation there would
    be stored XSS against the tenant admin console.
    """
    js = (DASH / "tenant.js").read_text(encoding="utf-8")
    start = js.index("s.owui_name || s.owui_email")
    block = js[start:start + 900]
    assert "innerHTML" not in block
    assert block.count("textContent") >= 2


def test_no_new_stylesheet_dependency_and_no_inline_script():
    """CSP: dashboard JS stays external. This feature adds no inline script and no handler.

    The dashboard CSP is ``script-src 'self'`` with no ``'unsafe-inline'`` — an inline
    ``<script>`` or an ``on*=`` attribute renders the page BLANK, not degraded.
    """
    html = (DASH / "tenant.html").read_text(encoding="utf-8")
    assert "owui" not in html.lower()          # nothing about this feature lives in the HTML
    js = (DASH / "tenant.js").read_text(encoding="utf-8")
    start = js.index("s.owui_name || s.owui_email")
    block = js[start:start + 900]
    assert "onclick=" not in block


# ════════════════════════════════════════════════════════════════════════════════════
# CAPTURE — header decoding, sanitization, PII discipline
# ════════════════════════════════════════════════════════════════════════════════════

def _hs(**headers):
    """A stand-in request exposing only case-insensitive ``headers.get`` (what we use)."""
    class _H:
        def __init__(self, d): self._d = {k.lower(): v for k, v in d.items()}
        def get(self, k, default=None): return self._d.get(k.lower(), default)

    class _R:
        def __init__(self, d): self.headers = _H(d)
    return _R(headers)


def test_display_name_is_percent_decoded():
    """OpenWebUI sends ``quote(name, safe=' ')`` — decoding is the correct read of the wire.

    ``utils/headers.py`` percent-encodes the NAME because HTTP header values are latin-1
    (RFC 9110 §5.5) and a non-ASCII display name cannot ride one raw. "José Álvarez" must
    not be stored and shown to an admin as "Jos%C3%A9 %C3%81lvarez".
    """
    from src.mcp import http_server as hs
    r = _hs(**{"X-OpenWebUI-User-Name": "Jos%C3%A9 %C3%81lvarez"})
    raw = hs._read_owui_header(r, hs._OWUI_NAME_HEADERS)
    assert hs._decode_owui_name(raw) == "José Álvarez"


def test_plain_ascii_name_passes_through_untouched():
    from src.mcp import http_server as hs
    r = _hs(**{"X-OpenWebUI-User-Name": "Jenn Marchetti"})
    raw = hs._read_owui_header(r, hs._OWUI_NAME_HEADERS)
    assert hs._decode_owui_name(raw) == "Jenn Marchetti"


def test_email_is_not_decoded_on_current_openwebui():
    """Current OWUI sends the email UNQUOTED — blind decoding would corrupt a literal ``%``.

    RFC 5322 §3.2.3 permits ``%`` in a local-part. ``od%d@example.com`` is a valid address
    that must survive verbatim; percent-decoding it yields ``od\\r@example.com``-class junk.
    """
    from src.mcp import http_server as hs
    for addr in ("jenn.marchetti@example.com", "od%d@example.com"):
        assert hs._decode_owui_email(addr) == addr


def test_email_IS_decoded_when_an_older_openwebui_encoded_it():
    """OpenWebUI's history disagrees with itself, so we resolve it by SHAPE, not by version.

    Commit ``f7920df`` ("Fix non-ascii error issue") percent-encoded ALL FOUR fields; later
    commits narrowed it to the name. A pinned older build really can send
    ``someone%40example.com``. Decode only when the raw value does not look like an address
    and the decoded one does — deterministic in every direction, no version sniffing.
    """
    from src.mcp import http_server as hs
    assert hs._decode_owui_email("someone%40example.com") == "someone@example.com"


def test_oversized_header_is_dropped_whole_not_truncated():
    """A 64KB header is not a person's name — it never reaches a decoder or a DB parameter."""
    from src.mcp import http_server as hs
    r = _hs(**{"X-OpenWebUI-User-Name": "A" * 65536})
    assert hs._read_owui_header(r, hs._OWUI_NAME_HEADERS) == ""


def test_absent_headers_yield_nothing():
    from src.mcp import http_server as hs
    r = _hs(**{"X-OpenWebUI-User-Id": "89f903d2-1111-4aaa-8bbb-000000000001"})
    assert hs._read_owui_header(r, hs._OWUI_NAME_HEADERS) == ""
    assert hs._read_owui_header(r, hs._OWUI_EMAIL_HEADERS) == ""


def test_header_names_match_openwebui_source():
    """Pin the exact names read from OpenWebUI ``backend/open_webui/env.py`` (main, 0.11.0).

    If this ever needs changing, re-read that file for the deployed tag — the constants are
    env-overridable and the module layout has moved between versions.
    """
    from src.mcp import http_server as hs
    assert hs._OWUI_NAME_HEADERS == ("X-OpenWebUI-User-Name",)
    assert hs._OWUI_EMAIL_HEADERS == ("X-OpenWebUI-User-Email",)


def test_control_characters_are_stripped_at_the_boundary():
    """CR/LF/NUL smuggled through a header never reach a stored field.

    They collapse to whitespace rather than being DELETED: a control character between two
    tokens is a separator, so deleting it would weld them into one misleading token
    ("Jenn\\r\\nSmith" → "JennSmith"). Neither form is exploitable (the value is stored as
    text and rendered with ``textContent``), but the separator reading is the honest one.
    """
    from saas.seats import _sanitize_owui_value, _OWUI_NAME_MAX
    assert _sanitize_owui_value("Jenn\r\nSet-Cookie: x=1", _OWUI_NAME_MAX) == "Jenn Set-Cookie: x=1"
    assert _sanitize_owui_value("a\x00b", _OWUI_NAME_MAX) == "a b"
    assert "\r" not in _sanitize_owui_value("a\rb", _OWUI_NAME_MAX)


def test_blank_and_non_string_values_become_none_not_empty_string():
    """Absent and blank must be stored identically — as NULL, which renders as today."""
    from saas.seats import _sanitize_owui_value, _OWUI_NAME_MAX
    for junk in ("", "   ", "\t\n", None, 42, [], {}):
        assert _sanitize_owui_value(junk, _OWUI_NAME_MAX) is None


def test_values_are_length_capped():
    from saas.seats import (_sanitize_owui_value, _sanitize_owui_email,
                            _OWUI_NAME_MAX, _OWUI_EMAIL_MAX)
    assert len(_sanitize_owui_value("N" * 5000, _OWUI_NAME_MAX)) == _OWUI_NAME_MAX
    assert _OWUI_NAME_MAX == 200
    assert _OWUI_EMAIL_MAX == 320                 # RFC 5321 §4.5.3.1.3 forward-path cap
    assert _sanitize_owui_email("x" * 5000 + "@example.com") is None   # over cap → not an addr


def test_junk_that_is_not_an_address_is_rejected_rather_than_shown():
    """A stray header value must not be rendered to an admin as if it were a colleague."""
    from saas.seats import _sanitize_owui_email
    for junk in ("not an email", "a@b@c.com", "@example.com", "user@", "user@nodot", "  "):
        assert _sanitize_owui_email(junk) is None
    assert _sanitize_owui_email("  Jenn.Marchetti@Example.COM ") == "jenn.marchetti@example.com"


def test_capture_never_raises_and_never_writes_when_saas_is_off():
    """SAAS_MODE OFF ⇒ the lane is ABSENT: no header read, no import, no write.

    The gate is the principal shape. ``account:``-prefixed principals are produced ONLY by
    ``_resolve_principal`` under SAAS_MODE, so in the open core the first branch returns
    before anything else happens. Proven by handing it a request whose ``headers.get``
    EXPLODES — if the lane were live it would raise, and it must not even be reached.
    """
    from src.mcp import http_server as hs

    class _Boom:
        @property
        def headers(self): raise AssertionError("headers must not be read when SAAS is off")

    for principal in (None, "shared", "anonymous",
                      "89f903d2-1111-4aaa-8bbb-000000000001"):
        assert hs._capture_owui_identity(
            _Boom(), principal, "89f903d2-1111-4aaa-8bbb-000000000001") is None


def test_capture_swallows_a_control_plane_failure():
    """A control-plane blip must never fail a tool call — the lane is decorative."""
    from src.mcp import http_server as hs
    import saas.seats as seats
    calls = []
    original = seats.record_owui_identity

    def _explode(*a, **kw):
        calls.append(a)
        raise RuntimeError("control plane down")

    seats.record_owui_identity = _explode
    try:
        r = _hs(**{"X-OpenWebUI-User-Name": "Jenn Marchetti"})
        assert hs._capture_owui_identity(r, "account:abc", "89f903d2-1111-4aaa-8bbb-1") is None
        assert calls, "the lane should have been reached under an account principal"
    finally:
        seats.record_owui_identity = original


def test_pii_is_never_logged_or_audited():
    """No log line, audit ``detail``, or eventlog payload may carry the name/email.

    MCP logs are noisy and long-lived; an audit trail of "seat X is now named <person>" is
    exactly the leak this rule exists to prevent. Asserted structurally over the two blocks
    that hold the values, because a leak here is invisible until it is in a log you cannot
    unwrite.
    """
    src = (ROOT / "src" / "mcp" / "http_server.py").read_text(encoding="utf-8")
    start = src.index("def _capture_owui_identity")
    block = src[start:src.index("\ndef ", start + 10)]
    assert "_log(" not in block

    seats_src = (ROOT / "saas" / "seats.py").read_text(encoding="utf-8")
    start = seats_src.index("def record_owui_identity")
    block = seats_src[start:seats_src.index("\ndef ", start + 10)]
    assert "_audit.record" not in block
    assert "eventlog" not in block
    assert "print(" not in block


def test_upstream_identity_is_opt_in_and_defaults_to_absent():
    """The PII columns are OPT-IN at ``list_seats``, and the DEFAULT is the security control.

    ⚠️ THIS TEST REPLACED A FALSE GREEN, and the reason is worth keeping. The first version
    asserted ``"owui_name" not in saas/admin.py`` and passed — while being worthless.
    ``admin.py::list_account_seats`` (the CROSS-ACCOUNT operator realm) returns
    ``list_seats`` rows **verbatim**, so simply adding the columns to the SELECT would have
    started serving every tenant's users' real names and email addresses to the global
    super-admin view, silently, without one line of ``admin.py`` ever mentioning them. A
    grep for the field name cannot see a pass-through leak.

    So the leak is closed at the DEFAULT instead: a caller gets these keys only by asking.
    Asserted against real behaviour with a stubbed cursor, not by grepping for a string.
    """
    import saas.seats as seats

    row = ("89f903d2-1111-4aaa-8bbb-000000000001", "active", None, None, True,
           "standard", True, None, False, "Jenn Marchetti", "jenn@example.com")

    class _Cur:
        def execute(self, *a, **kw): pass
        def fetchall(self): return [row]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    original = seats.db.connection
    seats.db.connection = lambda *a, **kw: _Conn()
    try:
        default = seats.list_seats("acct")[0]
        opted_in = seats.list_seats("acct", include_upstream_identity=True)[0]
    finally:
        seats.db.connection = original

    # DEFAULT: the keys are ABSENT entirely — not present-and-None. A pass-through caller
    # (the operator realm) therefore cannot leak what it never received.
    assert "owui_name" not in default and "owui_email" not in default
    # Everything else the seat card needs is unchanged by the gate.
    assert default["user_id"] == row[0] and default["seat_type"] == "standard"
    # OPTED IN: the tenant console's session-scoped surface gets them.
    assert opted_in["owui_name"] == "Jenn Marchetti"
    assert opted_in["owui_email"] == "jenn@example.com"


def test_only_the_session_scoped_tenant_surface_opts_in():
    """Exactly one call site asks for the PII — the session-authenticated tenant seat list.

    ``require_dashboard_session`` derives account_id server-side, so it is BOLA-safe and a
    seat-holder cannot reach it to enumerate colleagues. If a second opt-in ever appears,
    this goes red and that surface's scoping has to be argued for explicitly.
    """
    import re
    # A real CALL SITE, not a mention: the kwarg and the call on the same line. (Counting bare
    # mentions matched this feature's own docstrings and made the test unstable.)
    call = re.compile(r"list_seats\([^)]*include_upstream_identity\s*=\s*True")
    hits = []
    for path in (ROOT / "saas").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if call.search(line):
                hits.append((path.name, i))
    assert len(hits) == 1, hits
    assert hits[0][0] == "tenant_console.py", hits

    src = (ROOT / "saas" / "tenant_console.py").read_text(encoding="utf-8")
    fn = src[src.index("def tenant_list_seats"):]
    fn = fn[:fn.index("\n@router")]
    assert "include_upstream_identity=True" in fn
    # …and that function is session-authenticated, not caller-parameterised.
    decl = src[:src.index("def tenant_list_seats")].rsplit("@router", 1)[-1]
    assert "Depends(require_dashboard_session)" in fn.split("\n")[0] or \
           "Depends(require_dashboard_session)" in decl + fn[:300]
    assert re.search(r"account_id:\s*str\s*=\s*Depends\(require_dashboard_session\)", fn)


def test_individual_console_does_not_surface_the_field():
    """The seat-holder's own "My Memory" console does not render it either.

    Decision on record: the seat-holder already knows their own name, so showing it there
    adds a PII surface and a route to get wrong for no product value — and a seat-holder must
    never be able to enumerate colleagues.
    """
    user_src = (ROOT / "saas" / "user_console.py").read_text(encoding="utf-8")
    assert "owui_name" not in user_src
    assert "owui_email" not in user_src
    user_js = (DASH / "user.js").read_text(encoding="utf-8")
    assert "owui_name" not in user_js and "owui_email" not in user_js


# ════════════════════════════════════════════════════════════════════════════════════
# SCHEMA — migration present, idempotent, and correctly NOT in the tenant template
# ════════════════════════════════════════════════════════════════════════════════════

def test_migration_adds_all_three_columns_idempotently():
    mig = next(p for p in (ROOT / "migrations").glob("210_*.sql"))
    sql = mig.read_text(encoding="utf-8")
    for col in ("owui_name", "owui_email", "owui_identity_seen_at"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql
    assert "public.account_users" in sql
    # NULLABLE with no default and no backfill — absent identity is the normal case.
    assert "NOT NULL" not in sql.split("ALTER TABLE")[1]
    assert "UPDATE public.account_users" not in sql


def test_columns_belong_to_the_control_plane_not_a_tenant_schema():
    """``public.account_users`` exists ONLY in public (migration 131) and has no counterpart
    in the per-tenant template — so there is no template half to keep in step.

    The standing rule is that a migration-only OR template-only change silently breaks either
    existing or new tenants. It does not bite here, and this test is what makes that claim
    checkable instead of asserted: if ``account_users`` is ever added to the tenant template,
    this goes red and the migration needs a template twin.
    """
    template = (ROOT / "src" / "provisioning" / "templates" / "user_schema.sql").read_text(
        encoding="utf-8")
    assert "account_users" not in template
    assert "owui_name" not in template
    mig131 = (ROOT / "migrations" / "131_saas_account_control_plane.sql").read_text(
        encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.account_users" in mig131


def test_list_seats_selects_and_returns_the_new_columns():
    """The read path must actually carry them — verify the READ, not just the write."""
    seats_src = (ROOT / "saas" / "seats.py").read_text(encoding="utf-8")
    start = seats_src.index("def list_seats")
    block = seats_src[start:seats_src.index("\ndef ", start + 10)]
    # Match the COLUMNS, not one exact adjacency. The literal "owui_name, owui_email" broke when
    # migration 212 added a LEFT JOIN and every column had to take an "au."/"iu." alias — the
    # SELECT still carried both, the read still mapped them, and only this string went red. A
    # source-grep that pins incidental punctuation asserts the WRITE's spelling, not the read.
    assert "owui_name" in block and "owui_email" in block      # in the SELECT
    assert '"owui_name":' in block and '"owui_email":' in block   # and in the returned dict
