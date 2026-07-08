"""Unit tests for the spine atomic-scalar claim (2026-07-06 fix #1a seam helpers).

The spine harvest (`_harvest_via_sentence_pipeline`) now claims structured atomic scalars
pre-return: `_detect_atomic_values` (the seeded scalar_atomic format-grammar) proposes the
values, `_suppress_atomic_claimed_twins` drops any deriver edge whose OBJECT is EXACTLY a
claimed value (unless the edge already carries the detector's own rel_type), and the existing
`_classify_value_subject_residue` binder unions the typed has_* edges. These tests cover the
two PURE pieces (detector + suppression); the LLM subject binder and the live wiring are
validated on pre-prod against DB ground truth.

Run: python3 -m pytest tests/test_spine_atomic_claim.py -q
"""
from src.api import main as m


def test_detect_atomic_values_claims_ip_on_spine_text():
    """The bootstrap scalar_atomic format-grammar claims the dotted quad on the repro text
    (db_conn=None → bootstrap patterns; per-tenant extraction_patterns override live)."""
    got = m._detect_atomic_values("The edge firewall's IP is 172.16.5.9", None)
    ips = [a for a in got if a["rel_type"] == "has_ip"]
    assert ips and ips[0]["value"] == "172.16.5.9", got


def test_suppress_atomic_claimed_twins_drops_wrong_rel_twin():
    """A deriver twin that ate the claimed value under the WRONG rel (the live age corruption)
    is suppressed; unrelated edges are untouched."""
    atomic = m._detect_atomic_values("The edge firewall's IP is 172.16.5.9", None)
    edges = [
        {"subject": "firewall", "rel_type": "age", "object": "172.16.5.9"},
        {"subject": "user", "rel_type": "owns", "object": "firewall"},
    ]
    kept, dropped = m._suppress_atomic_claimed_twins(edges, atomic)
    assert dropped == 1
    assert {"subject": "user", "rel_type": "owns", "object": "firewall"} in kept
    assert not any((e.get("object") or "") == "172.16.5.9" for e in kept)


def test_suppress_atomic_claimed_twins_keeps_correct_capture():
    """An edge already carrying the detector's OWN rel_type for the value is a CORRECT capture
    and must never be dropped (exact value + rel match, no substring heuristics)."""
    atomic = m._detect_atomic_values("the router ip is 10.0.0.7", None)
    edges = [{"subject": "router", "rel_type": "has_ip", "object": "10.0.0.7"}]
    kept, dropped = m._suppress_atomic_claimed_twins(edges, atomic)
    assert dropped == 0 and kept == edges


def test_suppress_atomic_claimed_twins_exact_match_only():
    """Exact-normalized-value comparison ONLY: an object merely CONTAINING the value (a wider
    smeared span) is NOT suppressed — no substring matching (hard constraint #2)."""
    atomic = m._detect_atomic_values("my nas is a unifi at 192.168.1.9", None)
    edges = [{"subject": "user", "rel_type": "owns", "object": "unifi at 192.168.1.9"}]
    kept, dropped = m._suppress_atomic_claimed_twins(edges, atomic)
    assert dropped == 0 and kept == edges
