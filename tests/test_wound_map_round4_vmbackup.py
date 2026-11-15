"""THE WOUND-MAP ROUND-4 VM-BACKUP PINS (2026-11-13, the blended-family smoke's
vm-backup recall miss, live seat / image deploy-9c9501e2).

Input turn (verbatim): "The homelab hypervisor is Proxmox running on the r730; the
Plex VM is vmid 112 and it backs up nightly to the NAS at 2am."

Measured store wounds: no Plex VM entity / vmid 112 attribute / backup-schedule row;
the second conjunct's pronoun subject bound the nearest preceding PROPN ("proxmox")
→ junk (proxmox, back_to, nas) beside a schedule that reads as the hypervisor's.

The two flags this lane lands (both default ON; OFF = byte-for-byte legacy output):
  • SPINE_BACKUP_TO_TIER — the closed backup-verb-class frame whose VALUE is the
    to-PP-bearing cadence+clock span bounded at the clock ("nightly to the NAS at
    2am" — the sentence's trailing period never rides in), with a tier-0 subject
    hint on the clause's own nsubj.
  • SPINE_CONJUNCT_SUBJECT_ANAPHORA — a THING-pronoun subject of a CONJUNCT resolves
    to the SAME sentence's determined FIRST-CONJUNCT subject ("the Plex VM") before
    the cross-sentence recency race can hand it to "proxmox".

Same discipline as tests/test_wound_map_round3_pins.py: hermetic deriver pins via
L.derive_sentence_facts / L.detect_instruction_settings with SPACY_MODEL=en_core_web_sm.
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.extraction.linguistics as L  # noqa: E402

pytestmark = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (en_core_web_sm not installed) — "
           "spine deriver no-ops; executes wherever the model loads",
)

_REF = datetime.datetime(2026, 11, 13)

# The measured turn, verbatim (first conjunct + ';' + the backup clause).
MISSION_TURN = ("The homelab hypervisor is Proxmox running on the r730; the Plex VM "
                "is vmid 112 and it backs up nightly to the NAS at 2am.")
# The backup clause alone (the atom the live sentence pipeline can split out).
BACKUP_CONJUNCT = ("The Plex VM is vmid 112 and it backs up nightly to the NAS "
                   "at 2am.")


def _triples(text, ref=_REF):
    doc = L._parse(text)
    return [(f.subject, f.rel_type, str(f.object)) for f in
            (L.derive_sentence_facts(doc, ref) or [])]


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND — the missing backup-schedule row + the pronoun mis-bind (SPINE_BACKUP_TO_TIER
# + SPINE_CONJUNCT_SUBJECT_ANAPHORA)
# ══════════════════════════════════════════════════════════════════════════════════

def test_vmbackup_exact_turn_mints_the_schedule_row():
    """The measured turn mints (plex vm, has_setting, 'nightly to the NAS at 2am') —
    the value carries the destination PP and the clock, never the trailing period
    (the gold-match is any serving containing '2am')."""
    triples = _triples(MISSION_TURN)
    sched = [(s, v) for (s, r, v) in triples
             if r == L.INSTRUCTION_REGISTER_REL and "2am" in v]
    assert sched, triples
    assert any(s == "plex vm" and v == "nightly to the NAS at 2am"
               for (s, v) in sched), sched


def test_vmbackup_identifier_and_entity_land():
    """'the Plex VM is vmid 112' mints the Plex VM entity with its vmid attribute —
    the subject the schedule row binds to EXISTS (wound (c) closed)."""
    triples = _triples(BACKUP_CONJUNCT)
    assert ("plex vm", "vmid", "112") in triples, triples
    assert any(s == "plex vm" for (s, _, _) in triples), triples


def test_vmbackup_wrong_subject_negative_never_proxmox_never_it():
    """The wrong-subject negative: no back/has_setting row binds to 'proxmox' (the
    nearest preceding PROPN the recency race handed the pronoun to on the live seat)
    and none islands on the literal pronoun 'it'."""
    for sent in (MISSION_TURN, BACKUP_CONJUNCT):
        triples = _triples(sent)
        subjects = {s for (s, _, _) in triples}
        assert "proxmox" not in subjects, triples
        assert "it" not in subjects, triples
        # the backup predicate rows land on the coordination-local subject
        backs = [(s, r, v) for (s, r, v) in triples if r in ("back", "back_to")]
        assert backs and all(s == "plex vm" for (s, _, _) in backs), triples


def test_vmbackup_variant_weekly_offsite_repo():
    """The variant shape: a different cadence + destination + clock under another
    backup-class verb (snapshots), same closed frame, same subject binding."""
    sent = ("The git server is vmid 7 and it snapshots weekly to the offsite repo "
            "at 11pm.")
    triples = _triples(sent)
    sched = [(s, v) for (s, r, v) in triples
             if r == L.INSTRUCTION_REGISTER_REL and "11pm" in v]
    assert any(s == "git server" and v == "weekly to the offsite repo at 11pm"
               for (s, v) in sched), triples
    assert not any(s in ("it", "vmid") for (s, _, _) in triples), triples


def test_vmbackup_variant_definite_subject_no_coordination():
    """A definite-NP subject with no pronoun at all: the tier-0 hint reads the
    clause's own nsubj directly."""
    sent = "The plex VM backs up weekly to the offsite repo at 11pm."
    triples = _triples(sent)
    sched = [(s, v) for (s, r, v) in triples
             if r == L.INSTRUCTION_REGISTER_REL and "11pm" in v]
    assert any(s == "plex vm" and v == "weekly to the offsite repo at 11pm"
               for (s, v) in sched), triples


