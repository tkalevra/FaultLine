"""A redaction must also UNWELD THE NAME — executed against a REAL PostgreSQL.

WHY A REAL DATABASE. Every defect this suite pins was statement-level: the wrong operand
compared against `entity_id`, a case-sensitive match against a lowercase key, and an
`is_preferred = true` predicate that excluded the one class of alias that most needed
removing. A mocked cursor sees none of those — it returns whatever it was told to. The bug
reached production precisely because the only thing that could catch it was the SQL actually
running.

THE PRODUCTION SYMPTOM THIS REPRODUCES. On a live seat the surface `cato` stayed welded to a
laptop entity after the owner redacted it: the fact went, the name did not, so the thing they
asked to forget kept rendering and kept resolving. The weld had landed NON-preferred, which is
what made it structurally undeletable by the old statement.

Column types mirror production (`entity_id` and `alias` are TEXT, not uuid).
Skips cleanly when no throwaway database is offered via UNWELD_TEST_DSN.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.api.main import unweld_alias  # noqa: E402

DSN = os.environ.get("UNWELD_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set UNWELD_TEST_DSN to a THROWAWAY postgres to run the DB-backed unweld tests",
)

_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE entity_aliases (
    id serial PRIMARY KEY,
    entity_id text,
    alias text,
    is_preferred boolean DEFAULT false,
    preference_source text DEFAULT 'unspecified',
    created_at timestamptz DEFAULT now(),
    display_form text,
    UNIQUE (entity_id, alias));
"""

LAPTOP = "10253e31-e41d-58ac-8a21-c4aec36e75b0"
OTHER = "005500da-62bf-5ab4-a155-71117754e4c9"


@pytest.fixture()
def cur():
    sch = "uw_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(_DDL.format(sch=sch))
        yield c
    conn.rollback()
    conn.close()


def _add(cur, entity_id, alias, preferred=False, source="unspecified", ago="0 seconds"):
    cur.execute(
        "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source, created_at)"
        " VALUES (%s, %s, %s, %s, now() - %s::interval)",
        (entity_id, alias, preferred, source, ago),
    )


def _aliases(cur, entity_id):
    cur.execute(
        "SELECT alias, is_preferred FROM entity_aliases WHERE entity_id = %s ORDER BY alias",
        (entity_id,),
    )
    return {a: p for a, p in cur.fetchall()}


# ── the production case ───────────────────────────────────────────────────────────────────
def test_removes_a_non_preferred_weld(cur):
    """THE BUG. `cato` welded onto the laptop entity, non-preferred, must come off."""
    _add(cur, LAPTOP, "laptop", preferred=False, source="inferred")
    _add(cur, LAPTOP, "cato", preferred=False, source="rel_default")

    removed, _ = unweld_alias(cur, LAPTOP, "cato")

    assert removed == 1
    assert "cato" not in _aliases(cur, LAPTOP)


def test_matches_the_lowercase_key_when_caller_sends_display_casing(cur):
    """Stored aliases are the lowercase matching key; callers send the user's casing."""
    _add(cur, LAPTOP, "cato", source="rel_default")

    removed, _ = unweld_alias(cur, LAPTOP, "Cato")

    assert removed == 1
    assert _aliases(cur, LAPTOP) == {}


def test_surrounding_whitespace_does_not_defeat_the_match(cur):
    _add(cur, LAPTOP, "cato", source="rel_default")
    removed, _ = unweld_alias(cur, LAPTOP, "  Cato  ")
    assert removed == 1


def test_a_surface_string_is_not_an_entity_id(cur):
    """Passing the raw surface the caller sent must not silently match some other row."""
    _add(cur, LAPTOP, "cato", source="rel_default")

    removed, _ = unweld_alias(cur, "laptop", "cato")

    assert removed == 0
    assert "cato" in _aliases(cur, LAPTOP)


