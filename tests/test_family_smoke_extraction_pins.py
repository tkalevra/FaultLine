"""BUG-FAMILY-1..4 (owner-bar family-smoke 2026-11-11, live pre-prod seat 3e8dbfeb,
37% raw recall) — the four extraction defects, each pinned by its MEASURED sentence.

The BUG files (DEV/BUGS/family-smoke-20261111/) name the sentences; these pins freeze
the deterministic gates around the LLM extraction so the wounds cannot reopen:

  F1  the pre-classification edge-drop class (7/21 natural multi-fact turns):
      (a) scalar_datatype_preflight DATE coercion — "she was born on March 12, 1990."
          normalized to ISO instead of dropped (flag EDGE_DROP_PER_EDGE_EVAL);
      (b) rel_type_name_collision OBJECT-side defer — (user, owns, cats) judged by
          itself, never by the sibling junk edge that minted rel 'cats';
      (c) user_scalar_overwrite DUPLICATE honesty — the Telecaster turn's serial is a
          duplicate of stored memory, not a refused correction;
      (d) the REGISTER_SCHEDULE_TIER frames — the appointment date/time ("is on the
          15th at 2:30pm"), the drip schedule ("run 20 minutes … at 6am") and the
          backup schedule ("backs up nightly at 2am") land as has_setting rows.
  F2  the attribute-capture "NOT captured" class:
      (a) IDENTIFIER_UNIT_SCALAR — "the VM for Home Assistant is vmid 112" captures
          (vm, vmid, "112") instead of a related_measure blob + a nonsense unit;
      (b) TEMPORAL_DATE_CLAIMANT — "Our passport numbers expire in August 2027" files
          (passport numbers, expire_in, <iso>) and no longer queues 'august' as an
          identifier_noun cue.
  F3  the uncued-rule junk class: REGISTER_HABIT_CUE — "For deploy scripts I always
      use set -euo pipefail so failures cascade loudly." mints
      (deploy scripts, has_setting, "set -euo pipefail"); the junk chain twins
      (user, use, cascade) / (user, set, pipefail) are suppressed by the register
      deconfliction firewall. The negation scan-back contract is pinned unchanged.
  F4  the SS1 fallback gap: CORRECTION_SS1_FALLBACK's (subject, relation) matcher now
      covers the attribute-spec FACTS class — "Actually the Civic takes 0W-16 now…"
      retires the live (civic, take, 0w-20 …) row (real-schema litmus, DSN-gated).

Hermetic pins run everywhere; the store-side litmus class skips without a *_test
POSTGRES_DSN (same contract as tests/test_recall_coverage_frames.py).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("spacy")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

import src.extraction.linguistics as L  # noqa: E402

# ── THE MEASURED SENTENCES (verbatim from the BUG files / the smoke driver) ─────────
FAM1 = "My wife's name is Elara and she was born on March 12, 1990."
PET1 = "We have two cats: Miso is a tabby, and Juniper is a black rescue we adopted in June."
MED2 = "My endocrinologist appointment is on the 15th at 2:30pm, endocrinology clinic, third floor."
GARDEN2 = "Set the drip irrigation to run 20 minutes every morning at 6am during summer."
LAB1 = ("The homelab runs Proxmox on the r730; the VM for Home Assistant is vmid 112 "
        "and backs up nightly at 2am to the NAS.")
MUSIC1 = "My guitar is a 1998 Fender Telecaster in Butterscotch Blonde — serial number DN82147."
CODE1 = "For deploy scripts I always use set -euo pipefail so failures cascade loudly."
TRAVEL1 = "Our passport numbers expire in August 2027 — renew them six months early."
CORR_OIL = "Actually the Civic takes 0W-16 now, Honda changed the spec in the manual."

_REF = __import__("datetime").datetime(2026, 11, 11)


def _facts(text, ref=_REF):
    return L.derive_sentence_facts(L._parse(text), ref) or []


def _triples(text, ref=_REF):
    return [(f.subject, f.rel_type, str(f.object)) for f in _facts(text, ref)]


# ══════════════════════════════════════════════════════════════════════════════════
# F3 — the habit cue (always-adverb frames) + the deconfliction firewall

def test_f3_code1_measured_sentence_mints_the_setting():
    """code1 measured: the rule's VALUE lands on the register shelf, subject = the
    instrumental-for PP the habit speaks through."""
    hits = L.detect_instruction_settings(CODE1, L._parse(CODE1))
    assert ("deploy scripts", "set -euo pipefail", "has_setting") in [
        (s, v, r) for s, v, _st, _en, r in hits], hits


def test_f3_habit_subject_first_shape():
    """The BUG's second habit shape: "I always Y for X" — same mint, value whole."""
    s = "I always use set -euo pipefail for deploy scripts."
    hits = L.detect_instruction_settings(s, L._parse(s))
    assert ("deploy scripts", "set -euo pipefail", "has_setting") in [
        (s2, v, r) for s2, v, _st, _en, r in hits], hits


