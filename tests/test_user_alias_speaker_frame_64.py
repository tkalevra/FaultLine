"""#64 — a name of the USER must come from the speaker's own first-person naming frame.

A document import filed "what", "does", "every", "gauntlet loop" and "matt shumer" as aliases of the
user (preference_source='rel_default'). The single writer that stamps rel_default is the alias block
of main.py::_ingest_impl; it now consults ``_user_alias_write_refusal`` →
``linguistics.speaker_naming_licence`` before any registry write.

Revert-red: on the base tree ``speaker_naming_licence`` / ``_user_alias_write_refusal`` do not exist
(behaviour-level getattr, so the pin reds with a message rather than on collection) and the ingest
site registers every rel_default alias on the user.

Fresh-domain control: the licence is grammar, not vocabulary — a name the pins never mention, in
frames of a different shape (naming verb, inversion, grown naming noun), is licensed, and an
open-class PROPN in a third party's clause from an unrelated domain is refused.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

spacy = pytest.importorskip("spacy")
try:
    spacy.load("en_core_web_sm")
except Exception:  # noqa: BLE001
    pytest.skip("en_core_web_sm not installed", allow_module_level=True)

from src.extraction import linguistics as L  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _prod_linguistics():
    """Production values (Dockerfile SPACY_MODEL=en_core_web_sm, compose SPINE_NAMING_CHAIN=true),
    set on the module and undone afterwards — never module-level os.environ (#63)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(L, "LINGUISTIC_LAYER", True)
        mp.setattr(L, "_SPACY_MODEL", "en_core_web_sm")
        mp.setattr(L, "_nlp", None)
        mp.setattr(L, "_load_attempted", False)
        mp.setattr(L, "SPINE_NAMING_CHAIN", True, raising=False)
        yield

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "src" / "api" / "main.py"


def _licence():
    fn = getattr(L, "speaker_naming_licence", None)
    assert fn is not None, "#64: linguistics.speaker_naming_licence missing (base behaviour)"
    return fn


# ── the #64 wound: document-shaped text never names the user ─────────────────────────────────────
_DOC = ("What does every gauntlet loop need? Matt Shumer built the Gauntlet Loop "
        "and every agent runs it.")


@pytest.mark.parametrize("alias", ["what", "does", "every", "matt shumer", "gauntlet loop"])
def test_document_surfaces_never_license_a_user_alias(alias):
    ok, why = _licence()(_DOC, alias)
    assert not ok, f"{alias!r} licensed as a user name ({why})"


@pytest.mark.parametrize("text,alias", [
    ("What does every gauntlet loop need?", "what"),     # WP
    ("What does every gauntlet loop need?", "does"),     # AUX
    ("What does every gauntlet loop need?", "every"),    # DET
])
def test_closed_class_refused_by_pos(text, alias):
    ok, why = _licence()(text, alias)
    assert not ok and why.startswith("closed_class"), (alias, why)


def test_third_party_propn_refused():
    ok, _ = _licence()("I met Matt Shumer yesterday.", "matt shumer")
    assert not ok


# ── the speaker's own frames still license (no regression on "I'm Chris") ────────────────────────
@pytest.mark.parametrize("text,alias", [
    ("I'm Chris.", "chris"),
    ("I am Christopher and I live in Riverton.", "christopher"),
    ("I am called Chris.", "chris"),
    ("People call me Chris.", "chris"),
    ("Chris is my name.", "chris"),
    # #93 frames (used by the repair's chat-side judgement)
    ("My name's Chris.", "chris"),
    ("Name's Chris.", "chris"),
    ("This is Chris speaking.", "chris"),
    ("Chris here.", "chris"),
    ("It's Chris.", "chris"),
])
def test_speaker_frames_license(text, alias):
    ok, why = _licence()(text, alias)
    assert ok, (text, why)


# ── fresh-domain control ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,alias,expect", [
    ("My nickname is Wrenfield.", "wrenfield", True),          # grown naming noun, unseen name
    ("Everyone calls me Oksana.", "oksana", True),              # naming verb, unseen name
    ("Oksana repaired the harbour crane.", "oksana", False),    # third party's clause
    ("The harbour crane lifts every container.", "every", False),
])
def test_fresh_domain_control(text, alias, expect):
    ok, why = _licence()(text, alias)
    assert ok is expect, (text, alias, why)


# ── the ingest writer consults the gate before the registry write ────────────────────────────────
def _impl_src() -> str:
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ingest_impl":
            return ast.get_source_segment(_MAIN.read_text(), node) or ""
    return ""


