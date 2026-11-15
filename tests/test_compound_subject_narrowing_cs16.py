"""CS16 — COMPOUND-SUBJECT NARROWING (issue #16 / gauntlet lane 19).

THE WOUND (#12-measured, disclosed residual): "An adult blue-ringed octopus measures
about 5 centimeters across." — the verb-measure ingest NARROWED the compound subject to
its HEAD noun, so the scalar landed on the `octopus` head type node while the compound
type node (#11-built, the surface the correction addresses) held nothing until corrected.
Ingest and the correction wrote DIFFERENT nodes for the same stated subject; #12's rung-3
unique-holder addressing existed only because of this narrowing.

THE FIX (harvest seam — linguistics.py, the VERB_MEASURE_SCALAR lane's subject recovery):
  • the consumer else-arm reads the FULL NOMINAL (`_np_phrase`, the same recovery the #11
    compound-type lane and `_chain_svo` use — parity: every chain converges on one node by
    construction), never the bare head token;
  • a V2 bind (spaCy mis-tags "measures" NOUN/NNS+ROOT and tears the subject NP's
    restrictive modifiers off the head — they become the ROOT's own children) carries the
    surface recovered AT THE ROOT (`_vm_root_subject_np`: the same compound/amod(+HYPH)
    grammar `_np_modifier_toks` walks, `nmod` excluded — "adult" keeps its residue path).

THE HARD LINE: the scalar is CONTENT on the type node (the #11 ruling "measurement files
ON the type node"); the head keeps its own facts; no name/L4 contamination — the quality
word never mints a node, the compound never instance_of's anything.

CONSEQUENCE PINNED (CS2): the #12 correction chain then addresses via RUNG-1
(subject-scoped — the scalar and the correction subject are the same node); rung-3
unique-holder is NOT needed for this shape (the rung stays for genuinely-unique
cross-entity addressing — untouched).

The E2E runs the REAL harvest → /ingest against the throwaway POSTGRES_DSN tenant
(deterministic segmentation; same harness as tests/test_compound_type_l4). CS2's only
substitution vs the live path is the correction brain (unavailable in-process), mocked to
the shape the live deploy returned — evidenced in the cx12 repro.
"""

import os
import uuid

import pytest

# THE SPINE FLAG IS AN ENV-DEPLOY VALUE, NOT A CODE DEFAULT (CLAUDE.md Critical Truth #1):
# production sets SENTENCE_PIPELINE=true; set it before src.api.main imports so the E2E
# runs the deployed spine route, not the legacy harvest.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

import src.api.main as M  # noqa: E402
from src.extraction.linguistics import (  # noqa: E402
    _np_join,
    _np_phrase,
    _vm_root_subject_np,
)

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")

_KIND = "The blue-ringed octopus is a cephalopod."
_MEASURE = "An adult blue-ringed octopus measures about 5 centimeters across."


# ── helper units (DB-free, parse-driven — grammar only, no word lists) ──────────────────

def _doc(sent):
    from src.extraction.linguistics import _get_nlp
    nlp = _get_nlp()
    if nlp is None:
        pytest.skip("spaCy layer not loaded")
    return nlp(sent)


def test_vm_root_subject_np_recovers_hyphen_compound_from_mistagged_root():
    # THE T2 SHAPE: "measures" mis-tagged NOUN/ROOT tears the modifiers off the head —
    # adult=nmod(ROOT), ringed=amod(ROOT, blue amod across HYPH), octopus=compound(ROOT).
    # The recovery collects the compound/amod chain AT THE ROOT → the #11 compound surface.
    doc = _doc(_MEASURE)
    root = doc[6]                       # "measures"
    assert root.tag_ == "NNS" and root.dep_ == "ROOT"
    child = next(c for c in root.children if c.dep_ == "compound")   # "octopus"
    assert _vm_root_subject_np(root, child) == "blue-ringed octopus"
    # the per-token recovery CANNOT see the ROOT shape — it narrows to the bare head
    assert _np_phrase(child) == "octopus"


def test_vm_root_subject_np_recovers_chain_without_extra_noun():
    # Same mis-tag shape without the nmod sibling: the HYPH compound alone.
    doc = _doc("The blue-ringed octopus measures about 5 centimeters across.")
    root = doc[5]                       # "measures"
    assert root.dep_ == "ROOT"
    child = next(c for c in root.children if c.dep_ == "compound")
    assert _vm_root_subject_np(root, child) == "blue-ringed octopus"


