"""Unit tests for the generic reference/identifier scalar_atomic capture (migration 185).

A cleanly-STATED alphanumeric identifier (ticket / case / docket / order / reference /
invoice number: "ABC-12345", "2024-CV-00931", "7788-AA") now has a scalar_atomic
FORMAT-GRAMMAR row (`has_reference_id`) so `_detect_atomic_values` types it — routed to a
SCALAR on the owning entity by the SAME machinery IP/email use, instead of being shredded on
its dashes by the LLM/spine deriver.

These cover the PURE pieces with ZERO infra (db_conn=None → the _BOOTSTRAP mirror of the
seeded pattern; the live per-tenant extraction_patterns row OVERRIDES it in production):
  1. the detector CLAIMS a stated identifier as has_reference_id, whole,
  2. the deterministic GATE never eats plain numbers / plain (or lowercase) words / IPs /
     MAC / email / FQDN spans,
  3. `_suppress_atomic_claimed_twins` drops a deriver twin that ate the claimed identifier
     under a wrong rel (exact-value match only, no substring heuristics).
The subject-binding (`possessor_of_possessive_attribute`) is spaCy-driven and validated on
pre-prod; a grammar-only smoke check is included but SKIPS when the spaCy model is absent.

Run: python3 -m pytest tests/test_atomic_identifier_capture.py -q
"""
import pytest

from src.api import main as m


def _ref_values(text):
    got = m._detect_atomic_values(text, None)
    return [a["value"] for a in got if a["rel_type"] == "has_reference_id"]


# ── 1. capture ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,ident", [
    ("My case number for the Capital One claim is 7788-AA", "7788-AA"),
    ("The docket number is 2024-CV-00931", "2024-CV-00931"),
    ("My ticket number is ABC-12345", "ABC-12345"),
    ("Reference INC0012345 was opened yesterday", "INC0012345"),
    ("Order REF-2024-8891 shipped", "REF-2024-8891"),
])
def test_detect_atomic_values_claims_generic_identifier(text, ident):
    """The bootstrap scalar_atomic format-grammar claims the whole identifier on has_reference_id
    (db_conn=None → bootstrap; per-tenant extraction_patterns override live)."""
    assert ident in _ref_values(text), m._detect_atomic_values(text, None)


# ── 2. the deterministic gate — never eat plain numbers / words / structured atomics ──
@pytest.mark.parametrize("text", [
    "I have 3 cats and 12345 things",          # plain numbers → no letter
    "My ticket and docket are open",           # plain words → no digit
    "The refund is 45 dollars",                # bare count
    "utf8 and win10 and iPhone13 and mp3",     # lowercase / <2-digit tech tokens
    "My IP is 192.168.40.20",                  # IPv4 (no uppercase)
    "The MAC is AA-BB-CC-DD-EE-01",            # uppercase hyphen-MAC → yields to has_mac
    "Email me at bob@example.com",             # email span
    "My server is host.example.com",           # FQDN span
])
def test_gate_does_not_capture_non_identifiers(text):
    """No has_reference_id edge for plain numbers, plain/lowercase words, or structured atomics
    (IP/MAC/email/FQDN) — the case-sensitive uppercase+>=2-digit gate + boundary guards."""
    assert _ref_values(text) == [], m._detect_atomic_values(text, None)


def test_mac_still_typed_as_mac_not_identifier():
    """The uppercase hyphen-MAC is claimed by has_mac (its own lane), NOT has_reference_id."""
    got = m._detect_atomic_values("The MAC is AA-BB-CC-DD-EE-01", None)
    rels = {a["rel_type"] for a in got}
    assert "has_reference_id" not in rels
    assert "has_mac" in rels, got


def test_ip_still_typed_as_ip_not_identifier():
    """Existing IP capture is unaffected — the identifier gate requires an uppercase letter."""
    got = m._detect_atomic_values("The router ip is 172.16.5.9", None)
    assert any(a["rel_type"] == "has_ip" and a["value"] == "172.16.5.9" for a in got), got
    assert not any(a["rel_type"] == "has_reference_id" for a in got), got


