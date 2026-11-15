# -*- coding: utf-8 -*-
"""THE RELATIONAL NEGATION-DELETION IN THE STATEMENT LANE (LIVE-OPENWEBUI FIX 2).

THE MEASURED WOUND (DEV/BUGS/family-smoke-20261111/LIVE-OPENWEBUI-DIAGNOSIS.md, the owner's
prod seat): "Chris does not have a child named Aurora Flood" routed STATEMENT (the ingest
lane) and the negated name-type binding was SKIPPED SILENTLY — no negated row anywhere —
so Aurora later landed LIVE from the stale-chat re-assert (born_on + age=0 stored). The
scalar-own negation already works ("Chris does not own a Goose" → (user, owns, goose)
polarity='negated'); the relational did not.

THE FIX: the named-binding chain (``_chain_object_named_value`` — the always-on seam) emits
the negated binding's CATEGORY RELATION with negated=True — "X does not have a child named
Y" → (Y, child_of, X) negated; "I do not have a cat named Z" → (user, has_pet, Z) negated —
never the instance_of/alias/age edges (a denial names the RELATION's absence). Flag
SPINE_NEGATED_NAME_BINDING default ON; OFF → the skip is byte-parity.

No database: POSTGRES_DSN is cleared (the deriver is deterministic; the possession-rel
resolver fail-opens to its default)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.pop("POSTGRES_DSN", None)

import src.extraction.linguistics as L  # noqa: E402

# THE MEASURED PROD SENTENCES (the diagnosis §2).
MEASURED_CHILD = "Chris does not have a child named Aurora Flood"
MEASURED_PET = "I do not have a cat named Goose"


def _facts(text):
    return L.derive_sentence_facts(text, "user") or []


def _as_dicts(facts):
    out = []
    for f in facts:
        out.append(vars(f) if hasattr(f, "__dict__") else dict(f))
    return out


def test_measured_child_denial_emits_negated_child_of():
    """THE PIN: 'X does not have a child named Y' → the negated child_of row."""
    rows = _as_dicts(_facts(MEASURED_CHILD))
    neg = [r for r in rows
           if r.get("rel_type") == "child_of" and r.get("negated")]
    assert len(neg) == 1, rows
    assert "aurora flood" in str(neg[0].get("subject"))
    assert "chris" in str(neg[0].get("object"))


def test_measured_child_denial_never_asserts_type_or_alias():
    """A denial names the RELATION's absence — no instance_of/also_known_as edge."""
    rows = _as_dicts(_facts(MEASURED_CHILD))
    for r in rows:
        if "aurora flood" in str(r.get("subject")) or "aurora" in str(r.get("subject")):
            assert r.get("rel_type") not in ("instance_of", "also_known_as", "age",
                                             "has_gender"), rows


def test_measured_pet_denial_emits_negated_has_pet():
    rows = _as_dicts(_facts(MEASURED_PET))
    neg = [r for r in rows
           if r.get("rel_type") == "has_pet" and r.get("negated")]
    assert len(neg) == 1, rows
    assert "goose" in str(neg[0].get("object"))


def test_affirmed_binding_unchanged():
    """The affirmed shapes keep their full canonical edge set (byte-parity up the lane)."""
    rows = _as_dicts(_facts("We have a cat named Goose"))
    assert any(r.get("rel_type") == "has_pet" and not r.get("negated") for r in rows)
    assert any(r.get("rel_type") == "instance_of" and r.get("object") == "cat"
               for r in rows)
    kin = _as_dicts(_facts("Chris has two sons: Cyrus and Desmonde"))
    assert any(r.get("rel_type") == "child_of" and not r.get("negated") for r in kin)


def test_flag_off_is_byte_parity(monkeypatch):
    """SPINE_NEGATED_NAME_BINDING OFF → the negated binding is skipped (today's behavior):
    no negated child_of row, and no aurora-flood edge at all."""
    monkeypatch.setattr(L, "SPINE_NEGATED_NAME_BINDING", False)
    rows = _as_dicts(_facts(MEASURED_CHILD))
    assert not [r for r in rows if r.get("rel_type") == "child_of" and r.get("negated")]
    assert not [r for r in rows if "aurora" in str(r.get("subject"))]
