"""[it branch] FOSS-011 - a FIRST-MENTION naming sentence is a STATEMENT, never a CORRECTION.

Battery sentence (the bug repro, deterministic on the it stack - classified CORRECTION
twice, minting a "server" entity and destructively flipping pref_name server-to-devbox while
capturing NO relational content and NO IP):

    it: "Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5."
    es: "Mi servidor principal se llama DevBox, su IP es 10.0.1.5."   (measured: STATEMENT)
    en: "My main server is called DevBox, its IP is 10.0.1.5."        (measured: STATEMENT)

The fix aligns the it intent pattern set with the measured es/en behavior and adds the
doc first-mention guard, at four seams:

  1. linguistics.licensed_naming_names - the UD reflexive-clitic naming frames asserted
     names (grammar only; an English parse can never produce the frame, so en installs are
     byte-identical).
  2. NAMING_STATEMENT_PREROUTE + _is_first_mention_naming_statement - a declarative turn
     asserting exactly ONE name with NO correction cue routes STATEMENT before GLiNER2,
     mirroring the source-to-goal transition preroute. TWO declines keep every real correction
     on today ladder: the REPLACIVE shape ("il mio server si chiama Apollo ... anzi, si
     chiama DevBox" - two distinct names) and ANY correction/retraction cue.
  3. NAME_FIRST_MENTION_GUARD + _first_mention_naming_correction - /retract/correct declines
     a CORRECTION whose turn is naming-edges + one-name-per-subject + NO stored bearer
     (nothing to supersede) and answers status="first_mention_naming" instead of minting.
  4. remember_facts_tool reads that status and routes the turn back through the STATEMENT
     path, where the naming chains capture owns + alias (+ the IP scalar) in one call.

En-model pins run here (en_core_web_sm); the it-model pins run in the child process
(tests/_it_capture_helper.py, SPACY_MODEL=it_core_news_sm) and skip honestly without it -
the same convention as tests/test_it_engine_uplift.py. The critic live it-stack battery
re-runs them for real.
"""
import asyncio
import os
import subprocess
import sys

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.extraction.linguistics as L  # noqa: E402

_HELPER = os.path.join(os.path.dirname(__file__), "_it_capture_helper.py")


def _it_model_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("it_core_news_sm") is not None
    except Exception:  # noqa: BLE001
        return False


needs_it = pytest.mark.skipif(
    not _it_model_installed(),
    reason="it_core_news_sm not installed - the Italian parse runs only on an it stack")


def _it_names(*sentences):
    "Run licensed_naming_names per sentence in ONE child process under it_core_news_sm."
    p = subprocess.run([sys.executable, _HELPER, "names", *sentences],
                       capture_output=True, text=True, timeout=600)
    path = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
    assert path and os.path.exists(path), f"helper failed: rc={p.returncode} err={p.stderr[-2000:]}"
    import json
    with open(path) as fh:
        data = json.load(fh)
    os.unlink(path)
    return [tuple(x) if x is not None else None for x in data]


# 1. THE DECISION TABLE (no model needed - the classifier seam, deterministic)

def _decide(monkeypatch, names, cue=None):
    "Drive _is_first_mention_naming_statement with scripted naming frames + cue probe."
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_names", lambda _t: names)
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (cue, "confident" if cue else None))
    return M._is_first_mention_naming_statement("il mio server principale si chiama DevBox")


def test_first_mention_naming_classifies_statement(monkeypatch):
    """THE RED PIN: the battery sentence must classify STATEMENT (or capture owns+alias+IP in
    one call - the STATEMENT route the preroute feeds). One asserted name, no correction cue.
    Before FOSS-011 this helper did not exist and the turn routed CORRECTION on it."""
    assert _decide(monkeypatch, ("devbox",)) is True


def test_replacive_two_name_turn_never_preroutes(monkeypatch):
    """A TRUE it naming correction ("il mio server si chiama Apollo ... anzi, si chiama DevBox")
    asserts TWO distinct names - the replacive shape stays on the correction ladder, so the
    preroute can never steal it."""
    assert _decide(monkeypatch, ("apollo", "devbox")) is False