def test_f3_negation_scan_back_contract_holds():
    """The register's negation contract is unchanged: a negated token is never the value."""
    s = "For deploy scripts I never use bare rm so nothing breaks."
    assert L.detect_instruction_settings(s, L._parse(s)) == []


def test_f3_habit_flag_off_byte_parity(monkeypatch):
    """REGISTER_HABIT_CUE=false → the habit frames are dead entries (no mint)."""
    monkeypatch.setattr(L, "REGISTER_HABIT_CUE", False)
    assert L.detect_instruction_settings(CODE1, L._parse(CODE1)) == []


def test_f3_deriver_emits_only_the_register_mint():
    """The junk chain twins the smoke served ("you Use Cascade") and the shattered value
    ((user, set, pipefail)) are both suppressed; exactly ONE fact leaves the sentence."""
    triples = _triples(CODE1)
    assert triples == [("deploy scripts", "has_setting", "set -euo pipefail")], triples
    assert not any(r == "use" and o == "cascade" for _s, r, o in triples)
    assert not any(r == "set" for _s, r, o in triples)


def test_f3_existing_register_corpus_unchanged():
    """The seeded frames are untouched by the new entries; the rule-cue corpus shape now
    LANDS (BY-DESIGN PIN FLIP, BUG-FAMILY-7 2026-11-12).

    This pin pre-dated the rule-always frame and asserted the lab2 personal-rule shape
    mints NOTHING — the characterization of the exact wound BUG-FAMILY-7 filed (the
    sparse-store litmus's snapshot-rule miss: the RULE cue present, no frame captured the
    value, the recall abstained). The rule-always frame (cue-gated, the for-PP subject
    hint) is the fix, so the lab2 shape now mints the (lab, has_setting, value) row —
    the pin flips WITH the fix instead of asserting the bug forever. The seeded-frame
    invariant below (the "set X to Y" tier) is byte-identical to the pre-flip pin."""
    lab2 = ("Personal rule for the lab: always snapshot before a Proxmox upgrade, "
            "no exceptions.")
    hits = L.detect_instruction_settings(lab2, L._parse(lab2))
    assert ("lab", "snapshot before a Proxmox upgrade", "has_setting") in [
        (s2, v, r) for s2, v, _st, _en, r in hits], hits
    hits = L.detect_instruction_settings("Set Redis maxmemory to 512mb.",
                                         L._parse("Set Redis maxmemory to 512mb."))
    assert ("redis", "512mb.", "has_setting") in [(s, v, r) for s, v, _st, _en, r in hits]


# ══════════════════════════════════════════════════════════════════════════════════
# F1(d) — the schedule tier frames

def test_f1_med2_measured_sentence_lands_the_appointment_datetime():
    hits = L.detect_instruction_settings(MED2, L._parse(MED2))
    assert ("endocrinologist appointment", "the 15th at 2:30pm", "has_setting") in [
        (s, v, r) for s, v, _st, _en, r in hits], hits


def test_f1_garden2_measured_sentence_lands_the_drip_schedule():
    hits = L.detect_instruction_settings(GARDEN2, L._parse(GARDEN2))
    assert ("drip", "20 minutes every morning at 6am", "has_setting") in [
        (s, v, r) for s, v, _st, _en, r in hits], hits


def test_f1_lab1_measured_sentence_lands_the_backup_schedule():
    hits = L.detect_instruction_settings(LAB1, L._parse(LAB1))
    assert ("nightly at 2am", "has_setting") in [(v, r) for _s, v, _st, _en, r in hits], hits


