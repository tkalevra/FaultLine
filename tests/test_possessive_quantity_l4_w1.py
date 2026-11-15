"""POSSESSIVE-QUANTITY L4 (issue #19 wound 1) — pins.

The owner ruling these pins enforce: the wounded captures landed as ISLANDS; L4 must LINK THE
SUBJECT TO THE MEASUREMENT — the measurement hangs OFF the proper subject node (the substance)
through the ontology rails, the family noun is the ATTRIBUTE, never an entity, and the query is
a dumb walk subject→link→value.

Battery (deploy-#10 rows, DEV/batteries/nine-subject.yaml reconstructions):
  f1  "My bench press personal record is 140 kilograms."  → was (user, owns, bench) +
      (record, weight, '140') — the amount annihilated on an island, unit dropped.
  c1  "My bouillabaisse recipe uses 800 grams of fish."   → was the flat user weld + the
      'recipe use' / 'fish quantity' fragments.
  med "My lisinopril dosage is 10 milligrams."            → #18 must stay byte-identical
      ((lisinopril, quantity, '10 milligrams'), no phrase-entity island).
  variants: genitive fuel efficiency (byte-for-byte), first-person español vocabulary
      (rebinds onto the substance), tire-pressure/chapter-draft over-fire probes (flat legacy).

Requires: SPACY_MODEL=en_core_web_sm and, for the E2E half, POSTGRES_DSN pointing ONLY at a
throwaway test DB (the same discipline as the df18 suite).
"""
import json
import os
import time
import uuid

import pytest

# The LIVE STATEMENT route (the spine) — the same env the deployed composes set. Without this
# the E2E half exercises the LEGACY harvest and the wounds legitimately persist there.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

F1 = "My bench press personal record is 140 kilograms."
F2_Q = "What is my bench press personal record?"
C1 = "My bouillabaisse recipe uses 800 grams of fish."
C2_Q = "How much fish does my bouillabaisse recipe use?"
MED1 = "My lisinopril dosage is 10 milligrams."
MED3_Q = "What is my lisinopril dosage?"
V_FUEL = "My car's fuel efficiency is 7 liters per 100 kilometers."
V_VOCAB = "My español vocabulary is about 2000 words."
V_TIRE = "My tire pressure is 35 psi."       # over-fire probe: a THING head, not a measure head
V_DRAFT = "My chapter draft is 3000 words."  # over-fire probe: same shape, wrong rebind target

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN


def _facts(text):
    from src.extraction.linguistics import derive_sentence_facts
    return [(f.subject, f.rel_type, f.object, f.scalar_datatype)
            for f in derive_sentence_facts(text, None)]


# ── UNIT: the deriver seam ────────────────────────────────────────────────────────────────────────

def test_f1_bench_press_record_hangs_off_the_substance():
    rows = _facts(F1)
    assert ("bench press", "record", "140 kilograms", "string") in rows
    # the island + the detached amount are GONE
    assert not any(s == "user" and r == "owns" and o == "bench" for s, r, o, _ in rows)
    assert not any(s == "record" and r == "weight" for s, r, o, _ in rows)


def test_c1_recipe_quantity_links_through_l4():
    rows = _facts(C1)
    assert ("user", "related_to", "bouillabaisse recipe", None) in rows
    assert ("bouillabaisse recipe", "quantity", "800 grams of fish", "quantity") in rows
    # the split-brain SVO twin and the stripped measure fragment are dropped
    assert not any(r == "use" for s, r, o, _ in rows)
    assert not any(s == "fish" for s, r, o, _ in rows)


def test_med_dosage_shapes_unchanged_issue18():
    for text, substance in ((MED1, "lisinopril"),
                            ("My lisinopril dose is 10 milligrams.", "lisinopril"),
                            ("My thyroid level is 2.1.", "thyroid")):
        rows = _facts(text)
        assert (substance, "quantity", rows and next(o for s, r, o, _ in rows
                                                     if s == substance), "string") \
            in rows or any(s == substance and r == "quantity" for s, r, o, _ in rows), text


def test_genitive_possessor_untouched():
    rows = _facts(V_FUEL)
    assert ("car", "fuel_efficiency", "7 liters per 100 kilometers", "string") in rows


