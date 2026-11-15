"""MEASURED-VALUE (currency) EXPENSE-TYPING — the LongMemEval SUM-of-scalars ingest root.

Cluster (qids gpt4_d84a3211 gold=$185, aae3761f gold=15h, 7024f17c gold=0.5h): an aggregation ask
("how much total money have I spent on bike-related expenses", "how many hours … driving to my
three road trip destinations") totals a per-item MEASURE across the typed instances the user named.
The deployed query resolver (``_l4_direct_scalar_sum``) sums the numeric ``entity_attributes``
scalars carried by the DISTINCT instances that are ``instance_of`` the resolved TYPE — but the
deriver never gave the priced items a groundable type: a "$40" copula complement mis-cast as a
person ``age`` (unit stripped) and NO ``instance_of expense`` edge existed, so the type phrase
"bike-related expenses" grounded to nothing → the product answered "I don't have any … on record".

The INGEST fix (``derive_sentence_facts``, additive, subject-agnostic, deterministic, flag
``SPINE_MEASURED_VALUE_TYPE`` default ON):
  (1) a CURRENCY-shaped scalar VALUE predicated on an entity types that entity ``instance_of
      expense`` (a cost the user incurred) — DATATYPE-driven, NO domain word list, mirrors
      ``_chain_natal_birth``'s ``instance_of baby``. Refs: RDFS/OWL datatype-driven type inference.
  (2) a currency copula complement ("the lights were $40") is captured as a CURRENCY scalar with
      the symbol PRESERVED (``cost=$40``), NEVER a bare ``age=40`` — so the value keeps its unit and
      the expense-typing above can reach it.

The QUERY fix (``_resolve_count_type_uuids``, flag ``COUNT_HEAD_NOUN_FALLBACK`` default ON): when a
COMPOUND type phrase grounds nothing ("bike-related expenses" has no L4 node) it falls back to its
grammatical HEAD noun ("expense") — the head IS the type; a pre-head modifier only narrows it — so
the aggregation reaches the ``instance_of expense`` instances the ingest fix filed.

FAILS on the old code (no ``instance_of expense``; "$40" → age; no head-noun grounding);
PASSES on the fix. Covers MULTIPLE cluster exemplars (chain/lights/helmet) end-to-end at the
function level: clean currency scalars on typed instances → Σ = $185.
"""
import pytest

from src.extraction.linguistics import derive_sentence_facts
from src.api import main as m

_REF = "2024/05/01"


def _facts(sentence):
    return derive_sentence_facts(sentence, _REF) or []


def _instance_of(facts, type_name):
    return sorted({f.subject for f in facts
                   if f.rel_type == "instance_of" and f.object == type_name})


def _currency_scalars(facts):
    # (subject, value) pairs whose object surface carries a currency symbol
    return sorted((f.subject, f.object) for f in facts
                  if f.object and f.object.strip().startswith(("$", "£", "€", "¥")))


# ── INGEST (1): a currency-valued item is typed instance_of expense (fail-on-old) ──────────────
@pytest.mark.parametrize("sentence", [
    "I bought my Bell Zephyr helmet for $120.",
    "I got a new set of bike lights installed, which were $40.",
    "The tune-up cost me $25.",
])
def test_currency_item_typed_instance_of_expense(sentence):
    facts = _facts(sentence)
    exp = _instance_of(facts, "expense")
    assert exp, (
        f"no instance_of expense edge — the SUM walk has no typed instance to aggregate: "
        f"{sentence!r} → facts={[(f.subject, f.rel_type, f.object) for f in facts]}"
    )


def test_currency_copula_preserves_symbol_not_age():
    # "the lights were $40" — the OLD copula-measure cast this as a bare person `age=40` (unit
    # stripped). The fix captures it as a CURRENCY scalar with the $ preserved, and NO `age` edge.
    facts = _facts("I got a new set of bike lights installed, which were $40.")
    ages = [f for f in facts if f.rel_type == "age"]
    assert not ages, f"currency copula still mis-cast as age: {[(f.subject, f.object) for f in ages]}"
    cur = _currency_scalars(facts)
    assert any(v.strip().startswith("$") and "40" in v for _, v in cur), (
        f"$40 not captured as a currency scalar: {cur}"
    )


def test_non_currency_scalar_not_typed_expense():
    # a bare measured person age ("Sarah is 28") is NOT a currency value → NO instance_of expense.
    facts = _facts("Sarah is 28 years old.")
    assert not _instance_of(facts, "expense")


def test_user_subject_never_typed_expense():
    # a person is not an expense: even if a currency value lands on the user, no user->expense edge.
    facts = _facts("I spent $50.")
    assert "user" not in _instance_of(facts, "expense")