def test_f1_schedule_tier_flag_off_byte_parity(monkeypatch):
    """REGISTER_SCHEDULE_TIER=false → today's extraction, byte-for-byte: no sched value
    anywhere, and garden2 falls back to the 'set' frame's bare-verb mint (the measured
    HEAD shape this tier replaces)."""
    monkeypatch.setattr(L, "REGISTER_SCHEDULE_TIER", False)
    assert L.detect_instruction_settings(MED2, L._parse(MED2)) == []
    hits = L.detect_instruction_settings(GARDEN2, L._parse(GARDEN2))
    assert ("drip", "run", "has_setting") in [(s, v, r) for s, v, _st, _en, r in hits], hits
    assert not any("20 minutes" in v for _s, v, _st, _en, _r in hits), hits


def test_f1_schedule_frames_do_not_hijack_plain_time_mentions():
    """A one-shot time mention with no cadence/appointment frame never mints."""
    for s in ("I met Rachel at 5pm last week.",
              "We moved to Portland in 2019 at great expense."):
        assert L.detect_instruction_settings(s, L._parse(s)) == [], s


# ══════════════════════════════════════════════════════════════════════════════════
# F2(a) — the identifier-unit tier ("vmid 112")

def _wordnet_available() -> bool:
    from src.api.wordnet_ladder import has_common_noun_sense
    return has_common_noun_sense("vmid") is False


@pytest.mark.skipif(not _wordnet_available(),
                    reason="WordNet corpus unavailable — the identifier tier fail-safes to "
                           "the degraded emit (today's behaviour) without it")
def test_f2_lab1_measured_sentence_captures_vmid_112():
    """lab1 measured: (vm, vmid, "112") — the attribute's OWN scalar, no related_measure
    blob, no unit_scalar growth proposal for 'vmid'."""
    growth: list = []
    triples = []
    doc = L._parse("the VM for Home Assistant is vmid 112")
    for f in L.derive_sentence_facts(doc, _REF, growth_out=growth) or []:
        triples.append((f.subject, f.rel_type, str(f.object)))
    assert ("vm", "vmid", "112") in triples, triples
    assert not any(r == "related_measure" and "vmid" in o for _s, r, o in triples), triples
    assert ("vmid", "unit_scalar") not in [tuple(g) for g in growth], growth


@pytest.mark.skipif(not _wordnet_available(), reason="WordNet corpus unavailable")
def test_f2_identifier_tier_flag_off_byte_parity(monkeypatch):
    """IDENTIFIER_UNIT_SCALAR=false → today's degraded emit (related_measure + the
    unit_scalar growth proposal), byte-for-byte."""
    monkeypatch.setattr(L, "IDENTIFIER_UNIT_SCALAR", False)
    growth: list = []
    doc = L._parse("the VM for Home Assistant is vmid 112")
    triples = [(f.subject, f.rel_type, str(f.object))
               for f in L.derive_sentence_facts(doc, _REF, growth_out=growth) or []]
    assert any(r == "related_measure" and "vmid" in o for _s, r, o in triples), triples
    assert ("vmid", "unit_scalar") in [tuple(g) for g in growth], growth


def test_f2_real_unit_stays_a_measure():
    """A genuine (WordNet-word) unit outside the map keeps the degraded measure reading
    — the tier never reclassifies '3 decades'-shaped quantities (the warranty pin)."""
    if L.__dict__.get("_f2_probe_checked") is None:
        pass
    from src.api.wordnet_ladder import has_common_noun_sense
    if has_common_noun_sense("decade") is not True:
        pytest.skip("WordNet corpus unavailable")
    triples = _triples("The warranty on my roof is 3 decades.")
    assert any(r == "related_measure" and "decade" in o for _s, r, o in triples), triples


# ══════════════════════════════════════════════════════════════════════════════════
# F2(b) — the temporal exemption + the unclaimed-date claimant

def test_f2_travel1_measured_sentence_claims_the_expiry():
    """travel1 measured: the resolved date is CLAIMED on the verb's own verb_prep
    predicate, and 'august' is never queued as an identifier_noun cue."""
    growth: list = []
    doc = L._parse(TRAVEL1)
    triples = [(f.subject, f.rel_type, str(f.object))
               for f in L.derive_sentence_facts(doc, _REF, growth_out=growth) or []]
    assert any(s == "passport numbers" and r == "expire_in" and o.startswith("2027-08-01")
               for s, r, o in triples), triples
    assert ("august", "identifier_noun") not in [tuple(g) for g in growth], growth


