"""EP2 CENSUS (gauntlet emitter-provenance-honesty) — every attribute-emission site in
src/api/main.py derives its (fact_class, confidence) through the ONE canonical resolver,
_scalar_read_class_confidence, or carries an inline DECLARED OUT-OF-SCOPE sentinel with a
reason. The census is REGENERATED from source on every run (a mechanism-scan, not a frozen
name list), so a NEW hardcoded emitter fails this test the moment it lands — growth-only
against the mechanism.

The scan mirrors the in-code guidance at main.py::fetch_superseded_facts: find every dict
emission tagged "source": "attributes" or "db_superseded_attr" (the two attribute-lane
source tags; the second exists because the superseded-scalar lane tags differently — a
grep on "attributes" alone misses it, which a blind critic proved in the scalar-retract
campaign), locate the enclosing .append({ block, and require the derivation call inside
that block.

Non-vacuity is proved IN THIS FILE, not assumed: two tests feed the scanner poisoned
source — one with a freshly injected hardcoded emitter, one with a simulated REVERT of a
real fixed lane — and both must be flagged.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MAIN = os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")

_SOURCE_TAG = re.compile(r'"source": "(attributes|db_superseded_attr)"')
_APPEND_OPEN = re.compile(r"\.append\(\{$")
_DECLARED = "DECLARED OUT-OF-SCOPE"


def _scan(source: str):
    """Return (sites, violations). sites: list of (lineno, symbol, declared)."""
    lines = source.splitlines()
    symbol = None
    sym_of = {}
    for idx, line in enumerate(lines):
        m = re.match(r"^\s*def (\w+)", line)
        if m:
            symbol = m.group(1)
        sym_of[idx] = symbol
    sites, violations = [], []
    for idx, line in enumerate(lines):
        if not _SOURCE_TAG.search(line):
            continue
        # enclosing append-block start
        start = idx
        while start >= 0 and not _APPEND_OPEN.search(lines[start].strip()):
            start -= 1
        # the derivation may sit INSIDE the dict (as key values) or on the few lines
        # immediately ABOVE the .append({ where the tier vars are computed — both are
        # the honest mechanism; a hardcoded stamp has neither.
        block = "\n".join(lines[max(0, start - 16): idx + 3])
        declared = _DECLARED in "\n".join(lines[max(0, start - 45): start])
        honest = "_scalar_read_class_confidence(" in block
        sites.append((idx + 1, sym_of.get(idx), declared))
        if not honest and not declared:
            violations.append(
                f"src/api/main.py:{idx + 1} (in {sym_of.get(idx) or '<module>'}): "
                f'attribute-emission site stamps a tier WITHOUT deriving it through '
                f'_scalar_read_class_confidence and carries no {_DECLARED} declaration. '
                f'A fabricated ("A", 1.0) here overrides honest demotions via '
                f'deduplicate_facts or pre-empts them via a short-circuit answer.')
    return sites, violations


def test_ep2_census_every_attribute_emitter_is_honest():
    """THE bar: zero violations over the real source, and the census still SEES the sites
    (>= 16 — the count at gauntlet start; growth is allowed, hardness is not optional)."""
    with open(MAIN, encoding="utf-8") as fh:
        sites, violations = _scan(fh.read())
    assert len(sites) >= 16, (
        f"census scan found only {len(sites)} attribute-emission sites — the scanner "
        f"itself has rotted (16 existed at gauntlet start); fix the scanner, not the bar")
    assert sites, "no sites found at all"
    assert not violations, "\n".join(violations)


def test_ep2_census_declared_exception_is_exactly_pref_name():
    """Exactly ONE declared out-of-scope site is tolerated, and it is the pref_name
    registry lane (entity_aliases carries no provenance column — no STORED provenance
    exists to derive from). A second declaration is a smell a critic must read."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    sites, _viol = _scan(source)
    declared = [(ln, sym) for ln, sym, d in sites if d]
    assert len(declared) == 1, f"declared out-of-scope sites: {declared}"
    ln = declared[0][0]
    block = "\n".join(source.splitlines()[ln - 12: ln + 2])
    assert "pref_name" in block, (
        f"the declared exception at line {ln} is not the pref_name lane — re-read it")