# ── 3. twin suppression (exact value, no substring) ──────────────────────────
def test_suppress_atomic_claimed_twins_drops_identifier_twin():
    """A deriver twin that ate the claimed identifier under a WRONG rel is suppressed; the
    unrelated ownership edge is kept."""
    atomic = m._detect_atomic_values("My ticket number is ABC-12345", None)
    edges = [
        {"subject": "user", "rel_type": "owns", "object": "ABC-12345"},
        {"subject": "user", "rel_type": "owns", "object": "ticket"},
    ]
    kept, dropped = m._suppress_atomic_claimed_twins(edges, atomic)
    assert dropped == 1
    assert {"subject": "user", "rel_type": "owns", "object": "ticket"} in kept
    assert not any((e.get("object") or "") == "ABC-12345" for e in kept)


def test_suppress_keeps_correct_reference_capture():
    """An edge already carrying has_reference_id for the value is a CORRECT capture — never dropped."""
    atomic = m._detect_atomic_values("My case number is 7788-AA", None)
    edges = [{"subject": "user", "rel_type": "has_reference_id", "object": "7788-AA"}]
    kept, dropped = m._suppress_atomic_claimed_twins(edges, atomic)
    assert dropped == 0 and kept == edges


# ── possessive-attribute subject bind (grammar-only; skip without spaCy) ──────
def test_possessive_attribute_binds_identifier_to_user():
    """"My case number is 7788-AA" binds the identifier to the user (grammatical possessor),
    so the typed scalar is anchored ON the querying entity for the dumb walk. Requires spaCy."""
    from src.extraction import linguistics as L
    if L._parse("My case number is 7788-AA") is None:
        pytest.skip("spaCy model unavailable — possessive binder is a no-op fail-safe")
    assert L.possessor_of_possessive_attribute("My case number is 7788-AA", "7788-AA") == "user"


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT-SIGNALLED capture (migration 186 + spine _chain_identifier_context).
# A stated identifier WITH an identifier-context head noun ("ticket number is …")
# is captured as has_reference_id REGARDLESS of value shape — incl. a BARE NUMBER
# the value-shape atomic (mig 185) intentionally excludes. Cue class resolves via
# the DB-DOWN bootstrap floor when POSTGRES_DSN is unset, so this is DB-free.
# Requires the spaCy model (the deriver); skips cleanly without it.
# ══════════════════════════════════════════════════════════════════════════════
import importlib
import os
from datetime import datetime as _dtm

import src.extraction.linguistics as _ling

_ling = importlib.reload(_ling)  # ensure the module (with the new chain) is loaded fresh
_HAS_SPACY = _ling.linguistics_available()
requires_spacy = pytest.mark.skipif(not _HAS_SPACY, reason="en_core_web_sm not installed")
_REF = _dtm(2026, 7, 17)


def _triples(text):
    return [(f.subject, f.rel_type, f.object) for f in _ling.derive_sentence_facts(text, _REF)]


def _ref_ids(text):
    return [(s, o) for (s, r, o) in _triples(text) if r == "has_reference_id"]


@requires_spacy
@pytest.mark.parametrize("text,value", [
    ("my ticket number is 1234567", "1234567"),        # PURE NUMERIC — the reported drop
    ("my case number is 8891002", "8891002"),
    ("the docket number is 2024-CV-00931", "2024-cv-00931"),   # definite article → implicit user
    ("the confirmation number is 55021", "55021"),
    ("my order number is 55021", "55021"),
    ("my policy number is PN-99812", "pn-99812"),
])
def test_context_signalled_identifier_captured_as_reference_id(text, value):
    """A stated identifier with an identifier-context noun → (user, has_reference_id, <verbatim>),
    even a pure number a value-shape gate cannot claim. Value kept verbatim (lowercased by _emit)."""
    ids = _ref_ids(text)
    assert ("user", value) in ids, _triples(text)
    # never mis-read the number as an age.
    assert not any(r == "age" for (_s, r, _o) in _triples(text)), _triples(text)


@requires_spacy
def test_context_bare_number_not_captured_without_strong_cue():
    """'my favorite number is 7' — no STRONG identifier cue ('favorite'/'number' insufficient) →
    NOT captured as has_reference_id (it is a preference/value, not an identifier)."""
    assert _ref_ids("my favorite number is 7") == [], _triples("my favorite number is 7")


@requires_spacy
def test_context_lucky_number_not_captured():
    assert _ref_ids("my lucky number is 13") == [], _triples("my lucky number is 13")