def test_f2_fam1_month_never_queued_as_identifier_noun():
    """The same wound's fam1 shape: 'March 12, 1990' tokens never queue as identifier
    nouns (the birthday's own capture is the date lane's, already ISO)."""
    growth: list = []
    doc = L._parse(FAM1)
    L.derive_sentence_facts(doc, _REF, growth_out=growth) or []
    assert ("march", "identifier_noun") not in [tuple(g) for g in growth], growth


def test_f2_temporal_claimant_flag_off_byte_parity(monkeypatch):
    """TEMPORAL_DATE_CLAIMANT=false → today's behaviour: the identifier-NP scan queues
    the month token and no expire_in claimant is emitted."""
    monkeypatch.setattr(L, "TEMPORAL_DATE_CLAIMANT", False)
    growth: list = []
    doc = L._parse(TRAVEL1)
    triples = [(f.subject, f.rel_type, str(f.object))
               for f in L.derive_sentence_facts(doc, _REF, growth_out=growth) or []]
    assert ("august", "identifier_noun") in [tuple(g) for g in growth], growth
    assert not any(r == "expire_in" and o.startswith("2027") for _s, r, o in triples), triples


# ══════════════════════════════════════════════════════════════════════════════════
# F1(a) — the preflight DATE coercion tier

def test_f1_natural_date_coerces_to_iso():
    import src.api.main as M
    assert M._coerce_natural_date_scalar("March 12, 1990") == "1990-03-12"
    assert M._coerce_natural_date_scalar("August 2027") == "2027-08-01"
    assert M._coerce_natural_date_scalar("1990-03-12") == "1990-03-12"


def test_f1_unanchored_dates_are_never_coerced():
    """No year in the value → no coercion (the caller keeps today's refusal; a year is
    never guessed at the preflight)."""
    import src.api.main as M
    assert M._coerce_natural_date_scalar("March 12") is None
    assert M._coerce_natural_date_scalar("last march") is None


def test_f1_per_edge_flag_source_pins():
    import inspect
    import src.api.main as M
    src = inspect.getsource(M)
    assert "EDGE_DROP_PER_EDGE_EVAL" in src
    assert "scalar_date_coerced_preflight" in src
    assert "rel_type_collision_object_deferred" in src
    # the duplicate-honesty wording (F1c) is on the drop carrier
    assert "duplicate" in inspect.getsource(M._ingest_impl).lower()


def test_f4_ss1_fact_fallback_source_pins():
    import inspect
    import src.api.main as M
    src = inspect.getsource(M.correct_fact)
    assert "ss1_fact_fallback_retired" in src
    assert "normalize_rel" in src            # the stored-rel fold ('takes' → 'take')
    assert "single-candidate" in src         # the >1 fail-safe is documented at the site


def test_f2_f1_frame_flags_source_pins():
    import inspect
    import src.extraction.linguistics as ling
    src = inspect.getsource(ling)
    for flag in ("REGISTER_HABIT_CUE", "REGISTER_SCHEDULE_TIER",
                 "IDENTIFIER_UNIT_SCALAR", "TEMPORAL_DATE_CLAIMANT"):
        assert flag in src
    assert "always-use" in src and "sched-cadence" in src and "sched-datetime" in src


# ══════════════════════════════════════════════════════════════════════════════════
# F1(b)/(c) + F1(a) ingest-side + F4 — the STORE-side litmus (real schema, DSN-gated)

_DSN = os.environ.get("POSTGRES_DSN", "")


class _D:
    def get(self, k):
        return None

    def setex(self, k, t, v):
        return True

    def ping(self):
        return True


class _Idem:
    redis_url = "dict://test"
    ttl = 3600
    client = _D()

    def generate_key(self, text, user_id, endpoint, **kw):
        import hashlib
        return "idem_" + hashlib.sha256(
            f"{text}|{user_id}|{endpoint}".encode()).hexdigest()[:32]

    def get_or_lock(self, key):
        return True

    def get_cached_response(self, key):
        return None

    def is_duplicate(self, key):
        return False

    def cache_response(self, key, response, ttl_seconds=3600):
        return True

    def release_lock(self, key):
        return True