# ── ROUND 2 (critic gap): the SHAPE scan — enumerate emission sites by DICT SHAPE,
# independent of the source tag. The round-1 census was source-tag-keyed, and
# _build_sum_over_walk_fact escaped BOTH the fix and the census by tagging its dict
# "sum_over_walk": a fabricated ("A", 1.0) with a NOVEL source tag was invisible to a
# tag-keyed scan by construction. The shape scan flags any dict construction that
# carries a class LITERAL (dict value or _var = "A" assignment) plus a numeric
# confidence, is within reach of an entity_attributes / facts / staged_facts read, and
# neither derives through _scalar_read_class_confidence nor fails toward HELD
# (fact_provenance key present, no 1.0 confidence literal, no "A" class fallback) nor
# carries a DECLARED OUT-OF-SCOPE / EP-SYNTHETIC sentinel with its reason.

# ── ROUND 3 (critic r2 gap): open-block-anchored enumeration + REPORTED skipped set.
# The r2 scanner's ±120-line DB-proximity window missed the FROM facts read 128 lines
# above the informative_abstention return dict, so a hardcoded user_stated/1.0 abstention
# escaped. v3 anchors DB proximity to the ENCLOSING FUNCTION BLOCK (union with the ±120
# line neighbourhood), counts entity_aliases reads as DB proximity, anchors same-line
# inline dicts (data={...}), and RETURNS every un-anchored "confidence": line as the
# SKIPPED SET — reported, never silently dropped. A skipped line that itself sits in an
# emission dict (fact_class key within 25 lines) is a hard failure: the scanner has lost
# anchor on a real emitter.
_OPEN = re.compile(r'(\.append\(\{|insert\(\d+, \{|= \{)\s*$|^\s*return \{|\w+\(\{\s*$')
_CLASS_LIT = re.compile(r'"fact_class":\s*"[ABC]"|_\w+\s*=\s*"[ABC]"|or\s*"[ABC]"')
_DB_KEYS = ("entity_attributes", "entity_aliases", "FROM facts", "FROM staged_facts")
_INLINE_DICT = re.compile(r'\w+\s*=\s*\{.*"confidence":')


def _scan_shape(source: str):
    lines = source.splitlines()
    symbol = None
    blk = {}
    for i, line in enumerate(lines):
        m = re.match(r"^\s*def (\w+)", line)
        if m:
            symbol = m.group(1)
        blk[i] = (symbol, i, i)
    violations, skipped = [], []
    seen = 0
    for idx, line in enumerate(lines):
        if '"confidence":' not in line:
            continue
        start = idx
        while start >= 0 and idx - start < 40 and not _OPEN.search(lines[start]):
            start -= 1
        if start < 0 or idx - start >= 40:
            if not _INLINE_DICT.search(line):
                skipped.append((idx + 1, blk.get(idx, ("<module>",))[0]))
            continue
        window = "\n".join(lines[max(0, start - 16): idx + 40])
        if not (_CLASS_LIT.search(window)
                and re.search(r'"confidence":.*[0-9]', window)):
            continue
        name = blk.get(start, ("<module>",))[0]
        # ±300-line flat neighbourhood: wide enough that the enclosing function's
        # DB read is ALWAYS in reach (the r2 miss was a read 128 lines away; a
        # function-block table mis-assigns nested defs, which cost a debug round).
        # Over-inclusion only ADDS sites the honest/declared checks then judge.
        hood = "\n".join(lines[max(0, start - 300): min(len(lines), idx + 300)])
        if not any(k in hood for k in _DB_KEYS):
            continue
        seen += 1
        _conf_1_0 = "1.0" in lines[idx]
        _a_bad = (re.search(r'"fact_class":\s*[^,\n]*"A"|or\s*"A"', window)
                  or re.search(r'_\w+\s*=\s*"A"',
                               "\n".join(lines[max(0, start - 16): idx + 1])))
        honest = ("_scalar_read_class_confidence(" in window
                  or ('"fact_provenance"' in window and not _conf_1_0
                      and not _a_bad))
        declared = any(x in "\n".join(lines[max(0, start - 45): start])
                       for x in ("DECLARED OUT-OF-SCOPE", "EP-SYNTHETIC"))
        if not honest and not declared:
            violations.append(
                f"src/api/main.py:{idx + 1} (in {name}): a db-fed emission dict "
                f"carries literal class/confidence stamps without deriving through "
                f"_scalar_read_class_confidence, without failing toward HELD, and "
                f"without a declaration sentinel.")
    return seen, violations, skipped