def test_ingest_alias_block_calls_gate_before_register_alias():
    src = _impl_src()
    assert src, "_ingest_impl not found"
    gate = src.find("_user_alias_write_refusal(")
    assert gate != -1, "#64: _ingest_impl never consults _user_alias_write_refusal (base)"
    stamp = src.find("pref_source = 'rel_default'")
    reg = src.find("registry.register_alias,")
    assert stamp != -1 and reg != -1
    assert stamp < gate < reg, "the gate must sit between the rel_default stamp and the write"


def test_gate_function_policy():
    tree = ast.parse(_MAIN.read_text())
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "_user_alias_write_refusal"), None)
    assert fn is not None, "#64: _user_alias_write_refusal missing (base)"
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(_MAIN), "exec"), ns)  # noqa: S102
    g = ns["_user_alias_write_refusal"]
    kw = dict(is_correction=False)
    # OWNER RULING #86: the document / artefact lane never names the user, whatever the frame
    assert g(_DOC, "what", alias_on_user=True, pref_source="rel_default", source="document", **kw)
    assert g("I'm Matt Shumer.", "matt shumer", alias_on_user=True, pref_source="user_stated",
             source="document", **kw)
    assert g("My name is Chris.", "chris", alias_on_user=True, pref_source="user_stated",
             source="api", doc_lane=True, **kw), "a source_ref citation marks the document lane"
    assert g("My name is Chris.", "chris", alias_on_user=True, pref_source="user_stated",
             source="document", is_correction=True)
    # another entity's alias: untouched
    assert g(_DOC, "gauntlet loop", alias_on_user=False, pref_source="rel_default",
             source="document", **kw) is None
    # chat (#93): no first-person licence demanded; only closed-class tokens refused
    for text, alias in (("My name's Chris.", "chris"), ("Name's Chris.", "chris"),
                        ("This is Chris speaking.", "chris"), ("Chris here.", "chris"),
                        ("It's Chris.", "chris"), ("I'm Chris.", "chris"),
                        ("call me May", "may")):
        assert g(text, alias, alias_on_user=True, pref_source="rel_default",
                 source="mcp", **kw) is None, text
    assert g("What does every gauntlet loop need?", "what", alias_on_user=True,
             pref_source="rel_default", source="mcp", **kw) == "closed_class_token"


# ═════════════════════════════════════════════════════════════════════════════
# E2E — the real /ingest alias writer + the repair helper, on a throwaway tenant
# ═════════════════════════════════════════════════════════════════════════════
from tests.laneb_e2e_support import (  # noqa: E402
    dsn, e2e_fixture, ingest_edges, rows, state, user_aliases,
)

e2e = e2e_fixture()

_JUNK = ["what", "does", "every", "matt shumer", "gauntlet loop"]


def test_e2e_document_edges_never_alias_the_user(e2e):
    edges = [{"subject": "user", "object": a, "rel_type": "also_known_as"} for a in _JUNK]
    ingest_edges(e2e, _DOC, edges, source="document", source_ref="gauntlet-loop.md")
    got = user_aliases(e2e) & set(_JUNK)
    assert not got, f"#64: document surfaces filed as names of the user: {sorted(got)}"


def test_e2e_speaker_name_still_lands(e2e):
    # the self-predication grounding lane's shape (server.py::_ground_self_predication_facts →
    # /ingest): a rel_default also_known_as on the user, licensed by the turn's own copula frame
    ingest_edges(e2e, "I'm Oksana.",
                 [{"subject": "user", "object": "oksana", "rel_type": "also_known_as"}])
    assert "oksana" in user_aliases(e2e)
    # a closed-class token is refused on chat too
    ingest_edges(e2e, "What does every gauntlet loop need?",
                 [{"subject": "user", "object": "every", "rel_type": "also_known_as"}])
    assert "every" not in user_aliases(e2e)


@pytest.mark.parametrize("text,name", [
    ("My name's Chris.", "chris"), ("Name's Kaveh.", "kaveh"),
    ("This is Ilse speaking.", "ilse"), ("Tomasz here.", "tomasz"), ("It's Marit.", "marit"),
])
def test_e2e_93_chat_self_identification_lands(e2e, text, name):
    """#93: the rows the critic measured, on source=mcp — every one lands (it did on base)."""
    ingest_edges(e2e, text, [{"subject": "user", "object": name, "rel_type": "also_known_as"}])
    assert name in user_aliases(e2e), f"#93: chat self-name {name!r} refused"


def test_e2e_86_document_first_person_never_names_the_user(e2e):
    """Owner ruling #86: even a first-person naming frame inside a document is its author's."""
    ingest_edges(e2e, "Hi, I'm Rosalind Achterberg and this is my essay.",
                 [{"subject": "user", "object": "rosalind achterberg",
                   "rel_type": "also_known_as"}], source="document", source_ref="essay.pdf")
    assert "rosalind achterberg" not in user_aliases(e2e)


