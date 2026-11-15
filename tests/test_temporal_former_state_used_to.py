"""FORMER-STATE grammar in recall prose (convert_to_prose).

A state fact that WAS true (an undated stored 'past', or a superseded `historical` row)
must read as PROPER grammatical past — "You used to live in London" / "Diane used to work
for Acme" — NOT the old broken "Previously, You live in London" (a present-tense verb with
a mid-sentence capital grafted onto a present clause). The consuming LLM weaves the recall
prose verbatim, so broken grammar erodes trust.

Hard requirement: SUBJECT-AGNOSTIC and REL-AGNOSTIC. The transform is keyed ONLY on the
rendered sentence shape "<subject> <verb> <rest>" + a closed-class English de-conjugation
of the verb to its base form. There is NO rel-name, subject-name, or place literal in the
change under test. Proven here across:
  • 1st person ("you") AND 3rd person (a named subject),
  • multiple `state` rels (lives_in, works_for) through the identical path,
  • the current-vs-former contrast ("used to" former + "currently" surviving current),
  • no-regression: single present state, multi-valued state (all current, no "used to").

Hermetic: the per-tenant rel_type overlay (temporal_class + templates) and the identity
set are stubbed, exactly like tests/test_temporal_undated_state_framing.py — the direct
convert_to_prose call IS the query-side prose seam, and it asserts the ACTUAL rendered text.
"""
import os
import uuid

import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql://x")
import src.api.main as m


# ── de-conjugation unit: present finite verb → BASE (grammar-primitive, closed-class) ──

@pytest.mark.parametrize("surface,base", [
    ("live", "live"),      # 1st/2nd person present is already base
    ("lives", "live"),     # 3rd-person -s
    ("work", "work"),
    ("works", "work"),
    ("stays", "stay"),
    ("is", "be"), ("are", "be"), ("am", "be"), ("was", "be"), ("were", "be"),
    ("has", "have"), ("had", "have"),
    ("does", "do"), ("goes", "go"),
    ("watches", "watch"), ("fixes", "fix"), ("passes", "pass"),
    ("carries", "carry"), ("flies", "fly"),
])
def test_verb_present_to_base(surface, base):
    assert m._verb_present_to_base(surface) == base


@pytest.mark.parametrize("modal", ["will", "would", "can", "must", "used"])
def test_verb_present_to_base_declines_modals(modal):
    # A modal/aux must NOT get a "used to" reframe → None → caller keeps old form.
    assert m._verb_present_to_base(modal) is None


def test_reframe_former_state_1p_and_3p():
    assert m._reframe_former_state("You live in London", "you") == "You used to live in London"
    assert m._reframe_former_state("Diane lives in London", "Diane") == "Diane used to live in London"
    # copula → base "be"
    assert m._reframe_former_state("You are worried", "you") == "You used to be worried"


def test_reframe_former_state_failsafe_returns_none():
    # possessive scalar (does not lead with a whole subject token) → fail-safe None
    assert m._reframe_former_state("your age is 30", "you") is None
    # negated clause → fail-safe None (needs "used not to" we do not attempt)
    assert m._reframe_former_state("You are not worried", "you") is None


# ── query-side prose seam (convert_to_prose) — ACTUAL rendered text ────────────────────

_STATE_OVERLAY = {
    "lives_in": {"temporal_class": "state", "natural_language": "X lives in Y",
                 "natural_language_2p": "You live in Y", "label": "lives in",
                 "tail_types": ["ANY"]},
    "works_for": {"temporal_class": "state", "natural_language": "X works for Y",
                  "natural_language_2p": "You work for Y", "label": "works for",
                  "tail_types": ["ANY"]},
    # multi-valued state: both values stay current, NO "used to" reframe.
    "speaks": {"temporal_class": "state", "natural_language": "X speaks Y",
               "natural_language_2p": "You speak Y", "label": "speaks",
               "tail_types": ["ANY"], "is_single_valued": False},
}


@pytest.fixture
def _stub_overlay(monkeypatch):
    monkeypatch.setattr(m.rel_type_overlay, "resolve_current",
                        lambda *a, **k: _STATE_OVERLAY)
    monkeypatch.setattr(m, "_build_identity_set", lambda *a, **k: {"user"})


def _pf(rel, subj, obj_uuid, status, ed=None):
    return {"rel_type": rel, "_subject_id": subj, "_object_id": obj_uuid,
            "temporal_status": status, "event_date": ed, "confidence": 1.0,
            "fact_class": "A"}


@pytest.mark.parametrize("rel,verb", [("lives_in", "live"), ("works_for", "work")])
def test_1p_past_reads_used_to(_stub_overlay, rel, verb):
    """1st person: a lone undated past state → "You used to <base> <object>"."""
    obj = str(uuid.uuid4())
    out = m.convert_to_prose(
        [_pf(rel, "user", obj, "past")],
        db=None, anchor="user", user_id="user",
        preferred_alias_map={obj: "London" if rel == "lives_in" else "Acme"})
    assert out, out
    assert out[0].startswith("You used to %s " % verb), out[0]
    assert not out[0].startswith("Previously,"), out[0]


@pytest.mark.parametrize("rel,verb", [("lives_in", "live"), ("works_for", "work")])
def test_3p_named_subject_past_reads_used_to_base(_stub_overlay, rel, verb):
    """3rd person: a NAMED subject's past state de-conjugates the +s verb to base —
    "Diane used to live in ...", NOT "Diane used to lives ..." / "lived ...". Same path."""
    subj, obj = str(uuid.uuid4()), str(uuid.uuid4())
    pam = {subj: "Diane", obj: "London" if rel == "lives_in" else "Acme"}
    out = m.convert_to_prose(
        [_pf(rel, subj, obj, "past")],
        db=None, anchor="user", user_id="user", preferred_alias_map=pam)
    assert out, out
    assert out[0].startswith("Diane used to %s " % verb), out[0]
    assert "lives" not in out[0] and "lived" not in out[0], out[0]


def test_current_vs_former_contrast(_stub_overlay):
    """Coexisting former + current: former reads "used to", current reads "currently"."""
    tor, van = str(uuid.uuid4()), str(uuid.uuid4())
    pam = {tor: "Toronto", van: "Vancouver"}
    out = m.convert_to_prose(
        [_pf("lives_in", "user", van, "now"), _pf("lives_in", "user", tor, "past")],
        db=None, anchor="user", user_id="user", preferred_alias_map=pam)
    former = next(p for p in out if "Toronto" in p)
    current = next(p for p in out if "Vancouver" in p)
    assert former == "You used to live in Toronto", former
    assert current == "You currently live in Vancouver", current


def test_single_present_state_unchanged(_stub_overlay):
    """No former present → plain current assertion, no "currently", no "used to"."""
    van = str(uuid.uuid4())
    out = m.convert_to_prose(
        [_pf("lives_in", "user", van, "now")],
        db=None, anchor="user", user_id="user", preferred_alias_map={van: "Vancouver"})
    assert out == ["You live in Vancouver"], out


def test_multivalued_state_both_current_no_used_to(_stub_overlay):
    """Multi-valued state (speaks): both values current → NEITHER gets "used to"/"currently"."""
    en, fr = str(uuid.uuid4()), str(uuid.uuid4())
    pam = {en: "English", fr: "French"}
    out = m.convert_to_prose(
        [_pf("speaks", "user", en, "now"), _pf("speaks", "user", fr, "now")],
        db=None, anchor="user", user_id="user", preferred_alias_map=pam)
    joined = " || ".join(out)
    assert "used to" not in joined, joined
    assert "currently" not in joined, joined
    assert "English" in joined and "French" in joined, joined
