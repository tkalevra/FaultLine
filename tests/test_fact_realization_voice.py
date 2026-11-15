"""
Tests for DESIGN-fact-realization-and-voice.

Covers:
  - Perspective: the querying user's own identity slots render as "you";
    every other entity renders by its PREFERRED name (dead-name prevention).
  - Perspective is a GRAPH/UUID decision (anchor + user anchor + same_as),
    not a name-string match — a third party sharing the user's name is NOT
    collapsed to "you".
  - Stance (confidence-as-voice): Class C facts are not labeled "[staged]" in
    the prose string.
  - Back-compat: convert_to_prose without anchor falls back to plain name
    resolution (no perspective rewriting).
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from src.api.main import (
    convert_to_prose,
    _build_identity_set,
    _resolve_perspective_name,
    _you_agreement_fixup,
    _negate_prose,
)


class TestNegateProse:
    """ASSERTION POLARITY (Q1): the deterministic prose negation transform — a negated state must
    read back NEGATED, never as its positive opposite. Presentation-only; reads the polarity column."""

    def test_copula_state_inserts_not(self):
        assert _negate_prose("Your GPS is in state functioning") == \
            "Your GPS is not in state functioning"

    def test_second_person_are(self):
        assert _negate_prose("You are in state down") == "You are not in state down"

    def test_past_copula(self):
        assert _negate_prose("the server was in state up") == "the server was not in state up"

    def test_idempotent_when_already_negated(self):
        # A double-call (or a template already carrying "not") must NOT double-negate.
        assert _negate_prose("X is not in state Y") == "X is not in state Y"

    def test_no_aux_falls_back_to_clause_negation(self):
        # No copula/aux → clause-level negation, never the bare positive.
        out = _negate_prose("Fraggle barks")
        assert "not" in out and out != "Fraggle barks"

    def test_empty_safe(self):
        assert _negate_prose("") == ""


class TestYouAgreementFixup:
    """Minimal 2nd-person agreement fixup for the 3p-template fallback (no 2p row)."""

    def test_yous_to_your(self):
        assert _you_agreement_fixup("you's preferred name is Chris") == "your preferred name is Chris"

    def test_you_is_to_you_are(self):
        assert _you_agreement_fixup("you is the parent of Cyrus") == "you are the parent of Cyrus"

    def test_you_has_to_you_have(self):
        assert _you_agreement_fixup("you has a pet that is Fraggle") == "you have a pet that is Fraggle"

    def test_you_was_to_you_were(self):
        assert _you_agreement_fixup("you was born in Guelph") == "you were born in Guelph"

    def test_no_fuzzy_s_stripping(self):
        # The fixup must NOT mangle "you likes"/"you runs" — only the explicit set.
        assert _you_agreement_fixup("you likes Art") == "you likes Art"

    def test_empty_safe(self):
        assert _you_agreement_fixup("") == ""


def _mock_db(same_as_rows=None):
    """A MagicMock db whose cursor.fetchall returns rel_type templates and
    (when _build_identity_set runs the same_as query) the provided same_as rows.

    convert_to_prose calls fetchall once for rel_type templates; _build_identity_set
    calls fetchall once for same_as rows. We disambiguate by SQL text via execute.
    """
    db = MagicMock()
    cursor = MagicMock()
    db.cursor.return_value.__enter__.return_value = cursor
    return db, cursor


class TestIdentitySet:
    def test_includes_user_but_not_query_anchor(self):
        """Identity = the canonical USER ("user" + user_id), NOT the query
        subject anchor. The anchor is "WHO are we talking about?" and is only
        sometimes the user; seeding it collapsed third-party subjects to "you"
        (the "you has a pet that is you" bug)."""
        anchor = str(uuid4())  # a THIRD-PARTY subject (e.g. the pet)
        user_id = str(uuid4())
        db, cur = _mock_db()
        cur.fetchall.return_value = []  # no same_as rows

        ident = _build_identity_set(anchor, user_id, db)

        assert "user" in ident
        assert user_id.lower() in ident
        # The query anchor is NOT the user → must NOT be in the identity set.
        assert anchor.lower() not in ident

    def test_anchor_equal_to_user_still_renders_you(self):
        """When the query IS about the user, anchor == user_id, so "you" still
        works — driven by user_id, never by the anchor seed."""
        user_id = str(uuid4())
        db, cur = _mock_db()
        cur.fetchall.return_value = []

        ident = _build_identity_set(user_id, user_id, db)

        assert user_id.lower() in ident

    def test_same_as_expansion_merges_user_uuid(self):
        """same_as expands from the USER identity (user_id), not the anchor."""
        user_id = str(uuid4())
        ghost = str(uuid4())
        anchor = str(uuid4())  # unrelated third-party subject
        db, cur = _mock_db()
        # same_as row joining user_id <-> ghost
        cur.fetchall.return_value = [(user_id, ghost)]

        ident = _build_identity_set(anchor, user_id, db)

        assert user_id.lower() in ident
        assert ghost.lower() in ident  # merged via graph from the USER, not name
        assert anchor.lower() not in ident  # anchor is never an identity seed

    def test_same_as_query_failure_degrades_to_user(self):
        user_id = str(uuid4())
        db, cur = _mock_db()
        cur.execute.side_effect = Exception("db down")

        ident = _build_identity_set(str(uuid4()), user_id, db)

        # Still returns the user-based set (fail-loud but non-crashing)
        assert user_id.lower() in ident
        assert "user" in ident

    def test_pet_anchor_object_not_collapsed_to_you(self):
        """Regression for "you has a pet that is you": when the query anchors on
        the pet (resolve_anchor → pet UUID), the pet in the has_pet OBJECT slot
        must NOT render as "you"."""
        user_id = str(uuid4())
        pet_uuid = str(uuid4())
        db, cur = _mock_db()
        cur.fetchall.return_value = []

        ident = _build_identity_set(pet_uuid, user_id, db)  # anchor == pet

        assert pet_uuid.lower() not in ident
        with patch("src.api.main.resolve_display_name", return_value="fraggle"):
            assert _resolve_perspective_name(pet_uuid, ident, db) == "fraggle"


class TestPerspectiveResolution:
    def test_user_slot_renders_you(self):
        user_uuid = str(uuid4())
        ident = {"user", user_uuid.lower()}
        db = MagicMock()

        # resolve_display_name must NOT be consulted for an identity slot
        with patch("src.api.main.resolve_display_name") as mock_resolve:
            result = _resolve_perspective_name(user_uuid, ident, db)

        assert result == "you"
        mock_resolve.assert_not_called()

    def test_anchor_literal_user_renders_you(self):
        ident = {"user"}
        db = MagicMock()
        assert _resolve_perspective_name("user", ident, db) == "you"

    def test_third_party_renders_preferred_name(self):
        other_uuid = str(uuid4())
        ident = {"user", str(uuid4()).lower()}
        db = MagicMock()

        with patch("src.api.main.resolve_display_name", return_value="Cyrus"):
            result = _resolve_perspective_name(other_uuid, ident, db)

        assert result == "Cyrus"

    def test_namesake_third_party_not_collapsed(self):
        """A third party whose preferred name equals the user's name must NOT
        become 'you' — the decision is on the UUID, not the string."""
        user_uuid = str(uuid4())
        namesake_uuid = str(uuid4())  # different UUID, same display name "chris"
        ident = {"user", user_uuid.lower()}
        db = MagicMock()

        with patch("src.api.main.resolve_display_name", return_value="chris"):
            result = _resolve_perspective_name(namesake_uuid, ident, db)

        # graph identity says this is NOT the user → keep their (preferred) name
        assert result == "chris"


# ── The rel_type templates convert_to_prose ACTUALLY reads ────────────────────────────────
# It stopped reading them from the passed cursor at `5161ee7` — it resolves them from the
# per-tenant rel_type OVERLAY (main.py:34301). The `cur.fetchall.side_effect` template rows below
# were therefore DEAD: silently ignored, so these tests asserted against whatever the DB happened
# to hold (or against a bug). Patch the REAL seam — which also makes them DB-independent.
def _meta(**rows):
    return patch("src.api.main.rel_type_overlay.resolve_current", return_value=rows)


PARENT_3P = {"natural_language": "X is the parent of Y", "natural_language_2p": None,
             "tail_types": ["Person"], "label": "parent of"}
# Both templates, matching the REAL seed (3p: migration 031; 2p: migration 081). Checked against
# the seeded metadata on purpose — a fixture invented out of thin air pins a FICTIONAL contract,
# which is how three composer tests ended up guarding a bug instead of the product.
PARENT = dict(PARENT_3P, natural_language_2p="You are the parent of Y")
LIKES = {"natural_language": "X likes Y", "natural_language_2p": "You like Y",
         "tail_types": ["ANY"], "label": "likes"}
PREF = {"natural_language": "X's preferred name is Y", "natural_language_2p": "You go by Y",
        "tail_types": ["SCALAR"], "label": "preferred name"}
PREF_NO_2P = dict(PREF, natural_language_2p=None)


class TestTemplateDocHintLeak:
    """A maintainer DOC-HINT in a seed template must NEVER reach the reader.

    THIS BUG SHIPPED, and nothing in the suite covered it — the whole prose surface was green
    while users read:
        "Diane lives in Toronto (residence)"      "Diane was born on 1980-04-02 (date)"
    Four seeds carry these hints (lives_in/lives_at/born_on/instance_of). They are written for
    US, and they rendered VERBATIM straight through the convert_to_prose contract ("NO rel_type
    tokens leak") — the same class of leak as the rel_type LABEL doc-comment
    ("…(skos:altLabel; use pref_name…)").
    """

    def _render(self, rel, t3, t2, subj, obj):
        u, o = str(uuid4()), str(uuid4())
        fact = {"subject_id": u, "rel_type": rel, "object_id": o, "fact_class": "A"}
        db, cur = _mock_db()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = None
        meta = {rel: {"natural_language": t3, "natural_language_2p": t2,
                      "tail_types": ["Location"], "label": rel}}
        with _meta(**meta), \
                patch("src.api.main.resolve_display_name", side_effect=[subj, obj]):
            return convert_to_prose([fact], db)

    def test_3p_template_hint_never_reaches_the_reader(self):
        out = self._render("lives_in", "X lives in Y (residence)", "You live in Y",
                           "Diane", "Toronto")
        assert out == ["Diane lives in Toronto"]
        assert "(residence)" not in out[0], "maintainer doc-hint leaked into user-facing prose"

    def test_3p_date_hint_never_reaches_the_reader(self):
        out = self._render("born_on", "X was born on Y (date)", "You were born on Y",
                           "Diane", "1980-04-02")
        assert out == ["Diane was born on 1980-04-02"]
        assert "(date)" not in out[0]

    def test_strip_is_failsafe_and_value_safe(self):
        # A value containing parentheses must survive — the strip runs BEFORE substitution.
        from src.api.main import _strip_template_hint
        assert _strip_template_hint("X lives in Y (residence)") == "X lives in Y"
        assert _strip_template_hint("(only a hint)") == "(only a hint)"  # would empty -> keep
        assert _strip_template_hint(None) is None


class TestConvertToProsePerspective:
    def test_self_subject_renders_you(self):
        user_uuid = str(uuid4())
        child_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "parent_of",
            "object_id": child_uuid,
            "fact_class": "A",
        }
        db, cur = _mock_db()
        # convert_to_prose calls _build_identity_set FIRST (same_as fetchall),
        # then the rel_type templates fetchall.
        cur.fetchall.side_effect = [
            [],  # same_as expansion (none)
            [("parent_of", "X is the parent of Y")],  # templates
        ]

        with _meta(parent_of=PARENT_3P), \
                patch("src.api.main.resolve_display_name", return_value="Cyrus"):
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)

        # The template row here is 3p-only (no natural_language_2p), so this exercises
        # the null-fallback agreement fixup: "you is" → "you are" (migration 081 adds
        # the real 2p template "You are the parent of Y"; this guards un-backfilled rows).
        assert prose == ["you are the parent of Cyrus"]

    def test_no_anchor_falls_back_to_names(self):
        user_uuid = str(uuid4())
        child_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "parent_of",
            "object_id": child_uuid,
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.return_value = [("parent_of", "X is the parent of Y")]

        with _meta(parent_of=PARENT_3P), \
                patch("src.api.main.resolve_display_name", side_effect=["chris", "Cyrus"]):
            prose = convert_to_prose([fact], db)  # no anchor → plain names

        # Display names are TITLECASED at render — deliberate (`c059e8a`: "render it like a
        # person"). This test pinned the pre-c059e8a lowercase output; the product is right.
        assert prose == ["Chris is the parent of Cyrus"]

    def test_class_c_no_staged_label(self):
        user_uuid = str(uuid4())
        obj_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "likes",
            "object_id": obj_uuid,
            "fact_class": "C",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [
            [],  # same_as
            [("likes", "X likes Y")],  # templates
        ]

        with _meta(likes=LIKES), \
                patch("src.api.main.resolve_display_name", return_value="Art"):
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)

        # THE TEST'S ACTUAL INTENT: a Class-C fact must NOT be labelled "[staged]" in the prose
        # (tiering is carried by the recall VOICE, never printed as a token). That still holds.
        # The prose itself got BETTER: `likes` has a seeded 2p template ("You like Y"), so the
        # user now reads grammatical "You like Art" instead of the old 3p-fallback "you likes Art".
        # Pinning the ungrammatical string would have been pinning a defect.
        assert "[staged]" not in prose[0]
        assert prose == ["You like Art"]
        assert "[staged]" not in prose[0]

    def test_self_subject_uses_2p_template(self):
        """When subject renders 'you' AND a natural_language_2p exists, use it:
        subject baked in, only the object substituted into Y."""
        user_uuid = str(uuid4())
        child_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "parent_of",
            "object_id": child_uuid,
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [[]]  # same_as only

        # Templates come from the rel_type OVERLAY, not this cursor (since 5161ee7). Injecting
        # them via cur.fetchall fed a DEAD SEAM: no template resolved, the composer fell through
        # to the bare "you parent_of Cyrus" join, and the test read as a 2p regression that was
        # never there. Inject where the code actually LOOKS.
        with _meta(parent_of=PARENT), \
                patch("src.api.main.resolve_display_name", return_value="Cyrus"):
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)

        assert prose == ["You are the parent of Cyrus"]

    def test_pref_name_2p_template(self):
        """pref_name 2p form: 'X's preferred name is Y' → 'You go by Y'."""
        user_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "pref_name",
            "object": "Chris",
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [
            [],  # same_as
            [("pref_name", "X's preferred name is Y", "You go by Y")],
        ]
        with _meta(pref_name=PREF), \
                patch("src.api.main.resolve_display_name", return_value="Chris"):
            prose = convert_to_prose(
                [fact], db, anchor=user_uuid, user_id=user_uuid,
                preferred_alias_map={user_uuid: "Chris"},
            )
        assert prose == ["You go by Chris"]

    def test_non_you_subject_keeps_3p_template(self):
        """A third-party subject (not the querying user) keeps the 3p template
        even when a 2p template exists for the rel_type."""
        user_uuid = str(uuid4())
        other_uuid = str(uuid4())
        child_uuid = str(uuid4())
        fact = {
            "subject_id": other_uuid,
            "rel_type": "parent_of",
            "object_id": child_uuid,
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [[]]  # same_as (user identity set = just the user)

        # A 2p template EXISTS for parent_of — the point of this test is that a third-party
        # subject must not steal it. Injected via the overlay (the live seam), so the guard is
        # actually exercised instead of silently falling back to a bare join.
        with _meta(parent_of=PARENT), \
                patch("src.api.main.resolve_display_name", side_effect=["Marla", "Cyrus"]):
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)
        assert prose == ["Marla is the parent of Cyrus"]

    def test_object_side_you_stays_3p(self):
        """Object-side 'you' is already grammatical on the 3p template — only the
        SUBJECT being 'you' switches to 2p. Subject here is a third party."""
        user_uuid = str(uuid4())
        other_uuid = str(uuid4())
        fact = {
            "subject_id": other_uuid,
            "rel_type": "parent_of",
            "object_id": user_uuid,  # object IS the user → "you"
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [[]]  # same_as

        with _meta(parent_of=PARENT), \
                patch("src.api.main.resolve_display_name", return_value="Marla"):
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)
        # Subject=Marla (3p), object resolves to "you" → grammatical 3p sentence.
        assert prose == ["Marla is the parent of you"]

    def test_null_2p_falls_back_with_agreement_fixup(self):
        """Legacy/un-backfilled row: natural_language_2p IS NULL → 3p fallback with
        minimal agreement fixup so no 'you is' / 'you's' leaks."""
        user_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "pref_name",
            "object": "Chris",
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [
            [],  # same_as
            [("pref_name", "X's preferred name is Y", None)],  # 2p is NULL
        ]
        # ⚠️ This test used to pass FOR THE WRONG REASON: its mocked template row was dead, and
        # the string it asserted was produced by the Shape-4 CLOBBER BUG, not by the agreement
        # fixup it claims to guard. Patching the real seam makes it exercise the fixup for real.
        with _meta(pref_name=PREF_NO_2P), \
                patch("src.api.main.resolve_display_name", return_value="Chris"):
            prose = convert_to_prose(
                [fact], db, anchor=user_uuid, user_id=user_uuid,
                preferred_alias_map={user_uuid: "Chris"},
            )
        # "you's preferred name is Chris" → fixup → "your preferred name is Chris"
        assert prose == ["your preferred name is Chris"]
        assert "you's" not in prose[0]

    def test_placeholder_broken_template_falls_back_to_label(self):
        """PLACEHOLDER-VALIDITY GUARD: a grown natural_language template that baked the
        slot WORDS in instead of the X/Y placeholders (e.g. participated_in →
        "You participated in unknown" / "unknown participated in unknown") cannot carry
        the object — it silently rendered "…participated in unknown" even though the
        object alias resolved fine. The render must DETECT the missing placeholder
        STRUCTURALLY and demote to the neutral "X {label} Y" fallback so the resolved
        object alias actually appears. This is the temporal-ordered/comparison "unknown"
        bug (the dated occurrence object dropped to 'unknown'). Subject-agnostic:
        keyed on placeholder absence, NEVER on the literal word 'unknown'.
        """
        user_uuid = str(uuid4())
        occ_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "participated_in",
            "object_id": occ_uuid,
            "fact_class": "A",
            "event_date": "2023-01-10T00:00:00+00:00",
        }
        db, cur = _mock_db()
        # _build_identity_set same_as (empty) is read off the cursor.
        cur.fetchall.side_effect = [[]]
        # The corrupted template comes from the per-tenant overlay — both the 3p and the
        # 2p form baked "unknown" in place of the placeholders, exactly as observed live.
        broken_meta = {
            "participated_in": {
                "natural_language": "unknown participated in unknown",
                "natural_language_2p": "You participated in unknown",
                "label": "Participated in",
            }
        }
        with patch("src.api.main.rel_type_overlay.resolve_current",
                   return_value=broken_meta), \
             patch("src.api.main.resolve_display_name",
                   return_value="data analysis using python webinar"):
            prose = convert_to_prose(
                [fact], db, anchor=user_uuid, user_id=user_uuid,
                preferred_alias_map={occ_uuid: "data analysis using python webinar"},
            )
        # The object alias must appear — NEVER the baked "unknown".
        assert len(prose) == 1
        assert "unknown" not in prose[0].lower()
        assert "data analysis using python webinar" in prose[0]
        # event_date still appended by the dated-event branch (unchanged behavior).
        assert "(on 2023-01-10)" in prose[0]

    def test_valid_template_unchanged_by_placeholder_guard(self):
        """CONTROL: a well-formed template (real X/Y placeholders) is byte-for-byte
        unchanged by the guard — it only fires on a placeholder-missing template."""
        user_uuid = str(uuid4())
        child_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "parent_of",
            "object_id": child_uuid,
            "fact_class": "A",
        }
        db, cur = _mock_db()
        cur.fetchall.side_effect = [[]]
        good_meta = {
            "parent_of": {
                "natural_language": "X is the parent of Y",
                "natural_language_2p": "You are the parent of Y",
                "label": "Parent of",
            }
        }
        with patch("src.api.main.rel_type_overlay.resolve_current",
                   return_value=good_meta), \
             patch("src.api.main.resolve_display_name", return_value="Cyrus"):
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)
        assert prose == ["You are the parent of Cyrus"]

    def test_unbound_context_not_perspective_rewritten(self):
        """context (store_context) facts are unbound prose — no perspective."""
        user_uuid = str(uuid4())
        fact = {
            "subject_id": user_uuid,
            "rel_type": "context",
            "object_id": "some free prose about the user",
            "fact_class": "C",
        }
        db, cur = _mock_db()
        # _build_identity_set (same_as) first, then templates (none for context).
        cur.fetchall.side_effect = [
            [],  # same_as
            [],  # templates
        ]
        cur.fetchone.return_value = None  # no label row

        with patch("src.api.main.resolve_display_name") as mock_resolve:
            mock_resolve.return_value = "WHATEVER"
            prose = convert_to_prose([fact], db, anchor=user_uuid, user_id=user_uuid)

        # Subject should NOT have been resolved to "you" via perspective path;
        # the unbound guard routes through plain resolve_display_name instead.
        # (We only assert no crash + a string emitted; MCP uses the raw object.)
        assert len(prose) == 1


