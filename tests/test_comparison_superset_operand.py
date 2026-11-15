"""Temporal-comparison recall cluster — DIRECTIONAL operand resolution + dated-symmetry.

Live-grounded root cause (LME gpt4_0b2f1d21 / gpt4_6ed717ea, test seat 04658f0e):
a "which did I <verb> first, A or B" comparison MISSED because the dative-for / amod fold
at CAPTURE composes the beneficiary + premodifiers INTO the acquisition object surface —
"I got a new dog bed for Max" is stored under the alias ``new dog bed for max`` (@date).
The comparison NAMES the operand as the SHORTER "dog bed for max". The existing
``_resolve_phrase_to_longest_alias`` matches only ``alias ⊆ phrase`` (candidates no LONGER
than the phrase), so it can NEVER reach the folded (longer) surface → the operand never
resolves to its dated acquisition → the comparison walk finds nothing.

Two deterministic, flag-gated (default ON) fixes, pinned here:
  1. ``_resolve_phrase_to_superset_aliases`` — the DIRECTIONAL complement: resolve the
     operand to the SHORTEST registered alias that WHOLLY CONTAINS the phrase
     (``phrase ⊆ alias``), token-boundary, no fuzz.
  2. dated-symmetry DEGRADE-TO-LIST in ``_apply_get_first_last`` — a genuine ≥2-operand
     comparison with <2 DATE-reachable operands DEGRADES to the chronological ordered-event
     list (earliest first) instead of fabricating a single confident winner from the one
     dated operand. It must NEVER return [] here: the batch-1 empty-abstain rendered "No
     relevant facts found." and regressed three baseline-HIT comparisons (bike/car,
     Page Turners/Marketing, Zero/Ferrari) to MISS by burying the answerable list that
     CONTAINED the gold. The guard keeps its precision intent (no single fabricated winner)
     while PRESERVING the ordered list.
"""
from src.api import main


# ── fake DB cursor returning entity_aliases rows (DB does the ORDER BY; the test
#    returns rows already SHORT-TO-LONG to mirror it) ──────────────────────────────
class _AliasCursor:
    def __init__(self, rows):
        self._all = rows
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        phrase = (params[0] if params else "") or ""
        if "from entity_aliases" in s and "char_length(alias) >= char_length" in s:
            # superset: aliases at least as long as the phrase, SHORTEST first
            cand = [(a, e) for (a, e) in self._all if len(a) >= len(phrase)]
            cand.sort(key=lambda r: (len(r[0]), r[0]))
            self._rows = cand
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class _AliasDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _AliasCursor(self._rows)


# entity_aliases fixture from the live test-seat capture.
D_DOGBED = "d512a144-9878-5972-8634-5729d95afd34"
E_LUNA = "4d3a94a3-4963-52ef-adc7-836647691dd1"
_ALIASES = [
    ("new dog bed for max", D_DOGBED),
    ("dog bed max orthopedic memory foam dog petco", "697baa42-95ca-5024-aa73-381d40cc9aa5"),
    ("luna", E_LUNA),
    ("puppy luna", "f4d1dbd1-cfdd-5e27-8473-06548dac45e2"),
    ("eco friendly training pads from chewy.com", "eece49ca-4c2f-5875-b2b2-b3d3accba4b5"),
    ("set 10", "db82bc41-26e5-590d-a3d8-ddab7fe3aae9"),
]


# ── (1) superset resolver — the directional fix ───────────────────────────────────
def test_superset_resolves_folded_beneficiary_surface():
    """"dog bed for max" ⊆ the folded alias "new dog bed for max" → its DATED entity."""
    db = _AliasDB(_ALIASES)
    ids = main._resolve_phrase_to_superset_aliases(db, "dog bed for max")
    assert ids == [D_DOGBED]


def test_superset_picks_shortest_superset():
    """Closest (tightest) superset wins over a longer coincidental alias."""
    db = _AliasDB(_ALIASES + [("brand new dog bed for max deluxe edition", "zzz")])
    ids = main._resolve_phrase_to_superset_aliases(db, "dog bed for max")
    assert ids == [D_DOGBED]  # "new dog bed for max" is shorter than the deluxe alias


