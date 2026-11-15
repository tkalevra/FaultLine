"""KINSHIP-GENITIVE-LIVE (issue #19 wound 3) — pins.

The wound (deploy-#10 fam3, reproduced in-process with the live atomizer before the fix —
DEV/review/gauntlet/kinship-genitive-live/round_1/repro.txt):

  "My mother's name is Elaine and she was born on April 12th, 1958."
    → captured NOTHING: no mother entity, no elaine↔user kin row, no born_on
      (only a dangling `elaine` alias from the second clause's born edge, which
      itself mis-canonicalized `born → born_in` — a {Location}-tailed rel — and
      landed as a junk attribute).

TWO adjudicated seams:

  1. linguistics.py::_copular_name_complement — the LLM atomizer emits the first
     clause as a PERIOD-FINAL atom ("My mother's name is Elaine."), and in that
     parse spaCy tags the PROPN name ``acomp`` (adjectival-complement slot)
     instead of ``attr``; the helper admitted only attr/oprd/dobj/obj, so the
     genitive-name chain declined its own canonical sentence and the whole atom
     derived zero edges (derive_residue_uncovered mother/name/Elaine). The fix
     admits an ``acomp`` complement ONLY when it is a PROPN carrying the
     GLiNER2 PERSON typing (``_build_typed_doc`` writes it onto the deriver's
     Doc on the harvest path) — measured on the full doctrine sweep: 0/25
     proper adjectives admitted (Person-typed ones tag ADJ/JJ; PROPN-tagged
     ones type Location/NORP), names in the slot type Person 0.966-1.0.

  2. main.py::_canonical_rel_for_scalar_object — the spine's dated-passive lane
     names its rel from the participle surface, and the seeded alias
     ``born → born_in`` canonicalized the DATE-SCALAR object into a
     {Location}-tailed rel (a date can never occupy a Location slot).
     rel_type_aliases is UNIQUE(alias), so the ambiguity is dispatched at the
     canonicalize seam instead: a scalar-typed object resolves through the
     stem family's SCALAR-tailed rel (born + '1958-04-12' → born_on).

The owner ruling these pins enforce: L4 LINKS the subject to the measurement —
Elaine is the mother (the kin edge + the `mother` role-alias on the NAMED
person, the name at the naming layer per THE HARD LINE), and her born_on hangs
off that same person; the query is a dumb walk.

Requires: SPACY_MODEL=en_core_web_sm and, for the E2E half, POSTGRES_DSN
pointing ONLY at a throwaway test DB (df18/w1/w2 discipline).
"""
import datetime
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")
os.environ.setdefault("SPINE_NAMING_CHAIN", "true")  # every deploy compose sets it true

FAM3_ATOMS = "My mother's name is Elaine. She was born on April 12th, 1958."
# the live compound (the battery's own surface; the deterministic decomposer splits it)
FAM3_COMPOUND = "My mother's name is Elaine and she was born on April 12th, 1958."
FAM3_NAME_Q = "What is my mother's name?"
FAM3_BORN_Q = "When was my mother born?"
SISTER = "My sister Diane lives in Toronto with her dog Biscuit."
LANDLORD = "My landlord's name is Amara. She was born on June 2nd, 1970."
FATHER = "My father's name is Robert. He was born on March 3rd, 1955."

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN

_REF = datetime.datetime(2026, 9, 20, 12, 0, 0)


def _typed_doc(sent, name, label="PERSON"):
    """The deriver's Doc with ONE GLiNER2-typed span seeded, exactly as
    ``_build_typed_doc`` hands it over on the harvest path (deterministic stand-in
    for the real GLiNER2 pass, which types these names Person at 0.966-1.0)."""
    from src.extraction.linguistics import _parse
    doc = _parse(sent)
    i = doc.text.find(name)
    assert i >= 0, name
    sp = doc.char_span(i, i + len(name), label=label, alignment_mode="contract")
    assert sp is not None
    doc.set_ents([sp])
    return doc


