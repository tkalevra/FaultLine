"""
THE HARD LINE — a NAMED INSTANCE never receives a subclass_of from the L4 climb.

Regression guard for the category error: ingesting "My dog Fraggle is a poodle." stores
`fraggle instance_of poodle`, but the async classification climb / what-is grounder must NOT
mint `fraggle subclass_of animal` — that climbs the NAME up the type ladder. A name never
becomes a place. The structural test (`_is_named_instance`) is edge/naming-layer based (no
proper-noun word list): an entity that is the SUBJECT of an instance_of edge, OR whose every
alias is a naming-layer object, is a name and is excluded from the subclass_of climb.
"""
from unittest.mock import MagicMock

from src.re_embedder.embedder import _is_named_instance, _type_word_of_entity


def _mk_db(fetchone_seq=None, fetchall_seq=None):
    """Mock psycopg2 conn whose cursor returns queued fetchone()/fetchall() results in order."""
    db = MagicMock()
    cursor = MagicMock()
    db.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    db.cursor.return_value.__exit__ = MagicMock(return_value=False)
    if fetchone_seq is not None:
        cursor.fetchone.side_effect = list(fetchone_seq)
    if fetchall_seq is not None:
        cursor.fetchall.side_effect = list(fetchall_seq)
    return db, cursor


def test_named_instance_subject_of_instance_of_is_a_name():
    """fraggle is the SUBJECT of `fraggle instance_of poodle` → a named instance (skip climb)."""
    # First query (instance_of presence) returns a row → short-circuits True.
    db, _ = _mk_db(fetchone_seq=[(1,)])
    assert _is_named_instance(db, "fraggle-uuid") is True


def test_pure_name_no_type_word_is_a_name():
    """No instance_of edge, but every alias is a naming-layer object → pure name (skip climb)."""
    # 1) instance_of presence query → None (not an instance subject yet)
    # 2) _type_word_of_entity: naming-object aliases = {"fraggle"}; all aliases = ["fraggle"]
    db, _ = _mk_db(
        fetchone_seq=[None],
        fetchall_seq=[[("fraggle",)], [("fraggle",)]],
    )
    assert _is_named_instance(db, "fraggle-uuid") is True


def test_pure_type_word_is_not_a_name():
    """A TYPE node (poodle) with no instance_of-subject edge and a non-name alias → climbs."""
    # 1) instance_of presence → None
    # 2) _type_word_of_entity: no naming-object aliases; alias "poodle" survives → type-word.
    db, _ = _mk_db(
        fetchone_seq=[None],
        fetchall_seq=[[], [("poodle",)]],
    )
    assert _is_named_instance(db, "poodle-uuid") is False


def test_type_word_excludes_naming_layer_alias():
    """An entity carrying BOTH a name and a type-word returns the TYPE-word, not the name."""
    # naming-object aliases = {"fraggle"}; all aliases ordered = ["fraggle", "dog"] → "dog".
    db, _ = _mk_db(fetchall_seq=[[("fraggle",)], [("fraggle",), ("dog",)]])
    assert _type_word_of_entity(db, "ent-uuid") == "dog"


def test_fail_safe_on_error_treats_as_name():
    """Uncertain (DB error) → treat as a name and SKIP the subclass_of mint (never violate)."""
    db = MagicMock()
    db.cursor.side_effect = RuntimeError("db down")
    assert _is_named_instance(db, "x-uuid") is True


def test_empty_entity_id_is_a_name():
    """No entity → fail-safe True (never mint subclass_of for nothing)."""
    db = MagicMock()
    assert _is_named_instance(db, "") is True