class TestConvertToProseOccurrenceType:
    """FIX 2 (lean-query rendering): a dated participated_in occurrence whose object has an
    (<occurrence>, instance_of, <type>) edge in the SAME fact set now renders the TYPE word the
    question keys on ("webinar"/"workshop"). Metadata-driven (reads the existing instance_of edge),
    no hardcoded type map, subject-agnostic. Fail-safe: no instance_of type → render unchanged."""

    _TEMPLATE_META = {
        "participated_in": {
            "natural_language": "X participated in Y",
            "natural_language_2p": "You participated in Y",
            "label": "participated in",
        },
        "instance_of": {
            "natural_language": "X is a Y",
            "natural_language_2p": None,
            "label": "instance of",
            # instance_of is a HIERARCHY rel (migration 022/089) — set it so the
            # structure-driven composer routes it to BARE-TYPE, never NAMED-INSTANCE.
            "is_hierarchy_rel": True,
        },
        "related_to": {
            "natural_language": "X is related to Y",
            "natural_language_2p": None,
            "label": "related to",
            "head_types": ["ANY"],
            "tail_types": ["ANY"],
        },
    }

    def _run(self, facts, user_uuid, preferred_alias_map):
        db, cur = _mock_db()
        cur.fetchall.return_value = []  # same_as identity expansion (none)
        with patch("src.api.main.rel_type_overlay.resolve_current",
                   return_value=self._TEMPLATE_META), \
             patch("src.api.main.resolve_display_name",
                   side_effect=lambda eid, _db: preferred_alias_map.get(eid, str(eid))):
            return convert_to_prose(
                facts, db, anchor=user_uuid, user_id=user_uuid,
                preferred_alias_map=preferred_alias_map,
            )

    def test_dated_occurrence_appends_instance_of_type(self):
        user_uuid = str(uuid4())
        occ_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [
            {  # the dated participation occurrence
                "subject_id": user_uuid, "rel_type": "participated_in",
                "object_id": occ_uuid, "event_date": "2026-05-03", "fact_class": "A",
            },
            {  # the instance_of edge filing the occurrence at its type (same result set)
                "subject_id": occ_uuid, "rel_type": "instance_of",
                "object_id": type_uuid, "fact_class": "A",
            },
        ]
        pref = {occ_uuid: "Advanced Python", type_uuid: "webinar"}
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        # The participation render now carries the TYPE word AND the date.
        assert any("webinar" in p and "Advanced Python" in p and "2026-05-03" in p
                   for p in prose), f"type word missing from dated occurrence render: {joined}"

    def test_dated_occurrence_without_instance_of_unchanged(self):
        """Fail-safe: no instance_of edge in the set → render unchanged (no type word)."""
        user_uuid = str(uuid4())
        occ_uuid = str(uuid4())
        facts = [{
            "subject_id": user_uuid, "rel_type": "participated_in",
            "object_id": occ_uuid, "event_date": "2026-05-03", "fact_class": "A",
        }]
        pref = {occ_uuid: "Advanced Python"}
        prose = self._run(facts, user_uuid, pref)
        assert prose and "Advanced Python" in prose[0] and "2026-05-03" in prose[0]
        # No type edge → no extra type word appended (today's behavior preserved).
        assert "webinar" not in prose[0]

    def test_type_not_duplicated_when_already_in_prose(self):
        """If the type word already appears (occurrence name == type), it is NOT appended twice."""
        user_uuid = str(uuid4())
        occ_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "participated_in",
             "object_id": occ_uuid, "event_date": "2026-05-03", "fact_class": "A"},
            {"subject_id": occ_uuid, "rel_type": "instance_of",
             "object_id": type_uuid, "fact_class": "A"},
        ]
        # occurrence name and type both render as "webinar" → must not become "webinar webinar".
        pref = {occ_uuid: "webinar", type_uuid: "webinar"}
        prose = self._run(facts, user_uuid, pref)
        assert prose and prose[0].lower().count("webinar") == 1, \
            f"type word duplicated: {prose}"

    def test_dated_occurrence_appends_subject_matter(self):
        """Q1 (lean-query rendering): a dated participated_in occurrence with a BLAND head
        ("issue") whose (<occurrence>, related_to, <specific>) subject-matter edge ("gps
        system") is in the SAME fact set now surfaces that subject-matter word the question
        keys on. Metadata-driven (reads the existing related_to edge), subject-agnostic."""
        user_uuid = str(uuid4())
        occ_uuid = str(uuid4())
        type_uuid = str(uuid4())
        about_uuid = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "participated_in",
             "object_id": occ_uuid, "event_date": "2023-03-22", "fact_class": "A"},
            {"subject_id": occ_uuid, "rel_type": "instance_of",
             "object_id": type_uuid, "fact_class": "A"},
            {"subject_id": occ_uuid, "rel_type": "related_to",
             "object_id": about_uuid, "fact_class": "A"},
        ]
        pref = {occ_uuid: "issue", type_uuid: "issue", about_uuid: "gps system"}
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        # The participation render carries the gps subject-matter the gold keys on.
        assert any("gps system" in p for p in prose), \
            f"subject-matter (gps system) missing from occurrence render: {joined}"

    def test_dated_occurrence_without_subject_matter_unchanged(self):
        """Fail-safe: no related_to edge in the set → no subject-matter appended."""
        user_uuid = str(uuid4())
        occ_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "participated_in",
             "object_id": occ_uuid, "event_date": "2023-03-22", "fact_class": "A"},
            {"subject_id": occ_uuid, "rel_type": "instance_of",
             "object_id": type_uuid, "fact_class": "A"},
        ]
        pref = {occ_uuid: "issue", type_uuid: "issue"}
        prose = self._run(facts, user_uuid, pref)
        assert prose and " with " not in prose[0], \
            f"unexpected subject-matter appended: {prose}"