def test_cued_turn_defers_to_the_correction_dual_gate(monkeypatch):
    """Any correction/retraction cue (marker, contrastive negation, "I meant") wins over the
    naming reading - same deference as the source-to-goal transition guard."""
    assert _decide(monkeypatch, ("devbox",), cue="CORRECTION") is False


@pytest.mark.parametrize("names", [(), None])
def test_no_frame_or_layer_down_falls_through(monkeypatch, names):
    """No naming frame (or the layer unavailable) -> False: GLiNER2 keeps the turn - never a
    silent drop, never a preroute without evidence."""
    assert _decide(monkeypatch, names) is False


def test_interrogative_never_preroutes(monkeypatch):
    """A question is never a naming statement, whatever it mentions."""
    import src.api.main as M
    monkeypatch.setattr(L, "licensed_naming_names", lambda _t: ("devbox",))
    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(L, "is_interrogative_clause", lambda _t: True)
    assert M._is_first_mention_naming_statement("Come si chiama il mio server?") is False


# 2. EN INERTNESS (real en_core_web_sm - an English install is byte-identical)

_en = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable - the deriver no-ops")


@_en
@pytest.mark.parametrize("text", [
    "My main server is called DevBox, its IP is 10.0.1.5.",
    "my dog is called Rufus",               # fresh-domain control
    "Actually my sister's name is Thorne, not Wrenna.",
])
def test_english_parses_never_produce_a_licensed_frame(text):
    """The frame needs a UD reflexive clitic; a Penn parse cannot emit one, so the preroute
    is STRUCTURALLY inert on en installs - es/en measured behavior is untouched."""
    assert L.licensed_naming_names(text) == ()


@_en
def test_english_repair_turn_still_declines_the_preroute():
    """The en replacive repair keeps the correction ladder: cue fires (marker + contrastive
    negation) and the frame reader returns nothing - both declines, on the real parse."""
    import src.api.main as M
    assert M._is_first_mention_naming_statement(
        "Actually my sister's name is Thorne, not Wrenna.") is False


# 3. THE IT BATTERY (child process under it_core_news_sm; the critic stack runs these)

@needs_it
def test_battery_sentence_asserts_exactly_one_name():
    """THE FOSS-011 repro: ONE naming frame asserting devbox -> the decider == 1 gate fires
    STATEMENT (the classification the es/en fixtures already show)."""
    (names,) = _it_names("Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5.")
    assert names == ("devbox",), names


@needs_it
def test_true_it_naming_correction_asserts_two_names():
    """KEEP-GREEN PIN: the true it naming correction ("il mio server si chiama X ... anzi, si
    chiama Y") asserts TWO distinct names over the turn - the replacive shape - so the
    decider declines and the correction ladder (which supersedes) keeps the turn. This is
    what keeps a real correction green while the first mention goes STATEMENT."""
    (names,) = _it_names("Il mio server si chiama Apollo, anzi si chiama DevBox.")
    assert names is not None and len(set(names)) == 2, names
    assert set(names) == {"apollo", "devbox"}, names


@needs_it
def test_negated_naming_asserts_nothing():
    """Negated naming ("Mio fratello non si chiama Luca") is absence - no name asserted
    (parity with the naming chains negation gate)."""
    (names,) = _it_names("Mio fratello non si chiama Luca")
    assert names == (), names


@needs_it
def test_active_calling_is_not_a_naming_frame():
    """No reflexive clitic -> plain calling ("mia madre chiama Anna ogni giorno") - the
    clitic, not the complement label, is the frame."""
    (names,) = _it_names("Mia madre chiama Anna ogni giorno")
    assert names == (), names


@needs_it
def test_self_naming_and_fresh_domain_frames():
    """Self-naming ("Mi chiamo Marco") names the speaker; a domain nothing in the code or
    the seeds mentions ("La mia barca si chiama Stella") names the boat - grammar, not
    vocabulary."""
    n1, n2 = _it_names("Mi chiamo Marco", "La mia barca si chiama Stella")
    assert n1 == ("marco",), n1
    assert n2 == ("stella",), n2