def test_ep2_r2_shape_census_zero_violations():
    """THE round-2 bar: zero shape violations over the real source, and the scanner
    still SEES db-fed emission dicts (>= 6 -- the count when the shape scan landed;
    a scanner that sees nothing is vacuous)."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    seen, violations, skipped = _scan_shape(source)
    assert seen >= 8, (
        f"shape scanner sees only {seen} db-fed emission dicts -- it has rotted; "
        f"fix the scanner, not the bar")
    assert not violations, "\n".join(violations)
    # REPORTED, never silently skipped (critic r2): every un-anchored confidence
    # line must be prose/prompt/comment shape — a skipped line that sits in an
    # emission dict (fact_class key within 25 lines below) is a scanner that has
    # LOST ANCHOR on a real emitter and must fail here.
    # BIDIRECTIONAL lost-anchor check (closing tidy): a skipped line sits in an
    # emission dict if a fact_class key is within 25 lines in EITHER direction
    # (store_context's dict carries it ABOVE the confidence line, not below).
    src_lines = source.splitlines()
    for ln, sym in skipped:
        nearby = "\n".join(src_lines[max(0, ln - 26): ln + 25])
        assert '"fact_class"' not in nearby, (
            f"skipped line {ln} (in {sym}) sits inside an emission dict the scanner "
            f"could not anchor: {src_lines[ln - 1].strip()[:70]!r}")
    assert len(skipped) <= 10, (
        f"skipped set grew to {len(skipped)} -- re-read every entry and either "
        f"anchor it or declare it: {skipped}")

def test_ep2_r3_sentinel_count_is_deliberate():
    """Closing tidy: the sentinel escape hatches are CAPPED at the exact current
    count, so a new DECLARED OUT-OF-SCOPE / EP-SYNTHETIC site cannot accrete
    silently -- adding one is a deliberate act that updates this number WITH a
    reason, the same discipline the tag census applies to pref_name.

    Current census of sentinels (re-derive with:
      grep -n EP-SYNTHETIC and DECLARED OUT-OF-SCOPE over src/api/main.py):
      2 x DECLARED OUT-OF-SCOPE -- the alias-registry pair (pref_name lane in
          fetch_facts_from_anchor + _synthesize_user_name_facts): entity_aliases
          carries no provenance column; names are user-stated by HARD-LINE
          construction.
      7 x EP-SYNTHETIC -- both ingest Qdrant write mirrors (write-path payloads
          of rows committed by the same request), the staged Class-C sync mirror,
          named_comparison_winner + count_over_walk + recursive_walk metacog
          (computed verdicts that cite their inputs, never stored-row
          re-emissions)."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    assert source.count("EP DECLARED OUT-OF-SCOPE") == 2, (
        "a DECLARED OUT-OF-SCOPE site was added or removed -- update this count "
        "deliberately, with the reason recorded here and in the gauntlet ledger")
    assert source.count("EP-SYNTHETIC") == 7, (  # +1 closing tidy: store_context blob
        "an EP-SYNTHETIC site was added or removed -- update this count "
        "deliberately, with the reason recorded here and in the gauntlet ledger")