def test_vm_root_subject_np_nmod_stays_excluded():
    # "adult" is nmod of the mis-tagged ROOT — OUTSIDE the #11 compound grammar
    # (_np_modifier_toks walks compound/amod + HYPH advmod only). The quality word keeps
    # today's uncovered-residue path; it never enters the surface.
    doc = _doc(_MEASURE)
    root = doc[6]
    child = next(c for c in root.children if c.dep_ == "compound")
    surface = _vm_root_subject_np(root, child)
    assert surface == "blue-ringed octopus"
    assert "adult" not in surface


def test_vm_root_subject_np_failsafe_to_bare_child():
    # No ROOT-sibling modifiers → the bare compound-child surface (byte-identical fallback).
    doc = _doc("An octopus measures about 5 centimeters.")
    root = next(t for t in doc if t.dep_ == "ROOT")
    child = next(c for c in root.children if c.dep_ == "compound")
    assert _vm_root_subject_np(root, child) == _np_phrase(child) == "octopus"


def test_np_join_hyph_witness_parity():
    # The extracted join keeps _np_phrase's exact HYPH behaviour over a recovered
    # CONTIGUOUS modifier run: the two halves of ONE hyphenated word re-join with the
    # hyphen; plain adjacent recovered tokens join with spaces.
    doc = _doc("The blue-ringed octopus measures.")
    blue, ringed, octopus = doc[1], doc[3], doc[4]
    assert _np_join([blue, ringed]) == "blue-ringed"
    assert _np_join([blue, ringed, octopus]) == "blue-ringed octopus"
    assert _np_join([ringed, octopus]) == "ringed octopus"


def test_np_phrase_uses_extracted_join():
    # _np_phrase routes through the same join — a compound NP still carries the hyphen.
    doc = _doc("The blue-ringed octopus is a cephalopod.")
    head = next(t for t in doc if t.text == "octopus")
    assert _np_phrase(head) == "blue-ringed octopus"


# ── E2E harness (same shape as tests/test_compound_type_l4 / cx12) ──────────────────────

@pytest.fixture()
def cs16_tenant(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    monkeypatch.delenv("SAAS_MODE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(_DSN)
    try:
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                "ready_at) VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
    finally:
        conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(
                f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


def _rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _client_drive(user_id, statements):
    """Real harvest → /ingest per statement turn. Returns nothing; rows are the bar."""
    from starlette.testclient import TestClient
    M.SPINE_DETERMINISTIC_SEGMENTATION = True
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)

    class _D:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def setex(self, k, t, v):
            self.d[k] = v
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    M._idempotency_mgr = _Idem()

    edges_by_turn = []
    with TestClient(M.app) as client:
        from src.extraction import linguistics as _L
        import time
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        assert _L._nlp is not None, "spaCy layer never loaded — the spine would no-op"
        for text in statements:
            h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
            assert h.status_code == 200, h.text[:200]
            edges = h.json().get("edges") or []
            edges_by_turn.append(edges)
            if edges:
                r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                                 "edges": edges, "source": "mcp"})
                assert r.status_code == 200, r.text[:200]
    return edges_by_turn


def _alias_id(schema, surface):
    hits = [eid for (eid, a, _p) in
            _rows(schema, "SELECT entity_id, alias, is_preferred FROM entity_aliases")
            if a == surface]
    assert len(hits) == 1, (surface, hits)
    return hits[0]


# ── CS1 — the T2 scalar lands on the COMPOUND node; the #11 ladder unchanged ────────────