@needs_it
def test_decider_gate_maps_battery_to_statement_on_the_it_parse():
    """The composition the live battery re-runs: the REAL it parse of the battery sentence
    satisfies the decider == 1 gate (and the anzi turn does not) - the STATEMENT route fires
    on it, the replacive correction keeps its lane."""
    battery, replacive = _it_names(
        "Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5.",
        "Il mio server si chiama Apollo, anzi si chiama DevBox.")
    assert battery is not None and len(battery) == 1, battery
    assert replacive is not None and len(set(replacive)) != 1, replacive


# 4. THE CORRECTION-LANE FIRST-MENTION GUARD (real en deriver + a scripted alias probe)

class _FakeCur:
    """A cursor standing in for the entity_aliases probe: `stored` = aliases that exist."""
    def __init__(self, stored=()):
        self.stored = set(stored)
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql

    def fetchone(self):
        return ("row",) if self.stored else None


@_en
def test_guard_fires_on_a_first_mention_naming_turn():
    """EN battery equivalent: naming edges (owns + pref_name + ip), one name per subject,
    nothing stored -> the correction has nothing to supersede -> the guard fires."""
    import src.api.main as M
    assert M._first_mention_naming_correction(
        "My main server is called DevBox, its IP is 10.0.1.5.", _FakeCur()) is True


@_en
def test_guard_declines_when_the_bearer_is_stored():
    """A stored bearer = a plausible rename target - today correction lane owns it."""
    import src.api.main as M
    cur = _FakeCur(stored={"main server"})
    assert M._first_mention_naming_correction(
        "My main server is called DevBox, its IP is 10.0.1.5.", cur) is False


@_en
def test_guard_declines_the_cued_repair():
    """A marked repair ("Actually ..., not ...") keeps today path - the cue probe runs first."""
    import src.api.main as M
    assert M._first_mention_naming_correction(
        "Actually my sister's name is Thorne, not Wrenna.", _FakeCur()) is False


def test_guard_declines_a_two_name_replacive_turn(monkeypatch):
    """One subject asserting TWO names in one turn is the replacive shape - never a first
    mention (deriver stubbed: the decision is the guard own)."""
    import src.api.main as M
    from src.extraction import linguistics as _L

    class _F:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o

    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(_L, "derive_sentence_facts", lambda _t, _r, **k: [
        _F("server", "pref_name", "apollo"),
        _F("server", "pref_name", "devbox"),
    ])
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    assert M._first_mention_naming_correction("anzi si chiama DevBox", _FakeCur()) is False


def test_guard_declines_without_naming_edges(monkeypatch):
    """No pref_name/also_known_as edge -> not a naming turn -> today path."""
    import src.api.main as M
    from src.extraction import linguistics as _L

    class _F:
        def __init__(self, s, r, o):
            self.subject, self.rel_type, self.object = s, r, o

    monkeypatch.setattr(M, "_spacy_cue_route", lambda _t, _u=None: (None, None))
    monkeypatch.setattr(_L, "derive_sentence_facts", lambda _t, _r, **k: [
        _F("user", "age", "34"),
    ])
    monkeypatch.setattr(_L, "linguistics_available", lambda: True)
    assert M._first_mention_naming_correction("ho 34 anni", _FakeCur()) is False


# 5. THE MCP HANDOFF: remember_facts routes the declined correction through STATEMENT

_UID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_BATTERY = "Il mio server principale si chiama DevBox, il suo IP e 10.0.1.5."


@pytest.fixture(autouse=True)
def _pin_ready():
    """Skip the URL-probe + provisioning gate so every request stays inside the mocks
    (tests/mcp/test_remember_facts_failsafe.py convention)."""
    import src.mcp.server as S
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_UID)
    yield
    S._provisioned_users.discard(_UID)