def _facts(doc_or_text, prior=None):
    from src.extraction.linguistics import derive_sentence_facts
    return [(f.subject, f.rel_type, f.object)
            for f in derive_sentence_facts(doc_or_text, _REF, prior)]


# ── UNIT (no DB): the genitive chain on the period-final atom ────────────────────────────────────

def test_genitive_atom_with_period_binds_the_named_person():
    # THE WOUND SHAPE: the atomizer's own output ("My mother's name is Elaine.") parses
    # the PROPN name as acomp — the chain declined (zero edges) before the fix.
    rows = _facts(_typed_doc("My mother's name is Elaine.", "Elaine"))
    assert ("elaine", "parent_of", "user") in rows
    # the role-alias leg: the ROLE is an alias on the NAMED person (THE HARD LINE — the
    # name never becomes a place, the role never becomes an entity)
    assert ("elaine", "also_known_as", "mother") in rows


def test_genitive_atom_acomp_variant_name():
    # Emily is the other measured acomp-parsing name population member
    rows = _facts(_typed_doc("My mother's name is Emily.", "Emily"))
    assert ("emily", "parent_of", "user") in rows
    assert ("emily", "also_known_as", "mother") in rows


def test_attr_parse_still_binds_without_typing():
    # mid-sentence / no-period shape (attr slot) is the LEGACY arm — unchanged
    rows = _facts("My mother's name is Elaine")
    assert ("elaine", "parent_of", "user") in rows


def test_untyped_acomp_atom_still_declines():
    # FAIL-SAFE PARITY: without the GLiNER2 typing (str-fallback path) the acomp arm
    # must NOT fire — the discriminator is the typing, and a miss declines (today's
    # behaviour), never guesses.
    assert _facts("My mother's name is Elaine.") == []


def test_proper_adjective_location_typed_is_refused():
    # GLiNER2 types Norwegian Location 0.997 (measured): a PROPN-tagged proper
    # adjective with the real typing does NOT mint a relative
    assert _facts(_typed_doc("My mother's name is Norwegian.", "Norwegian",
                             label="LOCATION")) == []


def test_person_typed_adjective_is_refused_by_pos():
    # Shakespearean types Person 0.853 (measured) BUT tags ADJ/JJ — the POS gate
    # refuses it; no person is minted by the naming chain (the pre-existing
    # decline-path (role, name, value) edge is today's behaviour, out of footprint)
    rows = _facts(_typed_doc("My mother's name is Shakespearean.", "Shakespearean"))
    assert not any(s == "shakespearean" and r == "parent_of" for s, r, o in rows)


def test_acomp_arm_requires_person_not_other_typings():
    # the SAME acomp parse (Emily-shaped) with a NON-Person typing declines: the
    # discriminator is the PERSON typing, not PROPN-hood alone. (A PROPN in the
    # attr slot — "…name is Toronto." — is the LEGACY arm's pre-existing
    # permissiveness, out of this lane's footprint.)
    assert _facts(_typed_doc("My mother's name is Emily.", "Emily",
                             label="LOCATION")) == []


def test_passive_born_edge_carries_the_date_datatype():
    # the upstream contract Seam 2 dispatches on: the dated passive emits the
    # participle-surface rel with a DATE-typed scalar object (GREEN pre-fix — it
    # pins the INPUT shape, the mis-aliasing was downstream)
    from src.extraction.linguistics import derive_sentence_facts
    facts = derive_sentence_facts("She was born on April 12th, 1958.", _REF, ["Elaine"])
    assert any(f.subject == "elaine" and f.rel_type == "born"
               and f.object == "1958-04-12" and f.scalar_datatype == "date"
               for f in facts)


def test_place_variant_is_not_the_date_lane():
    # "born in Toronto" is the PP/place variant (rel bear_in, ENTITY object) — it must
    # NOT be captured by the dated-passive lane nor dispatch to a scalar rel
    rows = _facts("She was born in Toronto.", prior=["Elaine"])
    assert not any(r == "born" for s, r, o in rows)