def test_superset_no_containing_alias_returns_empty():
    """"training pads for luna" is in NO alias → [] (the capture-gap operand stays absent)."""
    db = _AliasDB(_ALIASES)
    assert main._resolve_phrase_to_superset_aliases(db, "training pads for luna") == []


def test_superset_token_boundary_no_midword():
    """Containment is token-boundary — a mid-word hit ("max" in "maxwell") does NOT match."""
    db = _AliasDB([("maxwell house coffee", "q")])
    assert main._resolve_phrase_to_superset_aliases(db, "max") == []


def test_superset_failsafe_no_db():
    assert main._resolve_phrase_to_superset_aliases(None, "dog bed for max") == []
    assert main._resolve_phrase_to_superset_aliases(_AliasDB(_ALIASES), "") == []


# ── (2) dated-symmetry abstain in _apply_get_first_last ───────────────────────────
A = "00000000-0000-0000-0000-0000000000a1"
B = "00000000-0000-0000-0000-0000000000b1"
USER = "00000000-0000-0000-0000-0000000000ff"


def _cmp_facts():
    # Only operand A carries a dated edge; operand B is undated (the asymmetric case).
    return [
        {"subject": USER, "rel_type": "get", "object": A, "event_date": "2023-04-29"},
        {"subject": USER, "rel_type": "use_for", "object": B, "event_date": None},
    ]


def _both_dated_facts():
    # BOTH operands carry a dated edge — but the caller's reachability count UNDERCOUNTED
    # them (dated_operand_groups=1), the reified/type-instance undercount that drove the
    # regression. The degrade must surface BOTH events (the list that contains the gold).
    return [
        {"subject": USER, "rel_type": "get", "object": A, "event_date": "2023-04-29"},
        {"subject": USER, "rel_type": "get", "object": B, "event_date": "2023-04-20"},
    ]


def test_dated_symmetry_degrades_to_ordered_list_not_empty():
    """REGRESSION PIN: the guard must DEGRADE to the ordered list, NEVER return []."""
    out = main._apply_get_first_last(
        _both_dated_facts(), "first", operand_ids=[A, B],
        operand_group_count=2, dated_operand_groups=1)
    assert out != []  # the batch-1 empty-abstain that regressed 3 questions is GONE
    dates = [f.get("event_date") for f in out if f.get("event_date")]
    # both events surface, chronological (earliest first) — the answerable list, not a
    # single fabricated confident winner (both dated events are present, not collapsed to 1).
    assert dates == ["2023-04-20", "2023-04-29"]
    assert {f["object"] for f in out if f.get("event_date")} == {A, B}


def test_dated_symmetry_degrade_surfaces_the_dated_event_when_one_undated():
    """Asymmetric case: still surfaces the dated event list — never empty, never fabricated."""
    out = main._apply_get_first_last(
        _cmp_facts(), "first", operand_ids=[A, B],
        operand_group_count=2, dated_operand_groups=1)
    assert out != []  # NOT "No relevant facts found."
    assert any(f.get("object") == A and f.get("event_date") for f in out)


def test_both_operands_dated_orders_normally():
    """Superset resolver preserved: a genuine 2-dated comparison still orders correctly."""
    facts = [
        {"subject": USER, "rel_type": "get", "object": A, "event_date": "2023-04-29"},
        {"subject": USER, "rel_type": "get", "object": B, "event_date": "2023-04-20"},
    ]
    out = main._apply_get_first_last(
        facts, "first", operand_ids=[A, B],
        operand_group_count=2, dated_operand_groups=2)
    # guard does NOT fire (dated_groups=2) → comparison render returns both operands,
    # WINNER first (04-20 == B is earliest).
    assert out and out[0]["object"] == B


def test_guard_inert_when_counts_absent():
    """dated_operand_groups=None (flag OFF) → byte-for-byte the pre-fix behaviour."""
    out = main._apply_get_first_last(
        _cmp_facts(), "first", operand_ids=[A, B],
        operand_group_count=0, dated_operand_groups=None)
    # the single dated operand surfaces (today's behaviour when the guard is off) —
    # NOT an empty abstain.
    assert out and out[0]["object"] == A