# ── keeping the entity renderable ─────────────────────────────────────────────────────────
def test_promotes_a_replacement_when_the_preferred_alias_is_removed(cur):
    _add(cur, OTHER, "p70 laptop", preferred=True, source="user_stated")
    _add(cur, OTHER, "thinkpad", preferred=False, source="inferred")

    removed, promoted = unweld_alias(cur, OTHER, "p70 laptop")

    assert (removed, promoted) == (1, 1)
    assert _aliases(cur, OTHER) == {"thinkpad": True}


def test_promotes_by_trust_ladder_not_insertion_order(cur):
    """The replacement is the most TRUSTED survivor, not merely the oldest."""
    _add(cur, OTHER, "p70 laptop", preferred=True, source="user_stated")
    _add(cur, OTHER, "guess", preferred=False, source="provisioned", ago="10 seconds")
    _add(cur, OTHER, "stated name", preferred=False, source="user_stated", ago="1 second")

    unweld_alias(cur, OTHER, "p70 laptop")

    assert _aliases(cur, OTHER) == {"guess": False, "stated name": True}


def test_ties_on_trust_break_by_age(cur):
    _add(cur, OTHER, "gone", preferred=True, source="user_stated")
    _add(cur, OTHER, "newer", preferred=False, source="inferred", ago="1 second")
    _add(cur, OTHER, "older", preferred=False, source="inferred", ago="30 seconds")

    unweld_alias(cur, OTHER, "gone")

    after = _aliases(cur, OTHER)
    assert after["older"] is True
    assert after["newer"] is False


def test_does_not_promote_while_a_preferred_alias_still_stands(cur):
    """Removing a NON-preferred alias must not disturb the display name."""
    _add(cur, OTHER, "p70 laptop", preferred=True, source="user_stated")
    _add(cur, OTHER, "junk", preferred=False, source="rel_default")

    removed, promoted = unweld_alias(cur, OTHER, "junk")

    assert (removed, promoted) == (1, 0)
    assert _aliases(cur, OTHER) == {"p70 laptop": True}


def test_removing_the_last_alias_leaves_the_entity_nameless(cur):
    """Nothing to promote is correct — the user asked for the last name to be forgotten."""
    _add(cur, OTHER, "only", preferred=True, source="user_stated")

    removed, promoted = unweld_alias(cur, OTHER, "only")

    assert (removed, promoted) == (1, 0)
    assert _aliases(cur, OTHER) == {}


# ── scope and contract ────────────────────────────────────────────────────────────────────
def test_other_entities_are_untouched(cur):
    _add(cur, LAPTOP, "cato", source="rel_default")
    _add(cur, OTHER, "cato", preferred=True, source="user_stated")

    unweld_alias(cur, LAPTOP, "cato")

    assert _aliases(cur, OTHER) == {"cato": True}


def test_a_missing_alias_is_a_no_op_not_an_error(cur):
    _add(cur, LAPTOP, "laptop", preferred=True, source="inferred")

    assert unweld_alias(cur, LAPTOP, "never-stored") == (0, 0)
    assert _aliases(cur, LAPTOP) == {"laptop": True}


@pytest.mark.parametrize("entity_id,alias", [(None, "cato"), (LAPTOP, None), (LAPTOP, "   ")])
def test_empty_operands_do_nothing(cur, entity_id, alias):
    _add(cur, LAPTOP, "cato", source="rel_default")
    assert unweld_alias(cur, entity_id, alias) == (0, 0)
    assert "cato" in _aliases(cur, LAPTOP)


def test_the_caller_owns_the_transaction(cur):
    """The helper must not commit — the handler commits, and a failure must be rollback-able."""
    _add(cur, LAPTOP, "cato", source="rel_default")
    cur.execute("SAVEPOINT sp_unweld")

    unweld_alias(cur, LAPTOP, "cato")
    assert "cato" not in _aliases(cur, LAPTOP)

    cur.execute("ROLLBACK TO SAVEPOINT sp_unweld")
    assert "cato" in _aliases(cur, LAPTOP)
