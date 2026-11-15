# -*- coding: utf-8 -*-
"""BUG-FAMILY-5 (smoke round 2) — the query-path hash-seed variance, PINNED.

The measured defect (DEV/BUGS/family-smoke-20261111/BUG-FAMILY-5-hash-seed-variance.md):
the SAME question on the SAME seat HIT or MISSED depending on the process's
PYTHONHASHSEED — vlan HIT→MISS across identical runs, keyword lists
(['allergic','medications','am']) reordered between processes and flipped topic-gate
outcomes. Root cause: iteration over SETS/DICTS of strings in the recall path
(determine_path's keyword→rel/taxonomy probe loops, the Component-2 inverse re-order,
the GLiNER2 label order, the MCP episodic/archive band rankings) walked in hash-seed
order, and every downstream consumer of list ORDER inherited the coin flip.

THE FIX under test: every order-dependent boundary sorts before use (same keywords,
same candidates, stable order), plus stable tiebreakers on the ranked loops whose
numeric keys tie.

THIS PIN: run the REAL recall path (`_query_impl`, the handler behind the /query door)
in TWO SUBPROCESSES — one started with PYTHONHASHSEED=0, one with PYTHONHASHSEED=random
(a fresh seed every run) — against the same freshly-provisioned throwaway seat planted
with the measured smoke rows (the iot-vlan row and the allergic_to row: the tool-noun
query class the smoke measured), and assert the served memories are BYTE-IDENTICAL
across the seeds, and that the answer actually HITS (a pin that passes on two identical
abstentions pins nothing).

Needs a reachable control-plane database (POSTGRES_DSN); skips honestly without one,
like every other DB-backed suite.
"""
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

pytestmark = pytest.mark.xdist_group("recall_determinism")

# ── the measured query class (the smoke's RECALLS + the brief's phrasings) ────────
QUESTIONS = [
    "what subnet does the iot vlan have?",                         # smoke vlan recall
    "what's the iot vlan?",                                        # the brief's phrasing
    "what medications am I allergic to?",                          # smoke allergy recall
    "what medications should be flagged before a prescription?",   # the brief's phrasing
]

VLAN_ENTITY = "iot-vlan-entity"
PEN_ENTITY = "penicillin-entity"