def test_ingest_flag_off_is_byte_identical(monkeypatch):
    # SPINE_MEASURED_VALUE_TYPE=off ⇒ NO instance_of expense typing AND the copula "$40" falls back
    # to the legacy bare-NUM → age reading — byte-for-byte the pre-fix behaviour.
    monkeypatch.setenv("SPINE_MEASURED_VALUE_TYPE", "off")
    facts = _facts("I got a new set of bike lights installed, which were $40.")
    assert not _instance_of(facts, "expense")
    assert any(f.rel_type == "age" for f in facts)   # legacy reading restored


# ── QUERY: head-noun fallback grounds a compound type phrase to its head (fail-on-old) ─────────

class _AliasCursor:
    """Minimal fake for _resolve_count_type_uuids: exact-alias lookup + (empty) taxonomy + descent."""
    def __init__(self, alias_map):
        self._alias = {k.lower(): v for k, v in alias_map.items()}
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM entity_aliases WHERE alias = ANY" in s:
            cands = params[0]
            ids = []
            for c in cands:
                ids += self._alias.get(str(c).lower(), [])
            self._result = [(i,) for i in ids]
        elif "FROM entity_taxonomies" in s:
            self._result = []
        elif "FROM entity_aliases WHERE lower(alias) = ANY" in s:
            self._result = []
        else:
            self._result = []  # no subclass descent in these fixtures

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _AliasDB:
    def __init__(self, alias_map):
        self._a = alias_map

    def cursor(self):
        return _AliasCursor(self._a)


def test_head_noun_fallback_grounds_compound_phrase(monkeypatch):
    # "bike-related expenses" has NO L4 node; only the HEAD "expense" is aliased. The OLD resolver
    # returned {} (full-phrase miss); the head-noun fallback grounds the head type node.
    monkeypatch.setattr(m, "_get_classification_rels",
                        lambda db: frozenset({"subclass_of", "instance_of"}))
    db = _AliasDB({"expense": ["expense-node"]})
    out = m._resolve_count_type_uuids(db, "bike-related expenses")
    assert out == {"expense-node"}


def test_head_noun_fallback_only_when_full_phrase_misses(monkeypatch):
    # when the FULL phrase grounds, the fallback must NOT widen to the head — byte-for-byte today.
    monkeypatch.setattr(m, "_get_classification_rels",
                        lambda db: frozenset({"subclass_of", "instance_of"}))
    db = _AliasDB({"road bike": ["roadbike-node"], "bike": ["bike-node"]})
    out = m._resolve_count_type_uuids(db, "road bikes")
    assert out == {"roadbike-node"}          # NOT the broader "bike-node"


def test_head_noun_fallback_disabled_is_byte_identical(monkeypatch):
    # flag OFF → the compound miss stays a miss (legacy behaviour).
    monkeypatch.setenv("COUNT_HEAD_NOUN_FALLBACK", "0")
    monkeypatch.setattr(m, "_get_classification_rels",
                        lambda db: frozenset({"subclass_of", "instance_of"}))
    db = _AliasDB({"expense": ["expense-node"]})
    assert m._resolve_count_type_uuids(db, "bike-related expenses") == set()


# ── END-TO-END at the function level: the three bike expenses Σ to the gold $185 ──────────────

def test_bike_expenses_sum_to_gold_185():
    # the three priced items (chain $25, lights $40, helmet $120) as instance_of expense, each
    # carrying its currency scalar → _l4_direct_scalar_sum totals the gold $185, flagged currency.
    class _SumCursor:
        def __init__(self, insts, rows):
            self._insts, self._rows, self._r = insts, rows, []

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if "FROM facts" in s and "UNION" in s and "object_id = ANY" in s:
                self._r = [(i,) for i in self._insts]
            elif "FROM entity_attributes" in s and "entity_id = ANY" in s:
                self._r = list(self._rows)
            else:
                self._r = []

        def fetchall(self):
            return list(self._r)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _SumDB:
        def __init__(self, insts, rows):
            self._i, self._r = insts, rows

        def cursor(self):
            return _SumCursor(self._i, self._r)

    rows = [("chain", "cost", None, "$25"),
            ("bike lights", "cost", None, "$40"),
            ("bell zephyr helmet", "buy", None, "$120")]
    out = m._l4_direct_scalar_sum(
        _SumDB(["chain", "bike lights", "bell zephyr helmet"], rows),
        {"expense-node"}, {"instance_of", "is_a"})
    assert out["miss"] is False
    assert out["sum"] == 185.0
    assert out["is_currency"] is True
    assert out["n_components"] == 3
    fact = m._build_sum_over_walk_fact({**{"type_phrase": "bike-related expenses",
                                           "measure_phrase": "total money"}, **out}, "user")
    assert "$185" in fact["definition"] and fact["fact_class"] == "A"