def test_variant_vocabulary_rebinds_onto_substance():
    rows = _facts(V_VOCAB)
    assert ("español", "vocabulary", "about 2000 words", "string") in rows
    # no user weld, no value-minted entity twin
    assert not any(s == "user" for s, r, o, _ in rows
                   if "vocabulary" in (r or ""))


def test_over_fire_probes_stay_flat_legacy():
    # 'pressure'/'draft' are NOT measurement-family members (floor ∪ tenant) — the family gate
    # is the discriminator between "my <X> <measure-noun>" and "my <thing> is N unit"; without
    # it, grammar alone would rebind (chapter, draft, '3000 words') — measured, the shapes are
    # structurally identical to the record/vocabulary sentences.
    for text, attr in ((V_TIRE, "tire_pressure"), (V_DRAFT, "chapter_draft")):
        rows = _facts(text)
        assert ("user", attr, rows and "35 psi" if "35" in text else "3000 words", "string") \
            in rows or any(s == "user" and r == attr for s, r, o, _ in rows), text


def test_flag_off_is_byte_identical_legacy(monkeypatch):
    from src.extraction import linguistics as L
    monkeypatch.setattr(L, "SPINE_POSSESSED_QUANTITY_L4", False)
    # f1 → the pre-lane wound shape: the possessive island + the detached record amount
    rows = _facts(F1)
    assert any(s == "user" and r == "owns" and o == "bench" for s, r, o, _ in rows)
    # MOVED PIN WITH EVIDENCE (moved-pins doctrine): the detached weight twin's subject moved
    # from the bare head 'record' to the FULL NP when c1b81af8 (minted-rel-interrogative #29
    # W2) keyed the measure subject on _np_phrase — its own words: "the measure subject is the
    # FULL NP (_np_phrase) — the bare-head 'plants' twin entity pinned dead". A/B-measured in
    # throwaway worktrees: this assert matched ('record', 'weight', '140') at eb5700da (the
    # battery's introduction, where flag_off was green) and ('bench personal record',
    # 'weight', '140') from c1b81af8 onward. The flag gate itself is UNCHANGED: flag-off F1 is
    # still the possessive island + the detached amount (the pre-lane wound shape at HEAD).
    assert any(s == "bench personal record" and r == "weight" and o == "140"
               for s, r, o, _ in rows)
    # c1 → the flat user weld + the twins
    rows = _facts(C1)
    assert any(s == "user" and r == "bouillabaisse_recipe" for s, r, o, _ in rows)
    assert any(s == "recipe" and r == "use" and o == "fish" for s, r, o, _ in rows)
    assert any(s == "fish" and r == "quantity" and o == "800 grams" for s, r, o, _ in rows)
    # the vocabulary weld + mojibake twin return
    rows = _facts(V_VOCAB)
    assert any(s == "user" and "vocabulary" in (r or "") for s, r, o, _ in rows)


# ── UNIT: the harvest-level structural mint suppression (pure filter) ────────────────────────────

def test_family_mint_suppression_drops_the_live_island_shapes():
    import src.api.main as M
    edges = [
        {"subject": "lisinopril", "rel_type": "quantity", "object": "10 milligrams",
         "object_datatype": "string"},                              # the landed family scalar
        {"subject": "user", "rel_type": "owns",
         "object": "lisinopril dosage"},                            # (i) the phrase mint
        {"subject": "lisinopril", "rel_type": "related_to",
         "object": "quantity"},                                     # (ii) the bare canonical
        {"subject": "user", "rel_type": "lisinopril_dosage",
         "object": "10 milligrams"},                                # (iii) the flat weld
        {"subject": "user", "rel_type": "take", "object": "lisinopril"},  # legit — KEPT
    ]
    out = M._suppress_measurement_family_mints(edges)
    kept = {(e["subject"], e["rel_type"], e["object"]) for e in out["edges"]}
    assert out["dropped"] == 3
    assert ("user", "take", "lisinopril") in kept
    assert ("lisinopril", "quantity", "10 milligrams") in kept
    assert ("user", "owns", "lisinopril dosage") not in kept
    assert ("lisinopril", "related_to", "quantity") not in kept
    assert ("user", "lisinopril_dosage", "10 milligrams") not in kept


