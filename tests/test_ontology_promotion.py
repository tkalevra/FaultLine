"""PROMOTE — the engine's grown ontology must be able to reach the user.

Three blockers stood between the growth engine and the reply, and this suite pins each one at
the level it can be pinned deterministically:

  1. THE RELEVANCE GATE. `confirmed_count` is the promotion gate and its intent is that a thing
     recurring earns its way in. It had two intended writers and neither fired: the ingest
     producers returned at their cycle-guard before the counter could move (a staged rung makes
     its own parent reachable, so the guard reads the rung's OWN existence as a cycle), and the
     recall ladder-climb fed nothing back at all.
  2. THE CLASS-C CLOCK. The tenant template declared staged_facts.expires_at with no default,
     dropping migration 012's 30-day default, so rows were born outside the lifecycle in both
     directions.
  3. THE PROVENANCE FILTER. Engine-grown rows were stripped from the returned set by lane name
     AND by the bare `subclass_of` relation — the latter also deleting the USER'S OWN
     "a fraggle is a kind of puppet" from every reply.

These are unit-level guards. The behavioural proof is on the deployed pre-production system
through the real client path; this suite exists so a later refactor cannot silently undo any of
the three.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "src" / "provisioning" / "templates" / "user_schema.sql"
MIGRATION = REPO / "migrations" / "264_restore_staged_facts_expiry_default.sql"
EMBEDDER = REPO / "src" / "re_embedder" / "embedder.py"
MAIN = REPO / "src" / "api" / "main.py"


def _code_only(py_src: str) -> str:
    """Strip comments and string literals so an assertion tests the CODE, not the prose about it.

    These files carry long explanatory comments that name the very things being asserted absent
    ("it never INSERTs", "never a DELETE"), so a naive substring check reads its own rationale
    back as a violation.
    """
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(py_src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except Exception:
        return py_src
    return " ".join(out)


def _sql_code_only(sql_src: str) -> str:
    """Strip `--` comment lines from SQL for the same reason."""
    return "\n".join(ln for ln in sql_src.splitlines() if not ln.lstrip().startswith("--"))


# ─────────────────────────── BLOCKER 2 — the Class-C clock ───────────────────────────

def test_template_declares_the_thirty_day_expiry_default():
    """The tenant template must carry migration 012's default.

    Without it every tenant built from this template is born clockless: expire_staged_facts and
    decay_class_c_hits key on `expires_at <= now()`, which NULL never satisfies, and
    fetch_unsynced_staged filters `expires_at > now()`, which NULL never satisfies either. The
    row can neither expire nor be seen. Measured: 39 of 39 pre-prod tenant schemas had no
    default, and the owner's seat carried 313 of 642 staged rows with a NULL expiry.
    """
    sql = TEMPLATE.read_text()
    m = re.search(r"^\s*expires_at\s+TIMESTAMPTZ([^,\n]*)", sql, re.M)
    assert m, "staged_facts.expires_at is not declared in the tenant template at all"
    decl = m.group(1)
    assert "DEFAULT" in decl.upper(), (
        "staged_facts.expires_at has NO DEFAULT in the tenant template — migration 012's "
        f"`DEFAULT now() + interval '30 days'` has been dropped again. Declaration was: {decl!r}"
    )
    assert "30 days" in decl, (
        f"the expiry default is not the 30-day Class-C window: {decl!r}"
    )


def test_a_migration_repairs_existing_tenants_not_only_new_ones():
    """The template is applied ONLY at schema creation, so it cannot reach existing tenants.

    A template-only fix silently skips every tenant that already exists; a migration-only fix
    silently skips every tenant provisioned afterwards. Both halves are required, and this repo
    has a documented history of shipping only one of them.
    """
    assert MIGRATION.exists(), "no migration repairs staged_facts.expires_at on existing tenants"
    sql = MIGRATION.read_text()
    assert "information_schema.schemata" in sql, "the migration does not fan out over tenants"
    assert "SET DEFAULT" in sql, "the migration does not set the column default"
    assert "expires_at IS NULL" in sql, "the migration does not backfill clockless rows"
    # The backfill must NOT be backdated: a row that never had a clock has not had its 30 days,
    # and backdating would make the repair itself reap history.
    assert "first_seen_at" not in _sql_code_only(sql), (
        "the backfill appears to backdate the window from first_seen_at — that would expire "
        "rows the moment the clock is repaired, which is the opposite of the intent"
    )


# ───────────────────── BLOCKER 1 — the relevance gate, ingest half ─────────────────────

def test_reconfirm_helper_exists_and_never_inserts_or_changes_tier():
    """The re-encounter writer must bump the counter and NOTHING else.

    Preventing a duplicate ROW and refusing to record a re-encounter are different things, and
    the cycle-guards conflated them. The fix separates them — but it must not become a second
    way to create rows or to move a fact between classes: A and B never change class, C→B is the
    only tier change, and it belongs to the promoter.
    """
    src = EMBEDDER.read_text()
    assert "def _reconfirm_existing_rung(" in src, "the re-encounter writer is gone"
    body = src.split("def _reconfirm_existing_rung(", 1)[1].split("\ndef ", 1)[0]
    assert "confirmed_count = confirmed_count + 1" in body, "it does not increment the counter"
    code = _code_only("def _f(" + body)
    assert "INSERT" not in code.upper(), "the re-encounter writer must never insert a row"
    assert "fact_class" not in code, "the re-encounter writer must never touch the tier"
    assert "promoted_at IS NULL" in body, "it would bump an already-promoted row"
    assert "deleted_at IS NULL" in body, "it would bump a tombstoned (forgotten) row"


@pytest.mark.parametrize("producer", [
    "_place_climb_rung",
    "_place_coinstance_rung",
])
def test_producers_check_for_a_re_encounter_before_the_cycle_guard(producer):
    """Order matters and is the whole bug.

    `_is_ancestor_or_descendant` walks facts ∪ staged_facts, so the cheapest way for it to
    return True is that the edge being placed IS the edge it finds. Running it first means a
    rung can never re-confirm itself — measured as 432 grown rungs on a live seat with none
    eligible and the highest count anywhere 2.
    """
    src = EMBEDDER.read_text()
    body = src.split(f"def {producer}(", 1)[1].split("\ndef ", 1)[0]
    assert "_reconfirm_existing_rung" in body, f"{producer} records no re-encounter"
    assert body.index("_reconfirm_existing_rung") < body.index("_is_ancestor_or_descendant"), (
        f"{producer} runs its cycle-guard BEFORE the re-encounter check, so the counter can "
        "never move — this is the original defect"
    )


def test_full_chain_commits_a_reconfirm_only_pass():
    """A chain whose rungs all already exist must not discard its re-confirmations.

    `_place_full_chain` owns its transaction so a chain is atomic. The early-out for
    `placed == 0` used to roll back unconditionally, which would throw away every bump made on
    a re-derivation of an existing ladder — the single most common outcome once the counter
    works at all.
    """
    src = EMBEDDER.read_text()
    body = src.split("def _place_full_chain(", 1)[1].split("\ndef ", 1)[0]
    assert 'out["placed"] == 0' in body
    tail = body.split('out["placed"] == 0', 1)[1][:900]
    assert 'reconfirmed' in tail and "commit()" in tail, (
        "a pass that only re-confirmed is still rolled back — the bumps are lost"
    )


# ───────────────────── BLOCKER 1 — the relevance gate, recall half ─────────────────────

def test_the_ladder_climb_feeds_the_counter_back():
    """Surfacing a rung by recall is the second intended increment path.

    The direct-anchor staged lane has always bumped `confirmed_count`; the classification ladder
    climb — the lane that reaches every rung ABOVE the anchor's own first hop, i.e. the entire
    grown ladder — fed nothing back at all. A grown rung was therefore unreachable by BOTH
    writers at once.
    """
    src = MAIN.read_text()
    lane = src.split("CLASSIFICATION LADDER CLIMB", 1)[1].split("Fetch SCALAR ATTRIBUTES", 1)[0]
    assert "confirmed_count = confirmed_count + 1" in lane, (
        "the ladder-climb lane still records nothing when it surfaces a staged rung"
    )
    assert "_INGEST_ENABLED" in lane, (
        "the bump is not gated on INGEST_ENABLED — knowledge-store mode must stay read-only"
    )
    assert "deleted_at IS NULL" in lane, "a forgotten row's counter/expiry would be reset"


def test_one_query_is_one_confirmation_across_every_lane():
    """Two lanes surface staged rows and both are writers — they must share one ledger.

    The anchor's own first hop is reached by the direct-anchor staged fetch AND by the
    classification ladder climb. Measured live before this guard: a single query took rung 1273
    from 1 to 3. A gate that counts how many LANES touched a row is not measuring recurrence,
    which is the whole property `confirmed_count` exists to carry.
    """
    src = MAIN.read_text()
    fn = src.split("def fetch_facts_from_anchor(", 1)[1].split("\ndef ", 1)[0]
    assert "_confirmed_staged_ids: set = set()" in fn, (
        "there is no per-query confirmation ledger"
    )
    # BOTH writers must subtract from the ledger and record into it.
    for writer in ("_direct_confirm", "_climb_confirm"):
        assert f"{writer} = sorted(set(" in fn and "- _confirmed_staged_ids)" in fn, (
            f"{writer} does not subtract what this query already confirmed"
        )
        assert f"_confirmed_staged_ids.update({writer})" in fn, (
            f"{writer} does not record what it took, so a later lane can double-count it"
        )
    # And the ledger is only meaningful if the function runs once per question.
    assert src.count("= fetch_facts_from_anchor(") == 1, (
        "fetch_facts_from_anchor is invoked from more than one place — a per-call ledger no "
        "longer guarantees one confirmation per query"
    )


# ───────────────── BLOCKER 3 — the provenance filter on the returned set ─────────────────

def test_a_user_stated_hierarchy_row_is_never_treated_as_grown_ontology():
    """The bare `subclass_of` clause deleted the user's own words.

    An engine-grown rung is already caught by the provenance clause, so the ONLY rows the
    relation clause could ever add are ones no engine lane minted — i.e. the user's own
    "a fraggle is a kind of puppet". That is their memory, and it was stripped from every reply.
    """
    import importlib.util
    spec = importlib.util.find_spec("src.api.main")
    assert spec is not None
    # Import-free check: the predicate is pure text, and importing main.py pulls the world in.
    src = MAIN.read_text()
    body = src.split("def _is_grown_ontology_row(", 1)[1].split("\ndef ", 1)[0]
    assert "fact_provenance" in body, "the predicate cannot see who asserted the row"
    assert "user_stated" in body, "a user statement is still classified as grown ontology"
    assert body.index("user_stated") < body.index('== "subclass_of"'), (
        "the user_stated exemption must be checked BEFORE the bare-relation clause, or the "
        "relation clause still swallows the user's own hierarchy statements"
    )


def test_the_grown_ladder_can_reach_the_reply_on_a_classification_question():
    """The engine's own ontology must be speakable when it IS the answer.

    The filter was unconditional: an engine-grown rung could not be returned by ANY question,
    so the entire growth engine was walked and never spoken. It is now conditional on what was
    ASKED — the classification axis, which is set only when the query is a "what is / what kind"
    question — rather than on who minted the row.
    """
    src = MAIN.read_text()
    lane = src.split("CLASSIFICATION LADDER CLIMB", 1)[1].split("Fetch SCALAR ATTRIBUTES", 1)[0]
    assert "_ladder_is_answer" in lane, "the return decision is still unconditional"
    assert "_ladder_is_answer = bool(_classif_axis_rels)" in lane, (
        "the gate must key on the query's own classification axis, not on a provenance list "
        "or any hardcoded vocabulary"
    )
    assert "_ladder_is_answer or not _is_grown_ontology_row(" in lane, (
        "grown rungs are still dropped on a classification question"
    )


# ───────────────── The C tier keeps moving: promoted, reaped, extended ─────────────────

def test_struck_class_c_is_evaluated_against_the_durable_tier_without_a_threshold():
    """A struck C row has three exits and none of them is 'sit there'.

    It graduates by promotion, it is reaped once A/B covers it, or it earns extended life to try
    again. The evaluation must be an IDENTITY question the store can already answer plus the
    store's OWN declared authority ordering — never a similarity floor or a confidence cutoff
    chosen here, which would be exactly the brittleness this work exists to remove.
    """
    src = EMBEDDER.read_text()
    assert "def evaluate_struck_class_c(" in src
    body = src.split("def evaluate_struck_class_c(", 1)[1].split("\ndef ", 1)[0]
    assert "fact_class = 'C'" in body, "it must only ever consider Class C rows"
    assert "deleted_at = now()" in body, "reaping must be a recoverable tombstone, never a DELETE"
    code = _code_only("def _f(" + body)
    assert "DELETE" not in code.upper(), "reaping must never hard-delete a row"
    assert "_FACT_PROVENANCE_RANK" in body, "no authority ordering is applied at all"
    # No invented numeric thresholds: the only numeric literal permitted is the tier's own
    # 30-day window, which is the clock the tier is already defined in.
    numbers = set(re.findall(r"(?<![\w.'])0?\.\d+(?![\w'])", _code_only("def _f(" + body)))
    assert not numbers, (
        f"a numeric threshold was introduced into the C evaluation: {sorted(numbers)} — "
        "weighting must be derived from what the graph has observed, never typed in"
    )


def test_expiry_is_withheld_while_the_tenant_has_no_brain():
    """Processability gates the clock.

    Expiry is definitional for short-term memory, but a row is only fairly reaped if it HAD its
    chance to be typed. A seat whose credential will not decrypt fails every extraction, so none
    of its rows ever got that chance — the outage is ours and the clock must not charge them
    for it.
    """
    src = EMBEDDER.read_text()
    assert "def _tenant_brain_unavailable(" in src
    body = src.split("def expire_staged_facts(", 1)[1].split("\ndef ", 1)[0]
    assert "_tenant_brain_unavailable()" in body, (
        "expiry still reaps rows for a seat that never had a working brain"
    )
    # The gate must sit before the REMOVAL, not before the (non-destructive) decay.
    assert body.index("confirmed_count = confirmed_count - 1") < body.index(
        "_tenant_brain_unavailable()"), (
        "the brain gate is withholding the non-destructive decay as well; it should only "
        "withhold the removal"
    )