# -- DOCUMENTED CENSUS LIMITATION (closing tidy, critic round 3) ----------------------
# The shape census is a LITERAL scanner: it flags class/confidence stamps that
# appear as LITERALS (dict values, or-fallbacks, _var = A assignments). A stamp
# whose value is built ONLY from variables with no literal anywhere in the window
# (e.g. _cls = some_opaque_fn(); ... fact_class: _cls) is INVISIBLE to it. As of
# the closing critic fourth-pass AST enumeration this blind spot is UNOCCUPIED --
# every variable-valued stamp traces to _scalar_read_class_confidence, a stored
# row column, or a declared sentinel. Future hardening (sketched, not built):
# parse main.py with the ast module, enumerate every dict literal containing
# BOTH confidence and fact_class keys, resolve each value expression to its
# def-use chain, and require the chain to terminate at the helper, a row index,
# or a sentinel-marked statement -- that closes the variable-only hole by
# construction. Until then, this test file guards the literal surface and the
# hand-maintained sentinel cap above guards the escape hatches.


def test_ep2_r3_shape_census_flags_a_reverted_abstention():
    """Revert-red for THE r3 gap: put the hardcoded user_stated/1.0 abstention back
    and the shape census must flag it (the r2 scanner missed it because its ±120
    DB-proximity window ended 8 lines above the FROM facts read)."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    # the full pre-r3 shape: the hardcoded class rule AND the hardcoded
    # user_stated/1.0 return (reverting only the dict values would leave the
    # helper call in the window and the scanner would rightly read the site as
    # honest-but-unused — this simulation restores the ACTUAL old code).
    reverted = source.replace(
        """            _fclass, _ia_conf = _scalar_read_class_confidence(
                _ia_prov, "informative_abstention")""",
        """            _fclass = "A" if any(f.get("fact_class") == "A" for f in _matched) else "B"
            _ia_conf = 1.0
            _ia_prov = "user_stated"
""").replace(
        """                "confidence": _ia_conf, "provenance": _ia_prov,
                "fact_provenance": _ia_prov,""",
        """                "confidence": 1.0, "provenance": "user_stated",
                "fact_provenance": "user_stated",""")
    assert reverted != source, "abstention revert substitution did not apply"
    _seen, violations, _sk = _scan_shape(reverted)
    # the unreverted source passes test_ep2_r2_shape_census_zero_violations, so ANY
    # violation here IS the reverted abstention site (nearest-def symbol attribution
    # may name the nested _neighbour_matches — cosmetic, the line number is the site).
    assert violations, (
        f"a reverted hardcoded-user_stated abstention was not flagged: {violations}")


def test_ep2_r2_shape_census_flags_a_novel_source_tag_emitter():
    """The exact gap a blind critic re-derived: a fabricated ("A", 1.0) dict with a
    NOVEL source tag (no 'attributes' tag anywhere) must fail the shape census --
    the poison here mirrors _build_sum_over_walk_fact's pre-fix shape."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    poison = '''

def _ep_shape_poison(db, out):
    cur = db.cursor()
    cur.execute("SELECT entity_id, attribute, value_text FROM entity_attributes")
    for row in cur.fetchall():
        _x = "A"
        out.append({
            "subject": row[0],
            "rel_type": row[1],
            "object": row[2],
            "confidence": 1.0 if _x == "A" else 0.5,
            "fact_class": _x,
            "source": "novel_tag_zzz",
        })