def test_cs1_measure_scalar_lands_on_compound_node_ladder_unchanged(cs16_tenant):
    user_id, schema = cs16_tenant
    edges = _client_drive(user_id, [_KIND, _MEASURE])

    # the harvest itself carries the COMPOUND subject (no narrowing at the seam)
    measure_edges = [e for e in edges[1] if e.get("rel_type") == "measure"]
    assert measure_edges, edges[1]
    assert measure_edges[0]["subject"] == "blue-ringed octopus", edges[1]

    compound = _alias_id(schema, "blue-ringed octopus")
    head = _alias_id(schema, "octopus")
    cephalopod = _alias_id(schema, "cephalopod")

    # THE BAR: the scalar is ON the compound type node — the #11 ruling's "measurement
    # files ON the type node" — and the HEAD keeps its own (empty) scalar slate.
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text, provenance, "
                          "superseded_at FROM entity_attributes")
    assert attrs == [(compound, "measure", "about 5 centimeters", "user_stated", None)], attrs
    assert not any(e == head for (e, *_r) in attrs), attrs

    # the #11 ladder rows are UNCHANGED by this lane:
    facts = _rows(schema, "SELECT subject_id, rel_type, object_id, fact_class FROM facts "
                          "WHERE deleted_at IS NULL")
    assert (compound, "subclass_of", cephalopod, "A") in facts, facts
    staged = _rows(schema, "SELECT subject_id, rel_type, object_id FROM staged_facts "
                           "WHERE deleted_at IS NULL AND promoted_at IS NULL")
    assert (compound, "subclass_of", head) in staged, staged          # the grown head rung
    assert (cephalopod, "subclass_of", _alias_id(schema, "animal")) in staged, staged


# ── CS2 — the #12 correction chain now addresses via RUNG-1 (subject-scoped) ────────────

_EXTRACTION = {
    "subject_name": "blue-ringed octopus",
    "old_rel_type": "measure",
    "old_value": "5 centimeters",
    "new_rel_type": "measure",
    "new_value": "7 centimeters",
    "dimension": "RELATIONAL",
    "confidence": 0.95,
}
_CORRECTION = "Actually, the blue-ringed octopus measures 7 centimeters across, not 5."


def test_cs2_correction_addresses_via_rung1_subject_scoped(cs16_tenant, monkeypatch):
    user_id, schema = cs16_tenant
    _client_drive(user_id, [_KIND, _MEASURE])
    compound = _alias_id(schema, "blue-ringed octopus")

    # RUNG PROOF (direct): the addressing chain on the post-T2 rows resolves the row
    # SUBJECT-SCOPED — rung 1 exact, before any value gate, rung 3 never reached.
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            subj, attr, via = M._address_scalar_target(
                cur, compound, "measure", "5 centimeters", user_id)
    finally:
        conn.close()
    assert (subj, attr, via) == (compound, "measure", "exact"), (subj, attr, via)

    # the correction E2E then lands the SAME way (log: via=exact, never unique_holder)
    from starlette.testclient import TestClient
    from unittest.mock import MagicMock
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)

    class _D:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def setex(self, k, t, v):
            self.d[k] = v
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    M._idempotency_mgr = _Idem()

    cap = MagicMock()
    cap.bind.return_value = cap
    cap.opt.return_value = cap
    monkeypatch.setattr(M, "log", cap)
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION))

    with TestClient(M.app) as client:
        r = client.post("/retract/correct", json={"text": _CORRECTION,
                                                  "user_id": user_id, "attested": True})
        assert r.status_code == 200, r.text[:200]
    out = r.json()

    # FROZEN BAR: the correction SUCCEEDS on the SCALAR axis, same E2E result as cx12.
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    assert out["dimension"] == "SCALAR", out
    assert out["facts_superseded"] >= 1, out

    vias = [c.kwargs.get("via") for c in cap.info.call_args_list
            if c.kwargs.get("via") is not None]
    assert "exact" in vias, [str(c) for c in cap.info.call_args_list]
    assert "unique_holder" not in vias, vias

    # rows: measure=7 centimeters live on the compound; the prior value RETAINED.
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text, provenance, "
                          "superseded_at FROM entity_attributes")
    assert attrs == [(compound, "measure", "7 centimeters", "user_corrected", None)], attrs
    hist = _rows(schema, "SELECT attribute, prior_value_text, new_value_text, action, "
                         "cause FROM entity_attributes_history")
    assert any(h[0] == "measure" and "5" in (h[1] or "") and "7" in (h[2] or "")
               for h in hist), hist

    # the ladder survived the correction untouched
    facts = _rows(schema, "SELECT subject_id, rel_type FROM facts WHERE deleted_at IS NULL")
    assert any(r[1] == "subclass_of" for r in facts), facts


# ── CS3 — subject-agnostic variants behave identically (no noun lists) ──────────────────