def _run_handoff(monkeypatch, retract_body):
    """Drive remember_facts_tool with a CORRECTION classification whose retract POST answers
    retract_body; spy both lanes. Returns (result, retract_calls, spine_calls)."""
    import src.mcp.server as S

    async def _classify(text, user_id, **k):
        return ("CORRECTION", 0.95, 0.70)

    retract_calls = []

    async def _retract(text, user_id, **k):
        retract_calls.append(text)
        return dict(retract_body)

    spine_calls = []

    async def _spine(text, user_id, **k):
        spine_calls.append(text)
        return {"status": "stored", "committed": 3}

    async def _retention(text, user_id, intent=None, source=None):
        return "stored"

    async def _harvest(text, user_id, source=None):
        return 1

    monkeypatch.setattr(S, "_classify_and_gate", _classify)
    monkeypatch.setattr(S, "retract_fact_tool", _retract)
    monkeypatch.setattr(S, "_ingest_statement_via_spine", _spine)
    monkeypatch.setattr(S, "_episodic_capture", _retention)
    monkeypatch.setattr(S, "_harvest_turn_facts", _harvest)
    monkeypatch.setattr(S, "_passes_ingest_gate", lambda _t: True)
    monkeypatch.setattr(S, "_statement_extractor_route", lambda _u: asyncio.sleep(0, "spine"))
    result = asyncio.run(S.remember_facts_tool(_BATTERY, _UID))
    return result, retract_calls, spine_calls


def test_first_mention_handoff_routes_back_through_the_statement_path(monkeypatch):
    """THE HANDOFF PIN: the backend declined the correction (first_mention_naming - nothing
    was written) -> remember_facts falls through to the STATEMENT pipeline (spine capture:
    owns + alias + IP in one call), and does NOT return the retract body. No loop: exactly
    one classification, one retract POST, one statement ingest."""
    result, retract_calls, spine_calls = _run_handoff(
        monkeypatch, {"status": "first_mention_naming", "message": "declined"})
    assert retract_calls == [_BATTERY]
    assert spine_calls == [_BATTERY]
    assert result.get("status") == "stored", result


def test_a_real_correction_is_returned_verbatim(monkeypatch):
    """Counter-pin: a normal corrected body is surfaced as-is - the handoff triggers only on
    the guard own status, never on the correction lane answers."""
    result, retract_calls, spine_calls = _run_handoff(
        monkeypatch, {"status": "corrected", "facts_superseded": 1, "committed": 0})
    assert result.get("status") == "corrected", result
    assert spine_calls == []


def test_handoff_status_is_not_a_failure():
    """A model-invoked retract_fact that receives the handoff must not see it stamped as a
    fault - it is a routing answer (the query_detected class), and the message tells the
    model the statement path owns the turn."""
    import src.mcp.server as S
    assert S.status_is_failure("first_mention_naming") is False


# 6. FLAGS + THE EN STATEMENT-LANE FIXTURE (the parity anchor)

def test_preroute_and_guard_flags_default_on():
    """Both kill-switches ship ON (bisectable OFF = byte-for-byte legacy)."""
    import src.api.main as M
    assert M.NAMING_STATEMENT_PREROUTE is True
    assert M.NAME_FIRST_MENTION_GUARD is True


@_en
def test_en_fixture_statement_lane_captures_owns_alias_and_ip():
    """PARITY FIXTURE (en, the measured-STATEMENT branch): the statement lane the handoff
    feeds captures owns + alias + IP in ONE call - exactly the content FOSS-011 lost. The it
    battery rides the same naming chains (migrations 282), which the it-model pins above and
    the critic live battery verify."""
    import datetime
    ref = datetime.datetime(2026, 10, 17)
    facts = L.derive_sentence_facts(
        "My main server is called DevBox, its IP is 10.0.1.5.", ref) or []
    triples = {(f.subject, f.rel_type, (f.object or "").strip().lower()) for f in facts}
    assert ("user", "owns", "main server") in triples, triples
    assert ("main server", "pref_name", "devbox") in triples, triples
    assert ("main server", "ip", "10.0.1.5") in triples, triples
    assert any(getattr(f, "preferred_label", False) and f.rel_type == "pref_name"
               for f in facts), triples