def test_vmbackup_indefinite_first_conjunct_declines_the_bind():
    """GUARD: the coordination-local binding fires only when the first-conjunct
    subject is DETERMINED — an indefinite article ('a laptop') declines, and the
    predicate rows keep today's legacy subject shape (never a guessed bind)."""
    sent = "A laptop is on the desk and it backs up weekly to the cloud at 3am."
    triples = _triples(sent)
    subjects = {s for (s, _, _) in triples}
    # no bind to an invented 'laptop' surface via the anaphora (the register's own
    # ladder may still pair the value with a leftward candidate — legacy behaviour)
    assert not any(s == "laptop" and r in ("back", "back_to")
                   for (s, r, _) in triples), triples


# ══════════════════════════════════════════════════════════════════════════════════
# THE VERB-PP GUARD NEGATIVES (the frame's closed backup-verb class)
# ══════════════════════════════════════════════════════════════════════════════════

def test_vmbackup_sends_it_back_to_vendor_never_mints_a_setting():
    """'sends it back to the vendor' is NOT a backup verb — no has_setting row mints,
    under both flag states (the pronoun there is an OBJECT, never a subject bind)."""
    sent = "He sends it back to the vendor."
    for state in (True, False):
        L.SPINE_BACKUP_TO_TIER = state
        L.SPINE_CONJUNCT_SUBJECT_ANAPHORA = state
        try:
            triples = _triples(sent)
            assert not any(r == L.INSTRUCTION_REGISTER_REL
                           for (_, r, _) in triples), (state, triples)
            subjects = {s for (s, _, _) in triples}
            assert "vendor" not in subjects or all(
                r != L.INSTRUCTION_REGISTER_REL for (_, r, _) in triples), triples
        finally:
            L.SPINE_BACKUP_TO_TIER = True
            L.SPINE_CONJUNCT_SUBJECT_ANAPHORA = True


def test_vmbackup_frame_regex_verb_gate():
    """The frame's OWN surface carries the verb gate: a cadence+clock span under a
    non-backup verb never enters the frame even when the lexicals align."""
    rx = L._SETTING_FRAME_RX["sched-backup-to"]
    assert rx.search("it backs up nightly to the NAS at 2am")
    assert rx.search("it snapshots weekly to the offsite repo at 11pm")
    assert rx.search("the exporter syncs daily at 02:30")
    assert not rx.search("he sends it back to the vendor weekly at 9am")
    assert not rx.search("she mails it back to the vendor monthly at 8am")
    # the clock bound: the trailing period never rides into the value group
    m = rx.search("it backs up nightly to the NAS at 2am.")
    assert m.group(1).rstrip().endswith("2am"), m.group(1)