# ── E2E (throwaway DSN): the rows + the rendered recall + controls ───────────────────────────────

_E2E_STATE: dict = {}


@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestKinshipGenitiveLiveE2E:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        # conftest pins SAAS_MODE=1 for the session; the open-core harvest is FAIL-CLOSED
        # without a bound tenant brain — pop it for this class only (df18/w1/w2 precedent).
        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)

        M._INGEST_ENABLED = True
        M.SPINE_DETERMINISTIC_SEGMENTATION = True  # w2 precedent — no LLM brain needed

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True

        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        conn = psycopg2.connect(_DSN)
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
        conn.close()
        _E2E_STATE["schema"] = schema
        _E2E_STATE["user_id"] = user_id

        def rows(sql, params=()):
            c = psycopg2.connect(_DSN)
            try:
                with c.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}")
                    cur.execute(sql, params)
                    return cur.fetchall()
            finally:
                c.close()

        _E2E_STATE["rows"] = rows
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 120:
                    time.sleep(0.5)
                assert _L._nlp is not None
                _E2E_STATE["client"] = client
                # battery order: the period-final ATOMS first (the live atomizer's own
                # output — segment_clauses splits a two-sentence turn at the period),
                # then the live compound surface, then the controls on the same tenant.
                for text in (FAM3_ATOMS, FAM3_COMPOUND, FATHER, SISTER, LANDLORD):
                    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
                    assert h.status_code == 200, h.text[:300]
                    r = client.post("/ingest", json={
                        "text": text, "user_id": user_id,
                        "edges": h.json().get("edges") or [], "source": "mcp"})
                    assert r.status_code == 200, r.text[:300]
                yield
        finally:
            if _had_saas:
                os.environ["SAAS_MODE"] = _saas_val

    def _aliases_of(self, alias):
        return _E2E_STATE["rows"](
            "SELECT e.id FROM entity_aliases ea JOIN entities e ON e.id = ea.entity_id "
            "WHERE ea.alias = %s", (alias,))

    def test_mother_rows_landed(self):
        # Elaine exists, the kin edge landed Class A, born_on is her scalar, and the
        # `mother` role-alias rides the SAME entity (the name binds as the mother's
        # name — alias on the mother entity per the hard line)
        elaine = self._aliases_of("elaine")
        assert elaine, "elaine entity missing"
        kin = _E2E_STATE["rows"](
            "SELECT f.rel_type, f.fact_class FROM facts f WHERE f.subject_id = %s "
            "AND f.rel_type = 'parent_of'", (elaine[0][0],))
        assert kin and kin[0][1] == "A", kin
        attr = _E2E_STATE["rows"](
            "SELECT value_text, value_date FROM entity_attributes "
            "WHERE entity_id = %s AND attribute = 'born_on'", (elaine[0][0],))
        assert attr and attr[0][0] == "1958-04-12", attr
        mother = self._aliases_of("mother")
        assert mother and mother[0][0] == elaine[0][0], "mother role-alias not on elaine"

    def test_no_born_in_junk_attribute(self):
        # the mis-canonicalized {Location}-tailed attribute must NOT exist
        assert not _E2E_STATE["rows"](
            "SELECT 1 FROM entity_attributes WHERE attribute = 'born_in'")

    def test_recall_name_renders_elaine(self):
        j = _E2E_STATE["client"].post("/query", json={
            "text": FAM3_NAME_Q, "user_id": _E2E_STATE["user_id"]}).json()
        _all = ((j.get("response") or "") + " " + " ".join(
            str(f.get("definition") or "") + " " + str(f.get("object") or "")
            for f in (j.get("facts") or []))).lower()
        assert "elaine" in _all, j.get("response")

    def test_recall_born_renders_the_date(self):
        j = _E2E_STATE["client"].post("/query", json={
            "text": FAM3_BORN_Q, "user_id": _E2E_STATE["user_id"]}).json()
        _all = ((j.get("response") or "") + " " + " ".join(
            str(f.get("definition") or "") + " " + str(f.get("object") or "")
            for f in (j.get("facts") or [])))
        assert "1958-04-12" in _all, j.get("response")

    def test_she_not_rebound_father_keeps_his_own_date(self):
        # the directional control: Robert (father) keeps HIS born_on, Elaine keeps hers
        robert = self._aliases_of("robert")
        assert robert
        r_attr = _E2E_STATE["rows"](
            "SELECT value_text FROM entity_attributes WHERE entity_id = %s "
            "AND attribute = 'born_on'", (robert[0][0],))
        assert r_attr and r_attr[0][0] == "1955-03-03", r_attr
        elaine = self._aliases_of("elaine")
        e_attr = _E2E_STATE["rows"](
            "SELECT value_text FROM entity_attributes WHERE entity_id = %s "
            "AND attribute = 'born_on'", (elaine[0][0],))
        assert e_attr and e_attr[0][0] == "1958-04-12", e_attr

    def test_sister_appositive_control_unchanged(self):
        # fam1's appositive shape captured fine on live and must keep doing so
        diane = self._aliases_of("diane")
        assert diane
        kin = _E2E_STATE["rows"](
            "SELECT rel_type FROM facts WHERE subject_id = %s OR object_id = %s",
            (diane[0][0], diane[0][0]))
        assert any(r[0] == "sibling_of" for r in kin), kin
        assert self._aliases_of("toronto")

    def test_landlord_genitive_subject_agnostic_control(self):
        # the same frame for a NON-kinship role: Amara binds as the person and the tie
        # lands Class A. DISCLOSED HONESTLY (not pinned, per the lane brief): her
        # born_on does NOT land on amara when spaCy NER mistypes the name NORP
        # (measured: en_core_web_sm tags 'Amara' NORP in this frame → the
        # `_turn_persons` pool is empty → the passive lane's pronoun fallback picks
        # a non-person NP and the born edge files on a junk 'name' entity) — a
        # pre-existing NER/coref seam, NOT this lane's genitive/alias fix; 'landlord'
        # also resolves only the generic `related_to` tie (the role_noun cue rail
        # does not carry it in the seed — tenant-growable).
        amara = self._aliases_of("amara")
        assert amara, "amara entity missing"
        _uid = _E2E_STATE["user_id"]
        tie = _E2E_STATE["rows"](
            "SELECT f.rel_type, f.fact_class FROM facts f "
            "WHERE (f.subject_id = %s AND f.object_id = %s) "
            "   OR (f.subject_id = %s AND f.object_id = %s)",
            (amara[0][0], _uid, _uid, amara[0][0]))
        # EITHER orientation (the atomizer's rewrite varies which chain emits the
        # non-kin tie; the invariant is the Class-A link between user and amara)
        assert any(r[0] == "related_to" and r[1] == "A" for r in tie), tie


# ── UNIT (DSN-gated): the scalar-object alias dispatch ───────────────────────────────────────────

@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestScalarObjectDispatch:
    def test_born_dispatches_to_born_on(self):
        import src.api.main as M
        schema = _E2E_STATE.get("schema")
        if not schema:
            pytest.skip("E2E class did not run first")
        assert M._canonical_rel_for_scalar_object("born", schema=schema) == "born_on"

    def test_no_scalar_tailed_sibling_declines(self):
        import src.api.main as M
        schema = _E2E_STATE.get("schema")
        if not schema:
            pytest.skip("E2E class did not run first")
        # 'founded'/'hired' have no seeded SCALAR-tailed sibling → None → the plain
        # alias / pending_placement growth path keeps today's behaviour
        assert M._canonical_rel_for_scalar_object("founded", schema=schema) is None
        assert M._canonical_rel_for_scalar_object("hired", schema=schema) is None