class TestConvertToProseNamedInstanceJoin:
    """STAGE A (lean-query rendering, generalized): a RELATIONAL fact whose object is a
    UUID-resolved NAMED INSTANCE that ALSO carries an (<object>, instance_of, <type>) edge now
    renders the object slot as "<type> named <Name>" — "I have a dog named Fraggle" → "You have a
    dog named Fraggle", not "…that is fraggle". Purely STRUCTURAL (object has a name + an
    instance_of type); ZERO kind literals, NO rel allow-list (the only metadata read is
    is_hierarchy_rel, so instance_of/subclass_of are skipped). Subject-agnostic — the SAME path
    renders "your friend Diane" / "a server named Apollo". Name casing preserved verbatim
    (det-safety); the type is purely additive. Fail-safe: no type → render unchanged."""

    _TEMPLATE_META = {
        "has_pet": {
            "natural_language": "X has a pet that is Y",
            "natural_language_2p": "You have a pet that is Y",
            "label": "has pet", "is_hierarchy_rel": False,
        },
        "owns": {
            "natural_language": "X owns Y",
            "natural_language_2p": "You own Y",
            "label": "owns", "is_hierarchy_rel": False,
        },
        "friend_of": {
            "natural_language": "X is a friend of Y",
            "natural_language_2p": "You are a friend of Y",
            "label": "friend of", "is_hierarchy_rel": False,
        },
        "instance_of": {
            "natural_language": "X is a Y",
            "natural_language_2p": None,
            "label": "instance of", "is_hierarchy_rel": True,
        },
        "subclass_of": {
            "natural_language": "X is a subclass of Y",
            "natural_language_2p": None,
            "label": "subclass of", "is_hierarchy_rel": True,
        },
    }

    def _run(self, facts, user_uuid, preferred_alias_map, fetchone_row=None):
        db, cur = _mock_db()
        cur.fetchall.return_value = []  # same_as identity expansion (none)
        cur.fetchone.return_value = fetchone_row  # instance_of DB fallback (None → inert)
        with patch("src.api.main.rel_type_overlay.resolve_current",
                   return_value=self._TEMPLATE_META), \
             patch("src.api.main.resolve_display_name",
                   side_effect=lambda eid, _db: preferred_alias_map.get(eid, str(eid))):
            return convert_to_prose(
                facts, db, anchor=user_uuid, user_id=user_uuid,
                preferred_alias_map=preferred_alias_map,
            )

    def test_named_pet_composes_type_named_name(self):
        """Headline: has_pet object 'fraggle' with instance_of 'dog' → 'a dog named fraggle',
        NOT 'a pet that is fraggle'. Name token preserved; type 'dog' added."""
        user_uuid = str(uuid4())
        pet_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "has_pet",
             "object_id": pet_uuid, "fact_class": "A"},
            {"subject_id": pet_uuid, "rel_type": "instance_of",
             "object_id": type_uuid, "fact_class": "A"},
        ]
        pref = {pet_uuid: "fraggle", type_uuid: "dog"}
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        # The has_pet render now carries "dog named Fraggle" — the structure-driven
        # composer renders shape 1 and the titlecase POST-PASS titles the resolved
        # single-token name slot (fraggle → Fraggle). Det-safe: the det-scorer is
        # case-insensitive, so the NAME token still matches gold; type stays lowercase.
        assert any("dog named Fraggle" in p for p in prose), \
            f"named-instance join missing: {joined}"
        # Det-safety: the NAME token survives (case-insensitive — scorer lowercases).
        assert any("fraggle" in p.lower() for p in prose), f"name token dropped: {joined}"

    def test_name_casing_preserved_verbatim(self):
        """DET-SAFETY: the resolved name's casing is preserved (no force-titlecase); the type
        is purely additive — a case-sensitive gold match must not break."""
        user_uuid = str(uuid4())
        pet_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "has_pet",
             "object_id": pet_uuid, "fact_class": "A"},
            {"subject_id": pet_uuid, "rel_type": "instance_of",
             "object_id": type_uuid, "fact_class": "A"},
        ]
        pref = {pet_uuid: "Fraggle", type_uuid: "dog"}  # mixed-case name
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        assert any("dog named Fraggle" in p for p in prose), \
            f"casing not preserved / join missing: {joined}"
        # The capital-F name must survive exactly; not lowercased.
        assert "fraggle named" not in joined.lower().replace("dog named fraggle", "")

    def test_subject_agnostic_server_and_friend(self):
        """SUBJECT-AGNOSTIC proof: 'a server named Apollo' (owns) and 'a friend named Diane'
        (friend_of) render via the SAME path — no kind branch."""
        user_uuid = str(uuid4())
        srv_uuid = str(uuid4()); srv_type = str(uuid4())
        fr_uuid = str(uuid4()); fr_type = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "owns",
             "object_id": srv_uuid, "fact_class": "A"},
            {"subject_id": srv_uuid, "rel_type": "instance_of",
             "object_id": srv_type, "fact_class": "A"},
            {"subject_id": user_uuid, "rel_type": "friend_of",
             "object_id": fr_uuid, "fact_class": "A"},
            {"subject_id": fr_uuid, "rel_type": "instance_of",
             "object_id": fr_type, "fact_class": "A"},
        ]
        pref = {srv_uuid: "Apollo", srv_type: "server",
                fr_uuid: "Diane", fr_type: "friend"}
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        assert any("server named Apollo" in p for p in prose), \
            f"server join missing: {joined}"
        assert any("friend named Diane" in p for p in prose), \
            f"friend join missing: {joined}"

    def test_type_equals_name_not_joined(self):
        """Guard: when type == name (a bare-kind object, no real instance name), do NOT
        compose 'a dog named dog' — the join is skipped."""
        user_uuid = str(uuid4())
        obj_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [
            {"subject_id": user_uuid, "rel_type": "has_pet",
             "object_id": obj_uuid, "fact_class": "A"},
            {"subject_id": obj_uuid, "rel_type": "instance_of",
             "object_id": type_uuid, "fact_class": "A"},
        ]
        pref = {obj_uuid: "dog", type_uuid: "dog"}  # name == type
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        assert "named" not in joined, f"'dog named dog' not guarded: {joined}"

    def test_bare_object_no_instance_of_unchanged(self):
        """Fail-safe: a relational object with NO instance_of type in the set and no DB
        fallback hit → render unchanged (no 'named' join)."""
        user_uuid = str(uuid4())
        obj_uuid = str(uuid4())
        facts = [{
            "subject_id": user_uuid, "rel_type": "owns",
            "object_id": obj_uuid, "fact_class": "A",
        }]
        pref = {obj_uuid: "Apollo"}
        prose = self._run(facts, user_uuid, pref, fetchone_row=None)
        joined = " || ".join(prose)
        assert "named" not in joined, f"unexpected join on bare object: {joined}"
        assert any("Apollo" in p for p in prose), f"object name lost: {joined}"

    def test_hierarchy_rel_object_not_joined(self):
        """instance_of / subclass_of (hierarchy rels) are NEVER composed — that would yield
        'a dog named dog' on the type edge itself. Only metadata (is_hierarchy_rel) gates this,
        no rel-name allow-list."""
        user_uuid = str(uuid4())
        sub_uuid = str(uuid4())
        sup_uuid = str(uuid4())
        # poodle subclass_of dog, AND poodle instance_of (its own type) in the set
        facts = [
            {"subject_id": sub_uuid, "rel_type": "subclass_of",
             "object_id": sup_uuid, "fact_class": "A"},
            {"subject_id": sub_uuid, "rel_type": "instance_of",
             "object_id": sup_uuid, "fact_class": "A"},
        ]
        pref = {sub_uuid: "poodle", sup_uuid: "dog"}
        prose = self._run(facts, user_uuid, pref)
        joined = " || ".join(prose)
        # subclass_of object 'dog' must NOT become 'a dog named dog' or get a 'named' join.
        assert "named" not in joined, f"hierarchy rel got named-join: {joined}"

    def test_db_fallback_resolves_missing_type(self):
        """DETERMINISTIC DB FALLBACK: the companion instance_of edge is NOT in the fact set,
        but the type-cast IS captured on the entity — resolve it by the object id via DB."""
        user_uuid = str(uuid4())
        pet_uuid = str(uuid4())
        type_uuid = str(uuid4())
        facts = [{
            "subject_id": user_uuid, "rel_type": "has_pet",
            "object_id": pet_uuid, "fact_class": "A",
        }]
        pref = {pet_uuid: "Fraggle", type_uuid: "dog"}
        # No instance_of edge in the set → index miss → DB fallback returns the type id.
        prose = self._run(facts, user_uuid, pref, fetchone_row=(type_uuid,))
        joined = " || ".join(prose)
        assert any("dog named Fraggle" in p for p in prose), \
            f"DB fallback type-join missing: {joined}"

    def test_object_is_user_not_joined(self):
        """The user (object resolves to 'you') is NEVER type-tagged as a named instance:
        'Marla is the parent of you', not 'a person named you'."""
        user_uuid = str(uuid4())
        other_uuid = str(uuid4())
        meta = dict(self._TEMPLATE_META)
        meta["parent_of"] = {
            "natural_language": "X is the parent of Y",
            "natural_language_2p": "You are the parent of Y",
            "label": "parent of", "is_hierarchy_rel": False,
        }
        facts = [{
            "subject_id": other_uuid, "rel_type": "parent_of",
            "object_id": user_uuid, "fact_class": "A",
        }]
        pref = {other_uuid: "Marla"}
        db, cur = _mock_db()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = ("zzz",)  # even if a stray type existed, the you-guard wins
        with patch("src.api.main.rel_type_overlay.resolve_current", return_value=meta), \
             patch("src.api.main.resolve_display_name",
                   side_effect=lambda eid, _db: pref.get(eid, str(eid))):
            prose = convert_to_prose(
                facts, db, anchor=user_uuid, user_id=user_uuid,
                preferred_alias_map=pref,
            )
        joined = " || ".join(prose)
        assert "named you" not in joined, f"user object got type-join: {joined}"
        assert any("you" in p for p in prose), f"object 'you' lost: {joined}"