# ══════════════════════════════════════════════════════════════════════════════════
# THE OFF-STATE BYTE-PARITY LEGS (each flag OFF → byte-for-byte legacy output)
# ══════════════════════════════════════════════════════════════════════════════════

def _with_flags(**flags):
    """Flip module flags for the duration of one derive; restore afterwards."""
    saved = {k: getattr(L, k) for k in flags}
    for k, v in flags.items():
        setattr(L, k, v)

    class _Restore:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            for k, v in saved.items():
                setattr(L, k, v)
    return _Restore()


def test_vmbackup_flags_off_reproduce_legacy_shapes():
    """Both flags OFF → the measured legacy output byte-for-byte: the predicate rows
    island on the literal 'it', and the register value swallows the trailing period
    (the generic sched-cadence span, unbounded)."""
    with _with_flags(SPINE_BACKUP_TO_TIER=False,
                     SPINE_CONJUNCT_SUBJECT_ANAPHORA=False):
        triples = _triples(MISSION_TURN)
    assert ("it", "back", "2am") in triples, triples
    assert ("it", "back_to", "nas") in triples, triples
    sched = [v for (s, r, v) in triples
             if r == L.INSTRUCTION_REGISTER_REL and "2am" in v]
    assert sched == ["nightly to the NAS at 2am."], triples


def test_vmbackup_flag_off_ab_battery_byte_identical():
    """The A/B battery: sentences OUTSIDE the new frames are byte-identical with the
    flags ON vs OFF (the flags only widen the backup-shape captures)."""
    battery = [
        "The homelab hypervisor is Proxmox running on the r730.",
        "Our passport numbers expire in August 2027.",
        "The kids are with me every other weekend and with Isabelle on weekdays.",
        "I always run the smoke suite before a deploy, never after.",
        "Backups kick off at 02:30 on the lab host.",
        "She carries an EpiPen and he is in grade 4.",
        "My commute takes 45 minutes and the movie lasts two hours.",
        "The migration freeze is the first Monday of every month.",
        "He sends it back to the vendor.",
        "The TLS certificate for bramble.internal expires March 3.",
    ]
    for sent in battery:
        with _with_flags(SPINE_BACKUP_TO_TIER=True,
                         SPINE_CONJUNCT_SUBJECT_ANAPHORA=True):
            on = _triples(sent)
        with _with_flags(SPINE_BACKUP_TO_TIER=False,
                         SPINE_CONJUNCT_SUBJECT_ANAPHORA=False):
            off = _triples(sent)
        assert on == off, (sent, on, off)


def test_vmbackup_flags_off_register_detector_legacy():
    """detect_instruction_settings with both flags OFF → the legacy register rows
    byte-for-byte on the backup clause (period-bearing sched-cadence span)."""
    with _with_flags(SPINE_BACKUP_TO_TIER=False,
                     SPINE_CONJUNCT_SUBJECT_ANAPHORA=False):
        rows = list(L.detect_instruction_settings(BACKUP_CONJUNCT, L._parse(BACKUP_CONJUNCT)))
    assert any(subject == "plex vm" and value == "nightly to the NAS at 2am."
               for (subject, value, _s, _e, _rel) in rows), rows
    with _with_flags(SPINE_BACKUP_TO_TIER=True,
                     SPINE_CONJUNCT_SUBJECT_ANAPHORA=True):
        rows = list(L.detect_instruction_settings(BACKUP_CONJUNCT, L._parse(BACKUP_CONJUNCT)))
    assert any(subject == "plex vm" and value == "nightly to the NAS at 2am"
               for (subject, value, _s, _e, _rel) in rows), rows