@requires_spacy
def test_context_plain_count_not_captured():
    """A plain count ('I have 3 cats') has no identifier context → no has_reference_id."""
    assert _ref_ids("I have 3 cats") == [], _triples("I have 3 cats")


@requires_spacy
def test_context_alphanumeric_id_single_reference_edge_no_double_capture():
    """An alphanumeric id in an identifier-context sentence yields exactly ONE has_reference_id edge
    from the deriver (the value-shape atomic in the /harvest lane collapses in the (subj,rel,obj)
    dedup — here we assert the deriver alone does not double-emit)."""
    ids = _ref_ids("my ticket number is ABC-12345")
    assert ids.count(("user", "abc-12345")) == 1, _triples("my ticket number is ABC-12345")


@requires_spacy
def test_context_phone_number_not_hijacked_to_reference_id():
    """'my phone number is 519-555-0123' — 'phone' is NOT an identifier_noun cue, so the context
    chain does NOT fire; the value routes to has_phone via the atomic lane (asserted separately).
    Here: the deriver emits NO has_reference_id for it."""
    assert _ref_ids("my phone number is 519-555-0123") == [], _triples("my phone number is 519-555-0123")
    # And the value-shape atomic still types it as a phone (its own lane, not has_reference_id):
    got = m._detect_atomic_values("my phone number is 519-555-0123", None)
    assert any(a["rel_type"] == "has_phone" for a in got), got


@requires_spacy
def test_context_employee_id_is_scalar_string():
    """'my employee id is 4471' — 'id' is a STRONG cue → has_reference_id string scalar on user
    (was a novel employee_id rel). The value is a string scalar, never an age."""
    facts = _ling.derive_sentence_facts("my employee id is 4471", _REF)
    assert any(f.scalar_datatype == "string" and f.object == "4471" for f in facts), \
        [(f.subject, f.rel_type, f.object) for f in facts]
    assert not any(f.rel_type == "age" for f in facts)


@requires_spacy
def test_context_account_balance_not_captured():
    """'my account balance is 500' — 'account' is strong but the HEAD is 'balance' (a monetary value,
    not an identifier) → NOT captured as has_reference_id (head-noun semantics decide)."""
    assert _ref_ids("my account balance is 500") == [], _triples("my account balance is 500")


@requires_spacy
def test_context_strong_head_alone_captured():
    """'my case number is 8891002' already covered; here the STRONG head alone ('my reference is …')."""
    assert ("user", "ref-2024-8891") in _ref_ids("my reference is REF-2024-8891"), \
        _triples("my reference is REF-2024-8891")


# ══════════════════════════════════════════════════════════════════════════════
# MEASURE / COUNT GUARD (skeptical-review tightening).
# A STRONG identifier cue as the copula subject head ("my ticket/order/policy/…")
# followed by a QUANTITY value ("5 dollars", "20 years", "3 items", "20-30 years")
# must NOT be captured as has_reference_id: a count/measure has its digits in a
# nummod/quantmod modifier on a DIGIT-LESS head noun — not in an identifier token.
# Deterministic + structural (spaCy dep/POS), no unit/currency word list. Biases
# toward NOT capturing an ambiguous quantity — a missed id is recoverable, a memory
# corrupted with a bogus identifier is not.
# ══════════════════════════════════════════════════════════════════════════════
@requires_spacy
@pytest.mark.parametrize("text", [
    "my ticket is 5 dollars",             # currency price, not an id
    "the invoice is 45 dollars",          # currency price (definite subject)
    "my order is 3 items",                # a count
    "my policy is 20 years",              # a duration measure
    "my account is 500 dollars overdrawn",  # currency + describer
    "my policy is 20-30 years",           # a RANGE measure (leading quantmod half)
])
def test_context_measure_value_not_captured_as_reference_id(text):
    """A strong identifier cue + a QUANTITY value (count/currency/duration/range) → no
    has_reference_id. The value's digits live in a nummod/quantmod modifier on a digit-less head
    noun (the measured thing), which the deriver distinguishes from an identifier token."""
    assert _ref_ids(text) == [], _triples(text)


@requires_spacy
def test_context_bare_number_id_survives_measure_guard():
    """The measure guard must NOT block a genuine bare-number id: 'my order number is 5' (comp is a
    bare NUM, not a measured noun) still captures has_reference_id."""
    assert ("user", "5") in _ref_ids("my order number is 5"), _triples("my order number is 5")