@pytest.fixture(scope="module")
def seat_env():
    if not _DSN or not _DSN.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("store-side litmus needs a throwaway *_test POSTGRES_DSN (skipped)")
    import uuid
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
    import src.api.main as M
    user_id = str(uuid.uuid4())
    schema, status = create_user_schema(user_id, derive_user_slug_from_uuid(user_id),
                                        psycopg2.connect(_DSN))
    assert status == "ready", status
    conn = psycopg2.connect(_DSN)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, f"{user_id[:8]}@example.invalid", user_id[:8], user_id[:8]))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                    "ready_at) VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING",
                    (user_id, schema))
    conn.commit()
    conn.close()
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    M._idempotency_mgr = _Idem()
    from starlette.testclient import TestClient
    with TestClient(M.app) as client:
        yield {"client": client, "user_id": user_id, "schema": schema, "M": M}
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    conn.close()


def _ingest(env, text, edges):
    r = env["client"].post("/ingest", json={"text": text, "user_id": env["user_id"],
                                            "edges": [e.model_dump() for e in edges],
                                            "source": "mcp"})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _attrs(env):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{env["schema"]}"')
            cur.execute("SELECT attribute, value_text FROM entity_attributes "
                        "WHERE superseded_at IS NULL")
            return set(cur.fetchall())
    finally:
        conn.commit()
        conn.close()


def test_f1a_fam1_measured_date_lands_normalized(seat_env):
    """fam1 measured: an edge carrying the natural-format birthday through the ingest
    preflight LANDS with the ISO value instead of being dropped."""
    from src.api.models import EdgeInput
    res = _ingest(seat_env, FAM1,
                  [EdgeInput(subject="elara", object="March 12, 1990", rel_type="born_on",
                             object_datatype="date", fact_provenance="user_stated"),
                   EdgeInput(subject="elara", object="user", rel_type="spouse",
                             fact_provenance="user_stated")])
    drops = res.get("dropped") or []
    assert not any(d.get("stage") == "scalar_datatype_preflight" for d in drops), drops
    attrs = {a for a, _v in _attrs(seat_env)}
    assert "born_on" in attrs
    vals = {v for a, v in _attrs(seat_env) if a == "born_on"}
    assert any(v.startswith("1990-03-12") for v in vals), vals


def test_f1c_music1_serial_drop_is_honest_duplicate(seat_env):
    """music1 measured: the serial already contained in the stored identifier value is
    dropped as a DUPLICATE of stored memory, never as a refused correction."""
    from src.api.models import EdgeInput
    res = _ingest(seat_env, MUSIC1,
                  [EdgeInput(subject="user", rel_type="has_reference_id",
                             object=("1998 fender telecaster in butterscotch blonde — "
                                     "serial number DN82147"),
                             object_datatype="string", fact_provenance="user_stated"),
                   EdgeInput(subject="user", rel_type="has_reference_id", object="DN82147",
                             fact_provenance="user_stated")])
    drops = res.get("dropped") or []
    serial_drops = [d for d in drops if str(d.get("object", "")).lower() == "dn82147"]
    assert serial_drops, drops
    assert "duplicate" in str(serial_drops[0].get("reason", "")).lower(), serial_drops
    assert "requires stating the turn as a correction" not in \
        str(serial_drops[0].get("reason", ""))


def test_f1b_collision_defer_names_the_object_side(seat_env):
    """F1b: an OBJECT-side collision with an established rel defers to classification —
    the drop (when the registry hard line then refuses the mint) is no longer the
    rel_type_name_collision pre-drop. VEHICLE NOTE (the evidenced-parity ITEM 4,
    REL_TYPE_PLURAL_NORMAL_FORM): the plural sibling surface ('cats') no longer mints
    vocabulary at all — the mint registers the singular class form ('cat'), so the
    plural can no longer BE the collision vehicle BY DESIGN (that shape is pinned in
    tests/test_evidenced_parity_items.py). This pin keeps guarding the SAME
    collision-defer mechanism with a non-plural unknown sibling surface."""
    from src.api.models import EdgeInput
    res = _ingest(seat_env, PET1,
                  [EdgeInput(subject="user", rel_type="purr", object="two",
                             object_datatype="string", fact_provenance="user_stated"),
                   EdgeInput(subject="user", rel_type="participated_in", object="purr",
                             fact_provenance="user_stated")])
    drops = res.get("dropped") or []
    purr_drops = [d for d in drops if d.get("object") == "purr"]
    assert purr_drops, "the colliding edge should surface its refusal honestly"
    stages = {d.get("stage") for d in purr_drops}
    assert "rel_type_name_collision" not in stages, purr_drops