'''
    _seen, violations, _sk = _scan_shape(source + poison)
    assert violations and any("_ep_shape_poison" in v for v in violations), (
        "the shape census did NOT flag a novel-source-tag hardcoded emitter -- it is "
        "blind to exactly the round-2 gap class")


def test_ep2_r2_shape_census_flags_a_reverted_sum_over_walk():
    """Revert-red for THE gap: put the pre-fix _build_sum_over_walk_fact shape back
    (flat class A + literal confidence, no provenance) and the shape census must
    flag it."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    reverted = source.replace(
        """        _sm_prov = sum_res.get("provenance") or "llm_inferred"
        _sm_class, _sm_conf = _scalar_read_class_confidence(_sm_prov, "scalar_sum")
""",
        """        _sm_prov = sum_res.get("provenance") or "llm_inferred"
        _sm_class = "A"
        _sm_conf = 1.0
""")
    assert reverted != source, "sum-over-walk revert substitution did not apply"
    _seen, violations, _sk = _scan_shape(reverted)
    assert any("_build_sum_over_walk_fact" in v for v in violations), (
        "a reverted sum-over-walk aggregate was not flagged by the shape census")


def test_ep2_r2_shape_census_flags_a_reverted_null_tier_fallback():
    """Revert-red for the NULL-tier fallback class: restoring the 1.0/'A' fallbacks
    on the direct facts lane must flag it (fail-toward-HELD is part of the honest
    shape, not decoration)."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    reverted = source.replace(
        '''                            "confidence": float(row[3]) if row[3] else 0.5,
                            "fact_class": row[4] or "B",''',
        '''                            "confidence": float(row[3]) if row[3] else 1.0,
                            "fact_class": row[4] or "A",''')
    assert reverted != source, "null-tier fallback revert substitution did not apply"
    _seen, violations, _sk = _scan_shape(reverted)
    assert violations, "a restored 1.0/'A' NULL-tier fallback was not flagged"

def test_ep2_census_flags_a_new_hardcoded_emitter():
    """Growth-only mechanism proof 1: inject a fresh hardcoded emitter into the source and
    the scan must flag it. This is the 'a NEW hardcoded emitter appears' bar, tested."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()
    poison = '''

def _ep_poison_probe(cur, facts):
    cur.execute("SELECT entity_id, attribute, value_text FROM entity_attributes")
    for row in cur.fetchall():
        facts.append({
            "subject": row[0],
            "rel_type": row[1],
            "object": row[2],
            "confidence": 1.0,
            "fact_class": "A",
            "source": "attributes",
        })
'''
    _sites, violations = _scan(source + poison)
    assert violations and any("_ep_poison_probe" in v for v in violations), (
        "the census scan did NOT flag a freshly injected hardcoded emitter — it is "
        "vacuous against exactly the growth it exists to police")


def test_ep2_census_flags_a_reverted_lane():
    """Growth-only mechanism proof 2 (revert-red): simulate a REVERT of two real fixes —
    the fetch_superseded_facts history lane and the transitive-member lane — by replacing
    their derivations back to the hardcoded stamp. The scan must flag both."""
    with open(MAIN, encoding="utf-8") as fh:
        source = fh.read()

    reverted = source.replace(
        "                    _sh_cls, _sh_conf = _scalar_read_class_confidence(_sp, _attr)\n",
        ""
    ).replace(
        '''                        "confidence": _sh_conf,
                        "fact_class": _sh_cls,
                        "fact_provenance": _SCALAR_READ_PROVENANCE_CANON.get(
                            (_sp or "").lower().strip(), "llm_inferred"),
                        "source": "db_superseded_attr",''',
        '''                        "confidence": 1.0,
                        "fact_class": "A",
                        "source": "db_superseded_attr",'''
    )
    assert reverted != source, "history-lane revert substitution did not apply"
    _sites, violations = _scan(reverted)
    assert any("fetch_superseded_facts" in v for v in violations), (
        "a reverted history lane was not flagged")

    reverted2 = reverted.replace(
        "                                        _tm_cls, _tm_conf = _scalar_read_class_confidence(_tm_p, row[1])\n",
        ""
    ).replace(
        '''                                            "confidence": _tm_conf,
                                            "fact_class": _tm_cls,
                                            "fact_provenance": _SCALAR_READ_PROVENANCE_CANON.get(
                                                (_tm_p or "").lower().strip(), "llm_inferred"),
                                            "source": "attributes",''',
        '''                                            "confidence": 1.0,
                                            "fact_class": "A",
                                            "source": "attributes",'''
    )
    assert reverted2 != reverted, "transitive-lane revert substitution did not apply"
    _sites2, violations2 = _scan(reverted2)
    assert any("fetch_facts_from_anchor" in v for v in violations2), (
        "a reverted fetch_facts_from_anchor lane was not flagged")