def _insert_flexible(cur, table, values, suffix=""):
    """INSERT only the columns this schema actually has (the open-core and the SaaS
    seat schemas differ — e.g. schema-scoped tables dropped ``user_id``). Values for
    columns the table lacks are dropped, so one plant works on both shapes."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s", (table,))
    cols = {r[0] for r in cur.fetchall()}
    use = {k: v for k, v in values.items() if k in cols}
    collist = ", ".join(f'"{k}"' for k in use)
    ph = ", ".join(["%s"] * len(use))
    cur.execute(f"INSERT INTO {table} ({collist}) VALUES ({ph}) {suffix}",
                tuple(use.values()))


# (table, values(seat) -> dict, suffix) — the rows the smoke's measured turns produced,
# filed DIRECTLY via SQL (deterministic — no LLM in the loop) so the only variance under
# test is the seed.
_PLANT_ROWS = [
    ("entities",
     lambda seat: {"id": VLAN_ENTITY, "user_id": seat, "entity_type": "Object"}, ""),
    ("entity_aliases",
     lambda seat: {"entity_id": VLAN_ENTITY, "user_id": seat, "alias": "iot vlan",
                   "is_preferred": True}, ""),
    ("entity_aliases",
     lambda seat: {"entity_id": VLAN_ENTITY, "user_id": seat, "alias": "vlan"}, ""),
    ("entity_aliases",
     lambda seat: {"entity_id": VLAN_ENTITY, "user_id": seat, "alias": "iot"}, ""),
    # net1: "The homelab VLAN for IoT devices is 10.0.20.0/24 and it has no internet egress."
    ("entity_attributes",
     lambda seat: {"user_id": seat, "entity_id": VLAN_ENTITY,
                   "attribute": "has_subnet", "value_text": "10.0.20.0/24",
                   "provenance": "user_stated"}, ""),
    # the question's own words binding the scope, the way the smoke's grown alias rows did
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_subnet", "alias": "subnet",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_subnet", "alias": "vlan",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    # a WIDE alias family onto seeded rel_types (migration 061) so one question carries a
    # ~9-word keyword SET — the exact shape the smoke measured reordering between processes
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_ip", "alias": "ip",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_mac", "alias": "mac",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_url", "alias": "url",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_email", "alias": "email",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_phone", "alias": "phone",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_uuid", "alias": "uuid",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "has_fqdn", "alias": "fqdn",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("entities",
     lambda seat: {"id": PEN_ENTITY, "user_id": seat, "entity_type": "Object"}, ""),
    ("entity_aliases",
     lambda seat: {"entity_id": PEN_ENTITY, "user_id": seat, "alias": "penicillin",
                   "is_preferred": True}, ""),
    ("rel_types",
     lambda seat: {"rel_type": "allergic_to", "label": "Allergic To",
                   "fact_class": "A", "tail_types": ["Object"], "source": "builtin",
                   "category": "medical", "confidence": 0.9, "is_symmetric": False,
                   "is_hierarchy_rel": False, "is_leaf_only": False},
     "ON CONFLICT (rel_type) DO NOTHING"),
    # the smoke's measured keyword set ['allergic','medications','am'] binding the scope —
    # TWO aliases onto one rel, so the sorted keyword loop's dedup order is exercised
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "allergic_to", "alias": "allergic",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    ("rel_type_aliases",
     lambda seat: {"canonical_rel_type": "allergic_to", "alias": "medications",
                   "source": "ontology"}, "ON CONFLICT (alias) DO NOTHING"),
    # med1: "I'm allergic to penicillin — flag it before any prescription."
    ("facts",
     lambda seat: {"user_id": seat, "subject_id": seat, "object_id": PEN_ENTITY,
                   "rel_type": "allergic_to", "provenance": "user_stated",
                   "fact_provenance": "user_stated", "fact_class": "A",
                   "confidence": 1.0}, ""),
]

_CHILD_TEMPLATE = r"""
import json, os, sys
os.environ.setdefault("SAAS_MODE", "1")
os.environ.setdefault("SAAS_SESSION_SECRET", "faultline-test-signing-secret-0123456789")
os.environ.setdefault("MCP_API_KEY", "unit-test-key")
os.environ.setdefault("SAAS_COOKIE_SECURE", "false")
os.environ.setdefault("SAAS_SIGNUP_LEDGER_SALT", "faultline-test-ledger-salt-xyz")
os.environ["SPACY_MODEL"] = "en_core_web_sm"
repo, seat, questions_json, out_path = sys.argv[1:5]
sys.path.insert(0, repo)
from src.api.models import QueryRequest
from src.api.main import _query_impl

out = {}
for q in json.loads(questions_json):
    resp = _query_impl(QueryRequest(text=q, user_id=seat))
    out[q] = json.loads(resp.model_dump_json())
with open(out_path, "w") as fh:
    json.dump(out, fh, sort_keys=True, default=str)
print("CHILD-OK")
"""

_CHILD_PATH_TEMPLATE = r"""
import json, os, sys
os.environ.setdefault("SAAS_MODE", "1")
os.environ.setdefault("SAAS_SESSION_SECRET", "faultline-test-signing-secret-0123456789")
os.environ.setdefault("MCP_API_KEY", "unit-test-key")
os.environ.setdefault("SAAS_COOKIE_SECURE", "false")
os.environ.setdefault("SAAS_SIGNUP_LEDGER_SALT", "faultline-test-ledger-salt-xyz")
os.environ["SPACY_MODEL"] = "en_core_web_sm"
import psycopg2
repo, seat, schema, questions_json, out_path = sys.argv[1:6]
sys.path.insert(0, repo)
from src.api.main import determine_path

conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
cur = conn.cursor()
cur.execute('SET search_path TO "%s"' % schema)
out = {}
for q in json.loads(questions_json):
    p = determine_path(q, conn, user_id=seat)
    out[q] = {
        "scalar_rels": list(p.scalar_rels),
        "relationship_rels": list(p.relationship_rels),
        "taxonomy_groups": list(p.taxonomy_groups),
        "aspect_bound_rels": list(getattr(p, "aspect_bound_rels", None) or []),
    }
with open(out_path, "w") as fh:
    json.dump(out, fh, sort_keys=True)
print("CHILD-OK")
"""

# The scope-stress question: ~9 distinct query words each binding a seeded rel_type via
# its alias — pre-fix, the keyword-probe loops walked this SET in PYTHONHASHSEED order,
# so the assembled scope lists (scalar_rels / relationship_rels via Component 2's
# ``list(expanded)``) came back in a DIFFERENT order every process.
STRESS_QUERY = ("what are the ip mac url email phone uuid fqdn subnet vlan "
                "of the iot vlan?")


@pytest.fixture(scope="module")
def det_seat(postgres_dsn):
    """A throwaway seat provisioned + planted once for the module, torn down after.

    Mirrors the ctx-bench driver's provisioning (account → seat → ensure_user_provisioned
    → wait_until_ready), then files the measured smoke rows directly via SQL so the
    two-seed comparison exercises ONLY the query path's ordering, never ingest variance."""
    if postgres_dsn is None:
        pytest.skip("no reachable control-plane database (POSTGRES_DSN unset or unreachable)")
    import psycopg2

    seat = str(uuid.uuid4())
    account_id = None
    slug = None
    schema_name = "faultline_" + seat.replace("-", "_")
    try:
        from saas import accounts as _accounts, seats as _seats
        from src.provisioning.provisioning_status import (
            check_provisioning_status, ensure_user_provisioned, wait_until_ready)
        from src.provisioning.schema_manager import derive_user_slug_from_uuid

        name = f"recall-det-{int(time.time())}-{seat[:8]}"
        created = _accounts.create_account(name=name, slug=name, account_kind="team")
        account_id, slug = created["account_id"], created["slug"]
        _seats.register_seat(account_id, seat, seat_type="standard")
        ensure_user_provisioned(seat, derive_user_slug_from_uuid(seat))
        schema = wait_until_ready(lambda: check_provisioning_status(seat),
                                  seat_uuid=seat, timeout=240.0, poll_s=2.0)
        assert schema, "seat provisioning did not become ready"
    except Exception as exc:
        if account_id:
            try:
                from saas import accounts as _accounts
                _accounts.delete_account(account_id, confirm_slug=slug, is_operator=True,
                                         purge_memory=True)
            except Exception:
                pass
        pytest.skip(f"seat provisioning unavailable on this database: "
                    f"{type(exc).__name__}: {str(exc)[:120]}")

    conn = psycopg2.connect(postgres_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema_name}"')
            # the walker's display line for the speaker (NOT preferred: provisioning
            # already minted the seat entity's one allowed preferred alias)
            _insert_flexible(cur, "entity_aliases",
                             {"entity_id": seat, "user_id": seat, "alias": "Det Tester",
                              "is_preferred": False})
            for table, values, suffix in _PLANT_ROWS:
                _insert_flexible(cur, table, values(seat), suffix)
        conn.commit()
    except Exception as exc:
        conn.close()
        pytest.skip(f"plant unavailable on this database (schema drift): "
                    f"{type(exc).__name__}: {str(exc)[:160]}")
    conn.close()

    yield {"dsn": postgres_dsn, "seat": seat, "schema": schema_name}

    # ── teardown: purge the seat exactly like the bench driver does ──────────────
    # archive FIRST (delete_account requires it), then delete + purge, then drop the
    # seat schemas as belt-and-suspenders (a half-failed purge must not leave state
    # on the shared floor).
    try:
        from saas import accounts as _accounts
        _accounts.set_account_status(account_id, "archived", is_operator=True)
        _accounts.delete_account(account_id, confirm_slug=slug, is_operator=True,
                                 purge_memory=True)
    except Exception:
        pass
    try:
        conn = psycopg2.connect(postgres_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                for prefix in ("faultline_", "flagent_"):
                    sch = prefix + seat.replace("-", "_")
                    cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def test_recall_identical_under_pythonhashseed_0_and_random(det_seat, tmp_path):
    """The same recall on the same seat under PYTHONHASHSEED=0 and =random must serve
    BYTE-IDENTICAL memories — and must actually hit the planted rows."""
    outs = {}
    for seed in ("0", "random"):
        out_path = str(tmp_path / f"recall_seed_{seed.replace('=', '_')}.json")
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["POSTGRES_DSN"] = det_seat["dsn"]
        r = subprocess.run(
            [sys.executable, "-c", _CHILD_TEMPLATE,
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
             det_seat["seat"], json.dumps(QUESTIONS), out_path],
            capture_output=True, text=True, timeout=900, env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert r.returncode == 0, (
            f"child (PYTHONHASHSEED={seed}) failed rc={r.returncode}\n"
            f"stdout tail: {r.stdout[-800:]}\nstderr tail: {r.stderr[-2000:]}")
        with open(out_path) as fh:
            outs[seed] = json.load(fh)

    # ── THE PIN: identical served memories across the seeds ─────────────────────
    assert outs["0"] == outs["random"], (
        "recall output differs between PYTHONHASHSEED=0 and PYTHONHASHSEED=random "
        "(BUG-FAMILY-5 regression: an order-dependent boundary is still unsorted)")

    # ── the answers must be real hits, not two identical abstentions ────────────
    def _served_text(payload):
        chunks = []
        for _q, resp in payload.items():
            for f in (resp.get("facts") or []):
                chunks.append(str(f.get("rel_type") or ""))
                chunks.append(str(f.get("object") or ""))
                chunks.append(str(f.get("definition") or ""))
        return " ".join(chunks).lower()

    served = _served_text(outs["random"])
    assert "10.0.20.0/24" in served, "the planted iot-vlan subnet did not surface"
    assert "penicillin" in served, "the planted allergic_to row did not surface"


def test_determine_path_scope_lists_identical_under_pythonhashseeds(det_seat, tmp_path):
    """THE MECHANISM, pinned directly: determine_path's assembled scope lists must come
    out in the SAME ORDER under PYTHONHASHSEED=0 and =random.

    This is the exact boundary the smoke measured reordering: the keyword-probe loops
    iterate the query's keyword SET, and Component 2 re-orders relationship_rels from a
    set (``list(expanded)`` pre-fix) — hash-seed order. The stress question carries ~9
    resolvable keywords so the assembled lists are long enough for two seeds to disagree
    pre-fix; post-fix every boundary is sorted and the lists are identical."""
    outs = {}
    for seed in ("0", "random"):
        out_path = str(tmp_path / f"path_seed_{seed}.json")
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["POSTGRES_DSN"] = det_seat["dsn"]
        r = subprocess.run(
            [sys.executable, "-c", _CHILD_PATH_TEMPLATE,
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
             det_seat["seat"], det_seat["schema"],
             json.dumps([STRESS_QUERY, QUESTIONS[0]]), out_path],
            capture_output=True, text=True, timeout=600, env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert r.returncode == 0, (
            f"path child (PYTHONHASHSEED={seed}) failed rc={r.returncode}\n"
            f"stdout tail: {r.stdout[-800:]}\nstderr tail: {r.stderr[-2000:]}")
        with open(out_path) as fh:
            outs[seed] = json.load(fh)

    assert outs["0"] == outs["random"], (
        "determine_path scope lists differ between PYTHONHASHSEED=0 and =random "
        "(BUG-FAMILY-5 regression: a scope-assembly boundary is still unsorted)\n"
        f"seed 0:     {json.dumps(outs['0'], indent=1)[:1200]}\n"
        f"seed random:{json.dumps(outs['random'], indent=1)[:1200]}")

    # and the stress scope must actually have BOUND (long enough for seeds to disagree)
    stress = outs["random"][STRESS_QUERY]
    assert len(stress["scalar_rels"]) >= 6, (
        f"stress query bound too few rels ({stress['scalar_rels']}) — the alias family "
        "did not resolve; the pin would be vacuous")