def test_cs3_variant_verb_root_full_compound_subject(cs16_tenant):
    # NON-medication, correctly-tagged verb ROOT ("measures" = VERB): the classic V1
    # clause-subject arm recovers the full restrictive compound ("adult king cobra" —
    # adult is dep=compound in this parse), never the bare head "cobra".
    user_id, schema = cs16_tenant
    text = "The adult king cobra measures roughly 3 meters."
    edges = _client_drive(user_id, [text])
    measure_edges = [e for e in edges[0] if e.get("rel_type") == "measure"]
    assert measure_edges, edges[0]
    assert measure_edges[0]["subject"] == "adult king cobra", edges[0]

    attrs = _rows(schema, "SELECT entity_id, attribute, value_text FROM entity_attributes")
    compound = _alias_id(schema, "adult king cobra")
    assert attrs == [(compound, "measure", "roughly 3 meters")], attrs
    # the bare head never became the scalar's host: the attrs equality above already pins
    # the compound as the ONLY holder — a 'cobra' node (if one ever mints) holds nothing.


def test_cs3_variant_mistagged_root_mints_compound_in_pass(cs16_tenant):
    # The V2 mis-tagged shape ALONE on a fresh seat (no kind statement): the compound type
    # node MINTS at the scalar's own subject resolution ("exists or mints in the same
    # pass") and the scalar files ON it — never on the bare head.
    user_id, schema = cs16_tenant
    text = "The blue-ringed octopus measures about 5 centimeters across."
    edges = _client_drive(user_id, [text])
    measure_edges = [e for e in edges[0] if e.get("rel_type") == "measure"]
    assert measure_edges, edges[0]
    assert measure_edges[0]["subject"] == "blue-ringed octopus", edges[0]

    compound = _alias_id(schema, "blue-ringed octopus")
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text FROM entity_attributes")
    assert attrs == [(compound, "measure", "about 5 centimeters")], attrs
    # the bare head is NOT the scalar's host — with no kind statement on this seat, no
    # 'octopus' node even mints (nothing addresses it); the compound is the ONLY holder.
    aliases = _rows(schema, "SELECT entity_id, alias FROM entity_aliases")
    head_ids = [e for (e, a) in aliases if a == "octopus"]
    assert not any(e in head_ids for (e, *_r) in attrs), (attrs, head_ids)


# ── CS4 — THE HARD LINE: scalar is CONTENT on the type node; no name/L4 contamination ───

def test_cs4_hard_line_scalar_is_content_not_name_not_l4(cs16_tenant):
    user_id, schema = cs16_tenant
    _client_drive(user_id, [_KIND, _MEASURE])
    compound = _alias_id(schema, "blue-ringed octopus")
    head = _alias_id(schema, "octopus")

    aliases = _rows(schema, "SELECT entity_id, alias, is_preferred FROM entity_aliases")
    alias_surfaces = {a for (_e, a, _p) in aliases}

    # the quality word NEVER minted a node (it stays the loud uncovered residue)
    assert "adult" not in alias_surfaces, alias_surfaces
    # the scalar VALUE never became an entity (content in entity_attributes, not a name)
    assert "about 5 centimeters" not in alias_surfaces, alias_surfaces

    # the compound's L4 footprint is the ladder ONLY (place edges), the measure is CONTENT
    # (entity_attributes) — the compound never instance_of's ANYTHING (the #11 frozen bar).
    all_rows = _rows(schema, "SELECT subject_id, rel_type, object_id FROM facts "
                             "WHERE deleted_at IS NULL") + \
        _rows(schema, "SELECT subject_id, rel_type, object_id FROM staged_facts "
                      "WHERE deleted_at IS NULL AND promoted_at IS NULL")
    assert not any(s == compound and r == "instance_of" for (s, r, _o) in all_rows), all_rows
    assert all(r == "subclass_of" for (s, r, _o) in all_rows if s == compound), all_rows

    # the head node keeps its own name and its own (empty) scalar slate — never fused
    assert (head, "octopus", True) in aliases, aliases
    attrs = _rows(schema, "SELECT entity_id FROM entity_attributes")
    assert not any(e == head for (e,) in attrs), attrs
    # ...and the compound never carries the head's name (the #11 alias arm)
    assert not any(e == compound and a == "octopus" for (e, a, _p) in aliases), aliases