def test_family_mint_suppression_keeps_the_l4_link_edge():
    import src.api.main as M
    edges = [
        {"subject": "bouillabaisse recipe", "rel_type": "quantity",
         "object": "800 grams of fish", "object_datatype": "quantity"},
        {"subject": "user", "rel_type": "related_to", "object": "bouillabaisse recipe"},
    ]
    out = M._suppress_measurement_family_mints(edges)
    assert out["dropped"] == 0
    assert len(out["edges"]) == 2


def test_family_mint_suppression_no_family_scalar_is_noop():
    import src.api.main as M
    edges = [
        {"subject": "user", "rel_type": "owns", "object": "record collection"},
        {"subject": "user", "rel_type": "level", "object": "advanced"},
    ]
    assert M._suppress_measurement_family_mints(edges)["dropped"] == 0


# ── E2E: the battery read back through the REAL harvest → /ingest → /query ──────────────────────

_E2E_STATE: dict = {}


@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestE2EBattery:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        # conftest.py's baseline turns SAAS_MODE ON for the whole session; under SaaS the
        # harvest is FAIL-CLOSED without a bound tenant brain (BYO-LLM), which withholds the
        # turn at door zero — the dz14/df18 precedent: this lane pins the OPEN-CORE engine
        # seam, so SaaS is off for this class's scope only (manual save/restore because the
        # fixture is CLASS-scoped and monkeypatch is function-scoped).
        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)

        pytest.main_mod = M
        M._INGEST_ENABLED = True
        # The df18 precedent: pin the DETERMINISTIC decomposer so the pin is a deterministic
        # replica of the deployed clean-atom condition (the local box has no LLM brain, so the
        # atomizer-down fallback would exercise a different segmentation than any deploy).
        M.SPINE_DETERMINISTIC_SEGMENTATION = True

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
                for text in (F1, C1, MED1, V_FUEL, V_VOCAB):
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

    def _attrs(self):
        # ANY alias (preferred-first), not preferred-ONLY: the hard-line demotion un-preferences
        # a bare common-noun alias (bare_common_type_noun_is_not_a_name) while the entity keeps
        # it as its sole display surface — a preferred-only lookup would render the UUID.
        return {(alias, attr, val) for alias, attr, val in _E2E_STATE["rows"](
            "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
            "a.entity_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), a.entity_id::text), "
            "a.attribute, a.value_text FROM entity_attributes a")}

    def _aliases(self):
        return {r[0] for r in _E2E_STATE["rows"]("SELECT alias FROM entity_aliases")}

    def _facts_rows(self):
        return _E2E_STATE["rows"](
            "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
            "f.subject_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.subject_id::text), "
            "f.rel_type, COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
            "f.object_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.object_id::text) "
            "FROM facts f")

    def test_battery_rows_read_back(self):
        attrs = self._attrs()
        assert ("bench press", "record", "140 kilograms") in attrs
        assert ("bouillabaisse recipe", "quantity", "800 grams of fish") in attrs
        assert ("lisinopril", "quantity", "10 milligrams") in attrs
        assert ("car", "fuel_efficiency", "7 liters per 100 kilometers") in attrs
        assert ("español", "vocabulary", "about 2000 words") in attrs

    def test_no_island_entities_or_aliases(self):
        aliases = self._aliases()
        for island in ("bench", "record", "recipe", "fish", "lisinopril dosage",
                       "2000 words", "bouillabaisse recipe use"):
            assert island not in aliases, f"island alias minted: {island}"
        for subj, rel, obj in self._facts_rows():
            assert not (subj == "user" and rel in ("owns",) and obj == "bench"), \
                "the (user, owns, bench) island survived"

    def test_recipe_links_to_user_through_l4(self):
        assert any(s == "user" and o == "bouillabaisse recipe"
                   for s, r, o in self._facts_rows()), \
            "the recipe must be walkable from the user (the L4 link)"

    def test_paired_interrogatives_render_the_values(self):
        for q, needle in ((F2_Q, "140"), (C2_Q, "800"), (MED3_Q, "10")):
            resp = _E2E_STATE["client"].post("/query", json={"text": q, "user_id": _E2E_STATE["user_id"]})
            assert resp.status_code == 200, resp.text[:300]
            facts = resp.json().get("facts") or []
            blobs = json.dumps(facts)
            assert needle in blobs, f"{q!r} did not render {needle}: {blobs[:400]}"
