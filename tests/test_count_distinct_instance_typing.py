"""COUNT-DISTINCT-INSTANCES cluster (the biggest LME miss cluster, ~50 "how many <type>" questions).

Root cause (INGEST, gap a): a SPECIFIC INSTANCE is COUNTABLE only when it is filed ``instance_of`` its
BARE HEAD-NOUN type, so distinct instances of the same kind UNIFY under one countable L4 type node that
``_l4_direct_instance_count`` / ``_resolve_count_type_uuids`` can resolve + count. The deriver reified
that companion ``instance_of`` edge ONLY for the direct object of a content verb ("I bought a navy blue
BLAZER" → blazer instance_of blazer-head). The SAME distinguishing NP introduced by:

  (1) a FIRST-PERSON POSSESSION  — "my Marketing Research class PROJECT" → (user, owns, <NP>) with NO
      companion type edge → uncountable; and
  (2) a NAMING construction      — "a 20-gallon community tank named Amazonia" → filed instance_of the
      UN-UNIFIED premodified NP ("new 20-gallon community tank"), not the bare head "tank" → no other
      instance of the kind shares that type node → the cardinality undercounts / misses.

was left un-typed / mis-typed. Fix (one convention, two seams in derive_sentence_facts): file every
reified specific instance at its BARE HEAD type — the naming chain uses the bare head, and the SVO
companion-instance_of reification is extended to the possession backbone rel + possessive determiner.

These are DERIVER-level (fail-on-old / pass-on-fix), covering the projects (6d550036) and tanks
(46a3abf7) exemplars of the cluster. Subject-agnostic, deterministic, no seeding.
"""
import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2023, 5, 30)


def _facts(text):
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", SPINE_NAMING_CHAIN="true")
    return {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(text, _REF)}


# ── FACET 1: possessed distinguishing NP is typed instance_of its bare head (countable) ──────────

@requires_model
def test_possessed_premodified_instance_is_typed_countable():
    # 6d550036 "how many projects have I led or am currently leading" -> 2. The led project arrives via
    # a first-person POSSESSION ("my Marketing Research class project") — before the fix it landed only
    # as (user, owns, <NP>) with no instance_of, so a "how many projects" cardinality had nothing to
    # count under the resolved "project" type. Now it reifies as instance_of the BARE HEAD "project".
    facts = _facts("I led the data analysis team for my Marketing Research class project.")
    assert ("marketing research class project", "instance_of", "project") in facts, facts
    # and it is still the possessed thing (the owns backbone is intact — additive companion only).
    assert ("user", "owns", "marketing research class project") in facts, facts


@requires_model
def test_possession_and_svo_project_unify_under_one_type():
    # The SAME cluster: a SVO-object project ("working on a solo project") and a possessed project must
    # BOTH file instance_of the SAME bare head "project" so two distinct instances UNIFY + count as 2.
    solo = _facts("I've been working on a solo project for my Data Mining class.")
    poss = _facts("I led the data analysis team for my Marketing Research class project.")
    assert ("solo project for data mining class", "instance_of", "project") in solo, solo
    assert ("marketing research class project", "instance_of", "project") in poss, poss


# ── FACET 2: named instance files instance_of the BARE HEAD type, not the premodified NP ─────────

@requires_model
def test_named_instance_typed_at_bare_head_not_premodified_np():
    # 46a3abf7 "how many tanks do I currently have" -> 3. Amazonia arrives via a naming construction
    # ("a 20-gallon community tank named Amazonia"). Before the fix it was filed instance_of the full
    # descriptive NP "new 20-gallon community tank" — an un-unified singleton type that a "how many
    # tanks" resolve("tank") never reaches. Now it files instance_of the bare head "tank".
    facts = _facts("I've set up a new 20-gallon community tank named Amazonia.")
    assert ("amazonia", "instance_of", "tank") in facts, facts
    # the un-unified premodified type node must NOT be minted (that is exactly the undercount cause).
    assert not any(r == "instance_of" and o != "tank"
                   for (s, r, o) in facts if s == "amazonia"), facts


@requires_model
def test_named_and_svo_tanks_unify_under_one_type():
    # Amazonia (naming) and the friend's-kid tank (SVO object) must BOTH be instance_of the bare "tank"
    # so distinct tanks unify + count. (subject-agnostic: no tank/aquarium literal anywhere.)
    amazonia = _facts("I've set up a new 20-gallon community tank named Amazonia.")
    kid = _facts("I set up a small 1-gallon tank for a friend's kid.")
    assert ("amazonia", "instance_of", "tank") in amazonia, amazonia
    assert ("small 1-gallon tank for kid", "instance_of", "tank") in kid, kid