def test_e2e_repair_retires_old_junk_and_is_idempotent(e2e):
    import psycopg2
    from src.api import junk_repair
    uid, schema = e2e["user_id"], e2e["schema"]
    # plant the #64 shapes as they sit on a pre-fix seat
    rows(schema, "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
                 "VALUES (%s,'every',false,'rel_default'),(%s,'matt shumer',false,'rel_default'),"
                 "(%s,'topher',false,'rel_default') ON CONFLICT DO NOTHING", (uid, uid, uid))
    # the document chunk was retained BEFORE its aliases were written (the lane order); the chat
    # turns too — the MCP retains every turn verbatim before extraction
    rows(schema, "INSERT INTO episodic_log (user_id, raw_text, source, created_at) VALUES "
                 "(%s, %s, 'document', now() - interval '5 minutes'), "
                 "(%s, 'My nickname is Topher.', 'mcp', now() - interval '5 minutes'), "
                 "(%s, 'Topher here.', 'mcp', now() - interval '4 minutes')",
         (uid, _DOC, uid, uid))
    # a chat alias with no naming frame in its turn ("Topher here") and one whose turn is not
    # retained at all ("oksana", from the earlier test) — neither is document-sourced: kept (#93)
    conn = psycopg2.connect(dsn())
    try:
        fn = getattr(junk_repair, "retire_user_document_junk", None)
        assert fn is not None, "#64: junk_repair.retire_user_document_junk missing (base)"
        cands = fn(conn, schema, apply=False)
        got = {c["alias"] for c in cands if c["table"] == "entity_aliases"}
        assert {"every", "matt shumer"} <= got, cands
        assert "topher" not in got, "a name the speaker stated in conversation must survive"
        assert "oksana" not in got
        fn(conn, schema, apply=True)
        conn.commit()
        again = fn(conn, schema, apply=False)
        assert not [c for c in again if c["table"] == "entity_aliases"], again
    finally:
        conn.close()
    left = user_aliases(e2e)
    assert "topher" in left and "every" not in left and "matt shumer" not in left


def test_e2e_repair_retires_document_content_on_the_user(e2e):
    """Addendum shapes: (user, want, first-person shooter) and appeal='over 10k tokens…' minted from
    a document chunk that never mentions the speaker → retired; the same shape the speaker stated
    in their own clause of the same document ("My favourite genre …") survives."""
    import psycopg2
    from src.api import junk_repair
    uid, schema = e2e["user_id"], e2e["schema"]
    chunk = ("Every gamer wants a first-person shooter. Its appeal is over 10k tokens per second "
             "throughput. My favourite genre is strategy.")
    rows(schema, "INSERT INTO episodic_log (user_id, raw_text, source, source_ref, created_at) "
                 "VALUES (%s, %s, 'document', 'doc-a', now() - interval '5 minutes')",
         (uid, chunk))
    for eid, alias in (("ent-fps", "first-person shooter"), ("ent-strat", "strategy")):
        rows(schema, "INSERT INTO entities (id, entity_type) VALUES (%s,'Concept') "
                     "ON CONFLICT DO NOTHING", (eid,))
        rows(schema, "INSERT INTO entity_aliases (entity_id, alias, is_preferred, "
                     "preference_source) VALUES (%s,%s,true,'unspecified') ON CONFLICT DO NOTHING",
             (eid, alias))
    rows(schema, "INSERT INTO facts (subject_id, object_id, rel_type, fact_class, source_ref) "
                 "VALUES (%s,'ent-fps','likes','A','doc-a'), (%s,'ent-strat','likes','A','doc-a')",
         (uid, uid))
    rows(schema, "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text) "
                 "VALUES (%s,%s,'appeal','over 10k tokens per second throughput')", (uid, uid))
    conn = psycopg2.connect(dsn())
    try:
        cands = junk_repair.retire_user_document_junk(conn, schema, apply=True)
        conn.commit()
        hit = {(c["table"], c.get("object") or c.get("attribute")) for c in cands}
        assert ("facts", "first-person shooter") in hit, cands
        assert ("entity_attributes", "appeal") in hit, cands
        assert ("facts", "strategy") not in hit, "the speaker's own clause is not document junk"
        again = [c for c in junk_repair.retire_user_document_junk(conn, schema, apply=False)
                 if c["table"] != "entity_aliases"]
        assert not again, again
    finally:
        conn.close()
    live = rows(schema, "SELECT object_id FROM facts WHERE subject_id = %s AND "
                        "superseded_at IS NULL AND rel_type = 'likes'", (uid,))
    assert ("ent-strat",) in live and ("ent-fps",) not in live