def test_f4_corr_oil_ss1_fact_fallback_retires_the_old_spec(seat_env, monkeypatch):
    """F4 measured (corr-oil): the old 0W-20 spec lives as a live FACTS row
    (civic, take, 0w-20 …); the correction names no old value. The SS1 fallback's
    (subject, relation) matcher now covers the attribute-spec class: the prior row is
    retired deterministically and the response says so."""
    import psycopg2
    from src.api.models import EdgeInput
    M = seat_env["M"]
    res = _ingest(seat_env,
                  "The Civic takes 0W-20 synthetic oil, changed every 8,000 km.",
                  [EdgeInput(subject="civic", rel_type="take", object="0w-20 synthetic oil",
                             fact_provenance="user_stated")])
    assert res.get("status") in ("valid", "partial", "committed"), res

    def _old_row_live():
        conn = psycopg2.connect(_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{seat_env["schema"]}"')
                cur.execute("SELECT f.superseded_at IS NULL FROM facts f "
                            "JOIN entity_aliases a ON a.entity_id = f.subject_id "
                            "LEFT JOIN entity_aliases oa ON oa.entity_id = f.object_id "
                            "WHERE a.alias = 'civic' AND f.rel_type = 'take' "
                            "AND lower(coalesce(oa.alias, '')) LIKE '%0w-20%' "
                            "ORDER BY f.id DESC LIMIT 1")
                row = cur.fetchone()
                return bool(row and row[0])
        finally:
            conn.commit()
            conn.close()

    assert _old_row_live(), "the old spec row must be live before the correction"

    monkeypatch.setattr(
        M, "_unified_correction_extraction_llm",
        lambda **k: {"subject": "Civic", "old_rel_type": "take", "old_value": None,
                     "new_rel_type": "take", "new_value": "0W-16", "dimension": "SCALAR",
                     "confidence": 0.9})
    r = seat_env["client"].post("/retract/correct",
                                json={"text": CORR_OIL, "user_id": seat_env["user_id"],
                                      "attested": True})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body.get("status") in ("corrected", "corrected_unresolved"), body
    msg = str(body.get("message", "")).lower()
    # R5 honesty: a landed retirement is the EFFECT, never "both may show up"
    assert "0w-16" in msg or body.get("status") == "corrected", body
    assert not _old_row_live(), "the old 0W-20 spec row must be retired by the fallback"
    assert not ("both may show up" in msg), body


def test_f4_ss1_flag_off_keeps_the_old_row(seat_env, monkeypatch):
    """CORRECTION_SS1_FALLBACK=false → the facts-row probe never runs (byte-parity):
    the old spec row survives the same correction."""
    import psycopg2
    from src.api.models import EdgeInput
    M = seat_env["M"]
    monkeypatch.setenv("CORRECTION_SS1_FALLBACK", "false")
    _ingest(seat_env, "The Outback takes 0W-20 synthetic oil.",
            [EdgeInput(subject="outback", rel_type="take", object="0w-20 synthetic oil",
                       fact_provenance="user_stated")])
    monkeypatch.setattr(
        M, "_unified_correction_extraction_llm",
        lambda **k: {"subject": "Outback", "old_rel_type": "take", "old_value": None,
                     "new_rel_type": "take", "new_value": "0W-16", "dimension": "SCALAR",
                     "confidence": 0.9})
    r = seat_env["client"].post("/retract/correct",
                                json={"text": "Actually the Outback takes 0W-16 now.",
                                      "user_id": seat_env["user_id"], "attested": True})
    assert r.status_code == 200, r.text[:300]
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{seat_env["schema"]}"')
            cur.execute("SELECT f.superseded_at IS NULL FROM facts f "
                        "JOIN entity_aliases a ON a.entity_id = f.subject_id "
                        "WHERE a.alias = 'outback' AND f.rel_type = 'take' "
                        "ORDER BY f.id DESC LIMIT 1")
            row = cur.fetchone()
            assert row is not None and row[0], \
                "flag OFF: the facts-row probe must not run (the old row stays live)"
    finally:
        conn.commit()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════════
# BUG-FAMILY-8 — the cross-lane date-pin regression class (the blended-family
# person-first reorder vs the date-attr landing). The 2026-11-13 wound report claimed
# the reorder broke the f1/f2 date pins; the measured resolution (this tree): the three
# original pins pass at every tree since the blended landing (126a6943, 09197238,
# 749b00f0, the merge) across hash seeds — the repair the brief demands ("the date
# coercion fires after the person-first binding resolves the owner; the born_on/expiry
# lands on the bound person entity") is the LIVE behavior. What the wound never had is
# the OWNER pin: no test asserted WHO the date lands on. These pins freeze the owner
# contract — the exact surface the filed regression describes. Red-proof: the flag-OFF
# parity arm shows the subject reverts to the bare pronoun the moment the person-first
# arm is lifted (BLENDED_FAMILY_CAPTURE=false → ('she', …)), i.e. the pin guards the
# very arm the filing indicts.

SERAPHINE1 = "My wife's name is Seraphine and she was born on April 9, 1991."


def _born_row(triples, who):
    """The (who, born-participle-or-born_on, ISO) row, if the date landed on ``who``."""
    return [t for t in triples
            if t[0] == who and t[1] in ("born", "born_on")
            and str(t[2]).startswith("199")]


def test_f8_fam1_date_lands_on_the_bound_person():
    """f1: 'she was born on March 12, 1990' — the born date lands on ELARA (the
    person-first binding resolved the owner BEFORE the date row emitted) and never on
    the bare pronoun 'she'."""
    triples = _triples(FAM1)
    assert _born_row(triples, "elara"), triples
    assert not [t for t in triples if t[0] == "she"], triples
    # the month never queues as an identifier noun behind the date lane's claim
    growth: list = []
    L.derive_sentence_facts(L._parse(FAM1), _REF, growth_out=growth)
    assert ("march", "identifier_noun") not in [tuple(g) for g in growth], growth


def test_f8_seraphine_possessive_person_date_claim():
    """The sparse-litmus f1 shape: the possessive-person date claim lands born on
    SERAPHINE (not the user, not the month) + the spouse row, and 'april' never queues
    as an identifier noun (the date-claimant exemption holds for the possessive-person
    claims)."""
    triples = _triples(SERAPHINE1)
    assert _born_row(triples, "seraphine"), triples
    assert ("seraphine", "spouse", "user") in triples, triples
    assert not [t for t in triples if t[0] in ("she", "user")
                and t[1] in ("born", "born_on")], triples
    growth: list = []
    L.derive_sentence_facts(L._parse(SERAPHINE1), _REF, growth_out=growth)
    assert ("april", "identifier_noun") not in [tuple(g) for g in growth], growth


def test_f8_owner_landing_flag_off_parity(monkeypatch):
    """BLENDED_FAMILY_CAPTURE=false → the pre-person-first owner order (the subject
    reverts to the bare pronoun 'she'): the by-construction red-proof that the owner
    pin above guards the person-first arm itself."""
    monkeypatch.setattr(L, "BLENDED_FAMILY_CAPTURE", False)
    triples = _triples(FAM1)
    assert [t for t in triples if t[0] == "she"
            and t[1] in ("born", "born_on")], triples
    assert not _born_row(triples, "elara"), triples


def test_f8_expiry_owner_contract_stands_with_person_first():
    """f2: the possessive-plural passport expiry — expire_in claims the ISO value on
    'passport numbers' AND 'august' stays unqueued, co-existing with the person-first
    lanes (both lanes' contracts hold simultaneously — the wound's own litmus)."""
    growth: list = []
    doc = L._parse(TRAVEL1)
    triples = [(f.subject, f.rel_type, str(f.object))
               for f in L.derive_sentence_facts(doc, _REF, growth_out=growth) or []]
    assert any(s == "passport numbers" and r == "expire_in"
               and o.startswith("2027-08-01") for s, r, o in triples), triples
    assert ("august", "identifier_noun") not in [tuple(g) for g in growth], growth
