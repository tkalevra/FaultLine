"""THE SPARSE-STORE RESIDUALS (BUG-FAMILY-7 next round, 2026-11-12) — the four fixes,
each pinned by its MEASURED shape:

  R1  THE HYPERVISOR RUNS-ON FRAME — "The homelab hypervisor is Proxmox on the r730."
      files (homelab hypervisor, run, proxmox) beside the naming alias (the complement-
      PROPN first-cut), so "what does the homelab hypervisor run?" binds the rel by its
      own verb and the walk reaches the row (RUNS_ON_FRAME, linguistics
      _chain_runs_on_predication; the serve side: determine_path's RELATIONAL ROW-OWNER
      ADMISSION, RECALL_REL_ROW_OWNERS — the register value-affinity admission's
      relational twin). The frame's load-bearing guard: the on-PP must hang off the ATTR
      (the predicate nominal — "Proxmox ON the r730"), never off the verb — English hangs
      predicate location/time PPs off the verb ("the meeting is on Friday"), so those
      shapes never enter the frame.
  R2  SEC-BUG-2's FULL SHELF — the C5 controls: "what is my bank account number?" (the
      has_reference_id row; measured live: it mis-served the sibling phone row) and
      "what laptop do I work on?" (the work_laptop attr; the surface never appears
      contiguously) convert through the declared PERSONAL-ATTRIBUTE SHELF (migration
      318's growable personal_attr_shelf class + the overlay floor + the
      RECALL_PERSONAL_ATTR_SHELF walk admission, token-spread matched). The medication
      and contact classes ALREADY serve (measured on the same seat — no shelf row).
  R3  FILED-2 — the fresh-mint bitemporal half: the template's entity_attributes gains
      valid_from and migration 317 fans it to every schema (the 011-fan repair, the
      034-honoring stamp). The DB-backed equivalence pin lives in test_boot_migrations
      (test_fresh_mint_is_migration_equivalent); these pins hold the file shapes.
  R4  THE PET-NAME 6TH — the live-brain-shape variance class, ROOT-CAUSED to the
      ROUND-5 hostname guard's unbounded end-of-sentence scan: on the litmus sentence
      ("Our dog Biscuit is a corgi; we adopted him in May 2023.") the appositive name
      scanned ACROSS the semicolon into the adopted clause's year and returned "2023",
      so the gate declared the pet's own NAME a hostname label and both the unified
      chain and analyze_name_type_bindings declined the (Biscuit ↔ dog) binding — the
      full pet shape (instance_of / has_pet) dropped from the deterministic layer on
      exactly the litmus sentence. The window is now the label clause (bounded: 3
      tokens, stopping at clause punctuation — the colon of the shape itself stays
      inside); SPINE_HOSTNAME_LABEL_WINDOW=false restores the pre-fix byte shape.

Hermetic pins run everywhere; the store-side litmus class skips without a *_test
POSTGRES_DSN (same contract as tests/test_family_smoke_extraction_pins.py).
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
from src.api import linguistic_cue_overlay as LCO  # noqa: E402
from src.api.main import (  # noqa: E402
    _query_personal_attr_shelf_rels as _shelf_rels,
)

_REF = __import__("datetime").datetime(2026, 11, 12)

HYP = "The homelab hypervisor is Proxmox on the r730."
HYP_FULL = ("The homelab hypervisor is Proxmox on the r730, and the Home Assistant VM "
            "is vmid 112.")
PET = "Our dog Biscuit is a corgi; we adopted him in May 2023."


def _triples(text, ref=_REF):
    doc = L._parse(text)
    return [(f.subject, f.rel_type, str(f.object))
            for f in L.derive_sentence_facts(doc, ref) or []]


# ══════════════════════════════════════════════════════════════════════════════════
# R1 — the runs-on frame

def test_r1_litmus_sentence_mints_the_run_edge():
    triples = _triples(HYP)
    assert ("homelab hypervisor", "run", "proxmox") in triples, triples
    # the naming alias first-cut stays (additive, never a rewrite)
    assert ("homelab hypervisor", "also_known_as", "proxmox") in triples, triples


def test_r1_full_litmus_turn_still_mints_the_run_edge():
    triples = _triples(HYP_FULL)
    assert ("homelab hypervisor", "run", "proxmox") in triples, triples


def test_r1_mail_server_twin_mints():
    triples = _triples("Our mail server is Postfix on the old desktop.")
    assert ("mail server", "run", "postfix") in triples, triples


def test_r1_verb_pp_time_and_place_never_fire():
    """The load-bearing guard: predicate location/time PPs hang off the VERB and carry
    no attr complement — they never enter the frame."""
    for s in ("The meeting is on Friday.",
              "The lamp is on the desk.",
              "My birthday is on the 15th.",
              "The show is on Netflix."):
        assert not any(r == "run" for _s, r, _o in _triples(s)), (s, _triples(s))


def test_r1_negation_scan_back_contract_holds():
    assert not any(r == "run" for _s, r, _o in
                   _triples("The hypervisor is not Proxmox on the r730."))


def test_r1_det_introduced_complement_is_a_type_never_a_name():
    assert not any(r == "run" for _s, r, _o in
                   _triples("The case is on a shelf in the lab."))


def test_r1_flag_off_is_byte_parity(monkeypatch):
    monkeypatch.setattr(L, "RUNS_ON_FRAME", False)
    triples = _triples(HYP)
    assert not any(r == "run" for _s, r, _o in triples), triples
    assert ("homelab hypervisor", "also_known_as", "proxmox") in triples, triples


def test_r1_rel_row_owner_admission_source_pins():
    import inspect
    import src.api.main as M
    src = inspect.getsource(M)
    assert "RECALL_REL_ROW_OWNERS" in src
    assert "rel_row_owners_admitted" in src
    # the flag parses like its siblings (default ON, the 0/false/no lever)
    assert M._RECALL_REL_ROW_OWNERS is True


# ══════════════════════════════════════════════════════════════════════════════════
# R2 — SEC-BUG-2's full shelf (the declared personal-attribute shelf)

def test_r2_bank_account_ask_admits_the_reference_shelf():
    assert _shelf_rels("what is my bank account number?") == ("has_reference_id",)


def test_r2_work_laptop_spread_ask_admits():
    """THE C5 spread shape: "what laptop do I work on?" never contains "work laptop"
    contiguously — the token-spread arm carries both words."""
    assert _shelf_rels("what laptop do I work on?") == ("work_laptop",)
    assert _shelf_rels("what is my work laptop?") == ("work_laptop",)


def test_r2_unrelated_asks_admit_nothing():
    for q in ("what is my phone number?",
              "what is my email address?",
              "how old is my sister?",
              "what is my security clearance?"):
        assert _shelf_rels(q) == (), (q, _shelf_rels(q))


def test_r2_flag_off_is_byte_parity(monkeypatch):
    import src.api.main as M
    monkeypatch.setattr(M, "_RECALL_PERSONAL_ATTR_SHELF", False)
    # the flag gates the WALK BLOCK, not the pure matcher — assert the block's gate value
    # and re-pin the matcher as pure (its flag lives at the admission site only):
    assert M._RECALL_PERSONAL_ATTR_SHELF is False


def test_r2_overlay_floor_carries_the_measured_members():
    floor = LCO.resolve_personal_attr_shelves("")
    assert floor.get("bank account") == "has_reference_id"
    assert floor.get("work laptop") == "work_laptop"
    assert LCO.PERSONAL_ATTR_SHELF_CATEGORY == "personal_attr_shelf"


def test_r2_category_is_registered_in_the_bootstrap_map():
    """An unregistered category resolves EMPTY + logs critical (the _bootstrap_for
    contract) — the cold-shelf regression the pet/medical floors taught."""
    assert LCO.PERSONAL_ATTR_SHELF_CATEGORY in LCO._BOOTSTRAP_BY_CATEGORY


def test_r2_migration_318_seed_shape():
    here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(here, "..", "migrations",
                            "318_seed_personal_attr_shelves.sql"),
               encoding="utf-8").read()
    assert "personal_attr_shelf" in sql
    assert "('bank account',    'personal_attr_shelf', 'has_reference_id'" in sql
    assert "('work laptop',     'personal_attr_shelf', 'work_laptop'" in sql
    assert "ON CONFLICT (cue, category) DO NOTHING" in sql   # idempotent, tenant-safe
    assert "faultline\\_%" in sql                            # the per-tenant DO loop
    # the desc-carries-canonical shape (the unit_scalar contract)
    assert "description" in sql and "example_text" in sql


def test_r2_walk_admission_source_pins():
    import inspect
    import src.api.main as M
    src = inspect.getsource(M)
    assert "personal_attr_shelf_admitted" in src
    assert "RECALL_PERSONAL_ATTR_SHELF" in src
    assert M._RECALL_PERSONAL_ATTR_SHELF is True


def test_r2_scoped_absent_guard_exempt_for_the_shelf():
    """THE MEASURED DOOR WOUND: 'what is my bank account number?' scoped to the GLiNER
    'bank' concept (absent as an ENTITY, present as the user's own has_reference_id
    attribute) and the phase-5 scoped-absent-concept gate blanked the walk's fetched row
    behind the abstention template. The guard exempts a query whose shelf surfaces the
    walk admitted — deterministic, metadata-coupled (flag OFF → no admission → no
    exemption → byte-parity)."""
    import inspect
    import src.api.main as M
    src = inspect.getsource(M)
    assert "THE DECLARED-SHELF EXEMPTION" in src
    # the exemption sits INSIDE the guard's condition (an `and not` clause over the
    # shelf-rels ∩ scalar_rels intersection), not as a separate early return
    assert src.count("THE DECLARED-SHELF EXEMPTION") == 2  # both phase-5 gates carry it
    _start = src.index("THE DECLARED-SHELF EXEMPTION")  # the scoped-absent gate's copy
    # (slice widened 2800 → 4200: the wound-map round's hoisted health-shelf probe now
    # sits between the C5 probe and the scoped-absent guard — same gate, more lines)
    guard = src[_start:_start + 6200]
    # the unscoped path never runs the anchor walk, so the exemption PROBES the default
    # anchor's own attributes directly (metadata-coupled: the user holds the row)
    assert "_shelf_held = False" in guard
    assert "_query_personal_attr_shelf_rels(query_text)" in guard
    assert "SELECT 1 FROM entity_attributes WHERE entity_id = %s" in guard
    assert "and not _shelf_held" in guard
    assert "and _query_scoped_to_absent_concept(query_text, db, user_id," in guard
    # BOTH phase-5 anti-fabrication gates exempt the shelf-held shape (the #61
    # absent-possessed-aspect gate is the second one the bank ask hit on the door).
    # VEHICLE UPDATE (wound-map round 3, 2026-11-13, by design): the health-shelf HOLD
    # probe hoisted above BOTH gates — the #61 guard now reads across a line break with
    # the health conjunct (same semantics, one added conjunct); the scoped-absent guard
    # carries the health conjunct too (the allergy-after route: the meds-flag ask hit
    # the scoped-absent gate BEFORE the health exemption below it ever ran).
    assert ("if (_absent_np and anchor == user_id and not _shelf_held"
            in src and "and not _health_shelf_held):" in src)
    # the scoped-absent guard itself carries the health conjunct (the conjunct order
    # is guard-verbatim: ... and not _shelf_held / and not _health_shelf_held / and
    # _query_scoped_to_absent_concept(...))
    _g = src.index("and not _health_shelf_held")
    assert "_query_scoped_to_absent_concept" in src[_g:_g + 200]
    assert "and not _shelf_held" in src[_g - 200:_g]


# ══════════════════════════════════════════════════════════════════════════════════
# R3 — FILED-2 (the fresh-mint bitemporal half)

def test_r3_template_carries_valid_from():
    sql = open(os.path.join(REPO, "src", "provisioning", "templates",
                            "user_schema.sql"), encoding="utf-8").read()
    ea = sql.split("CREATE TABLE IF NOT EXISTS entity_attributes", 1)[1]
    ea = ea.split("PRIMARY KEY (entity_id, attribute)", 1)[0]
    assert "valid_from TIMESTAMP WITH TIME ZONE" in ea, "the template column is the FILED-2 fix"
    assert "valid_until TIMESTAMP WITH TIME ZONE" in ea
    assert "valid_from" in ea and "011" in ea  # the comment carries the 011-fan contract


def test_r3_migration_317_fan_shape():
    here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(here, "..", "migrations",
                            "317_entity_attributes_valid_from_fan.sql"),
               encoding="utf-8").read()
    assert "ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ" in sql   # additive + idempotent
    assert "SET valid_from = created_at WHERE valid_from IS NULL" in sql  # the 034-honoring stamp
    assert "relname = 'entity_attributes'" in sql                     # the 313/314 probe pattern
    assert "WHEN OTHERS THEN NULL" in sql                             # the guarded per-schema loop
    assert "UPDATE" in sql


# ══════════════════════════════════════════════════════════════════════════════════
# R4 — the pet-name 6th (the hostname guard's window)

def test_r4_litmus_sentence_keeps_the_pet_binding():
    """THE MEASURED WOUND: on the exact litmus sentence the ROUND-5 hostname guard
    scanned across the semicolon into the adopted clause's year ('2023') and declined
    the (Biscuit ↔ dog) binding — the full pet shape dropped. With the window the
    binding stands and the unified chain emits the pet shape."""
    doc = L._parse(PET)
    bindings = [(b.name, b.type_noun, b.possessor_is_self)
                for b in L.analyze_name_type_bindings(doc)]
    assert ("Biscuit", "dog", True) in bindings, bindings
    triples = _triples(PET)
    assert ("biscuit", "instance_of", "dog") in triples, triples
    assert ("user", "has_pet", "biscuit") in triples, triples
    assert ("user", "adopt", "biscuit") in triples, triples


def test_r4_real_hostname_construction_still_caught():
    s = "My laptop, hostname: chris-P70 has an ip of: 192.168.40.83/25 and exists on tailscale."
    doc = L._parse(s)
    lap = next(t for t in doc if t.text == "laptop")
    assert L._hostname_binding_appositive(lap) == "chris-p70"


def test_r4_window_flag_off_restores_the_unbounded_scan(monkeypatch):
    """OFF = the pre-fix byte shape (the cross-clause mis-read that ate the binding)."""
    monkeypatch.setattr(L, "SPINE_HOSTNAME_LABEL_WINDOW", False)
    doc = L._parse(PET)
    dog = next(t for t in doc if t.text == "dog")
    assert L._hostname_binding_appositive(dog) == "2023"


def test_r4_short_sentence_binding_unchanged():
    """The short litmus sentence always worked — the window must not REGRESS it."""
    doc = L._parse("Our dog Biscuit is a corgi.")
    bindings = [(b.name, b.type_noun, b.possessor_is_self)
                for b in L.analyze_name_type_bindings(doc)]
    assert ("Biscuit", "dog", True) in bindings, bindings


# ══════════════════════════════════════════════════════════════════════════════════
# THE STORE-SIDE LITMUS (real schema, DSN-gated) — the walk admissions end-to-end

_DSN = os.environ.get("POSTGRES_DSN", "")


@pytest.fixture(scope="module")
def walk_env():
    if not _DSN or not _DSN.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("store-side litmus needs a throwaway *_test POSTGRES_DSN (skipped)")
    import uuid as _uuid
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
    user_id = str(_uuid.uuid4())
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
    import src.api.main as M
    M._TENANT_READY_CACHE.add(user_id)
    yield {"user_id": user_id, "schema": schema, "M": M}
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    conn.close()


def _dp(env, query, anchor=None):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{env["schema"]}"')
        conn.commit()
        return env["M"].determine_path(
            query, conn, user_id=env["user_id"],
            anchor_is_concrete_entity=False, anchor_uuid=anchor,
            anchor_resolved_uuid=anchor,
        )
    finally:
        conn.close()


def _plant(env):
    """The measured C5 store: the run edge on a sparse compound-alias entity + the user's
    reference/work-laptop attributes."""
    import uuid as _uuid
    import psycopg2
    hyp_id = "hyp-" + _uuid.uuid4().hex[:12]
    prox_id = "prox-" + _uuid.uuid4().hex[:12]
    conn = psycopg2.connect(_DSN)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{env["schema"]}"')
        cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Concept'), (%s, 'Concept'), "
                    "(%s, 'Person') ON CONFLICT (id) DO NOTHING",
                    (hyp_id, prox_id, env["user_id"]))
        cur.execute("INSERT INTO rel_types (rel_type, label, tail_types) "
                    "VALUES ('run', 'Run', ARRAY['ANY']) ON CONFLICT (rel_type) DO NOTHING")
        cur.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES (%s, %s), (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (hyp_id, "homelab hypervisor", prox_id, "proxmox"))
        cur.execute("INSERT INTO facts (subject_id, object_id, rel_type, fact_provenance) "
                    "VALUES (%s, %s, 'run', 'user_stated')", (hyp_id, prox_id))
        cur.execute("INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text) "
                    "VALUES (%s, %s, 'has_reference_id', '123456789'),"
                    "(%s, %s, 'work_laptop', 'thinkpad') "
                    "ON CONFLICT (entity_id, attribute) DO NOTHING",
                    (env["user_id"], env["user_id"], env["user_id"], env["user_id"]))
    conn.commit()
    conn.close()
    return hyp_id


def _walk(env, query, anchor):
    """determine_path + THE WALK (fetch_facts_from_anchor — the seam where the declared
    shelf admissions layer the anchor's own rows into the path, the health shelf's home)."""
    import psycopg2
    path = _dp(env, query, anchor=anchor)
    facts = env["M"].fetch_facts_from_anchor(anchor, env["user_id"], path,
                                             query_text=query)
    return path, facts


def test_r1_row_owner_admission_reaches_the_run_row(walk_env):
    hyp_id = _plant(walk_env)
    path = _dp(walk_env, "what does the homelab hypervisor run?")
    assert "run" in path.relationship_rels, path.relationship_rels
    assert "run" in (path.aspect_bound_rels or []), path.aspect_bound_rels
    owners = list(getattr(path, "register_value_entity_ids", None) or [])
    assert hyp_id in owners, owners


def test_r1_row_owner_admission_flag_off_is_byte_parity(walk_env, monkeypatch):
    import src.api.main as M
    _plant(walk_env)
    monkeypatch.setattr(M, "_RECALL_REL_ROW_OWNERS", False)
    path = _dp(walk_env, "what does the homelab hypervisor run?")
    assert not (getattr(path, "register_value_entity_ids", None) or []), \
        "flag OFF: the row-owner pin never fires"


def test_r2_shelf_admission_lays_the_bank_reference(walk_env):
    _plant(walk_env)
    path, facts = _walk(walk_env, "what is my bank account number?",
                        walk_env["user_id"])
    assert "has_reference_id" in path.scalar_rels, path.scalar_rels
    # the walk fetched the row BY CONSTRUCTION: the value renders
    assert any("123456789" in str(f.get("value") or f.get("object") or "")
               for f in facts), [str(f)[:80] for f in facts[:8]]


def test_r2_shelf_admission_lays_work_laptop_on_the_spread_ask(walk_env):
    _plant(walk_env)
    path, facts = _walk(walk_env, "what laptop do I work on?",
                        walk_env["user_id"])
    assert "work_laptop" in path.scalar_rels, path.scalar_rels
    assert any("thinkpad" in str(f.get("value") or f.get("object") or "").lower()
               for f in facts), [str(f)[:80] for f in facts[:8]]


def test_r2_shelf_admission_flag_off_is_byte_parity(walk_env, monkeypatch):
    import src.api.main as M
    _plant(walk_env)
    monkeypatch.setattr(M, "_RECALL_PERSONAL_ATTR_SHELF", False)
    path = _dp(walk_env, "what is my bank account number?",
               anchor=walk_env["user_id"])
    assert "has_reference_id" not in path.scalar_rels, path.scalar_rels


def test_r2_no_row_admits_nothing(walk_env):
    """Metadata-coupled: an anchor that holds NO shelf attribute admits nothing — the
    abstention stands honestly (the class's own contract)."""
    import uuid as _uuid
    bare = str(_uuid.uuid4())
    path = _dp(walk_env, "what is my bank account number?", anchor=bare)
    assert "has_reference_id" not in path.scalar_rels, path.scalar_rels
