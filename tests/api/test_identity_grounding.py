"""Identity-grounding regression tests — LOOSE ON INPUT, TIGHT ON RETURN.

Covers three identity behaviors, split across the ingest (registration) and read
(surfacing) layers per CLAUDE.md's "dumb registry / smart query" principle:

  REGISTRATION (loose on input) — _extract_identity / _extract_preferred_name:
      The ingest path accepts the captured name candidate RAW. A legitimate
      lowercase "my name is sam" MUST register. A loose candidate ("worried"
      from "I'm worried…") MAY register as just-another alias — there is NO
      capitalization gate and NO junk-word rejection at registration. Cheap
      aliases hang off the one user; discernment is deferred to read time.

  READ (tight on return) — _is_surfaceable_user_name:
      At the moment the user's name is surfaced for recall, a junk common/state
      word ("worried"/"thinking") must NOT be presented as the user's name; the
      caller falls back to a better alias or to "you". The filter uses ONLY the
      closed stopword set (a language primitive) — NO capitalization assumption,
      because aliases are stored lowercased and casing is unavailable at read.

  BUG 2 (unchanged) — first-person pronoun ("i"/"me"/"my"/"myself"/"mine") resolves
      to the request user_id, never minting a phantom surrogate. Identity
      RESOLUTION, not name pattern-detection — kept as-is.
"""

import uuid

import pytest

from src.api.main import (
    _extract_identity,
    _extract_preferred_name,
    _is_surfaceable_user_name,
)
from src.entity_registry.registry import EntityRegistry, _FIRST_PERSON_PRONOUNS


# ─────────────────────────────────────────────────────────────────────────────
# REGISTRATION — LOOSE ON INPUT
# A real name registers regardless of casing; a loose state-word candidate is
# allowed to register raw. NO capitalization gate, NO junk rejection at ingest.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Capitalized real names register (stored lowercased).
    ("My name is Alicia", "alicia"),
    ("I'm Sam", "sam"),
    ("I am Dana", "dana"),
    ("Call me Bob", "bob"),
    ("People call me Theo", "theo"),
    # LOOSE ON INPUT: a legitimate LOWERCASE declaration must register too — the
    # old caps-gate dropped this, the bug this revision fixes.
    ("my name is sam", "sam"),
    ("i'm dana", "dana"),
])
def test_extract_identity_registers_names_loosely(text, expected):
    assert _extract_identity(text) == expected


@pytest.mark.parametrize("text,expected", [
    # LOOSE: a state/gerund word IS allowed to register as a raw alias — the dumb
    # registry does not judge it. It just becomes another also_known_as.
    ("I'm worried about the move", "worried"),
    ("I am thinking of hosting a dinner", "thinking"),
])
def test_extract_identity_loose_allows_state_words(text, expected):
    """Registration no longer pattern-detects: a loose candidate lands raw."""
    assert _extract_identity(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("I prefer to be called Sam", "sam"),
    # LOOSE ON INPUT: lowercase explicit declaration registers.
    ("I prefer to be called sam", "sam"),
    ("I go by Theo", "theo"),
    # LOOSE: a state word is allowed to register as a preferred alias too.
    ("I prefer to be called worried", "worried"),
])
def test_extract_preferred_name_registers_loosely(text, expected):
    assert _extract_preferred_name(text) == expected


def test_extract_preferred_name_single_char_floor():
    """The only ingest guard kept: a bare single character is not a name."""
    assert _extract_preferred_name("I go by X") is None


# ─────────────────────────────────────────────────────────────────────────────
# READ — TIGHT ON RETURN
# _is_surfaceable_user_name gates whether a stored alias may be SURFACED as the
# user's display name. Closed stopword set; NO caps assumption (aliases are
# stored lowercased).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("alias", ["sam", "alicia", "dana", "bob", "theo"])
def test_surfaceable_accepts_real_names(alias):
    """A real (lowercased) name IS surfaceable as the user's name."""
    assert _is_surfaceable_user_name(alias) is True


@pytest.mark.parametrize("alias", ["worried", "thinking", "married", "going", "tired"])
def test_surfaceable_rejects_junk_common_words(alias):
    """Junk common/state words are NOT surfaced as the user's name — they fell
    through the loose registration but the read filter holds them back."""
    assert _is_surfaceable_user_name(alias) is False


@pytest.mark.parametrize("alias", ["", None, "   "])
def test_surfaceable_failsafe_on_empty(alias):
    """Fail-safe: empty/None → not surfaceable (caller falls through to its
    existing fallback — today's behavior on a missing name)."""
    assert _is_surfaceable_user_name(alias) is False


def test_surfaceable_no_caps_assumption():
    """Aliases are stored lowercased — the filter must accept a lowercase real
    name (no capitalization requirement) and still reject a lowercase stopword."""
    assert _is_surfaceable_user_name("sam") is True
    assert _is_surfaceable_user_name("worried") is False


# ─────────────────────────────────────────────────────────────────────────────
# LOOSE-IN / TIGHT-OUT — the end-to-end contract for a single candidate
# ─────────────────────────────────────────────────────────────────────────────

def test_loose_in_tight_out_state_word():
    """"I'm worried" registers 'worried' (loose in) but it is never surfaced as
    the user's name (tight out)."""
    registered = _extract_identity("I'm worried about the move")
    assert registered == "worried"                       # loose: it registered
    assert _is_surfaceable_user_name(registered) is False  # tight: never surfaced


def test_loose_in_tight_out_real_lowercase_name():
    """A real lowercase name registers AND surfaces."""
    registered = _extract_identity("my name is sam")
    assert registered == "sam"                         # loose: registered
    assert _is_surfaceable_user_name(registered) is True   # tight: surfaceable


# ─────────────────────────────────────────────────────────────────────────────
# BUG 2 — first-person pronoun grounding in EntityRegistry.resolve (UNCHANGED)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCursor:
    """Minimal cursor: records executes, returns no rows. Enough for the
    name=='user'/pronoun branch which only runs SET search_path + INSERT."""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self._cur = _FakeCursor()
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.mark.parametrize("pronoun", sorted(_FIRST_PERSON_PRONOUNS))
def test_first_person_pronoun_resolves_to_user(pronoun):
    """"i"/"me"/"my"/"myself"/"mine" ground to the request user_id — never a
    fresh surrogate. The phantom "i" entity bug. Identity RESOLUTION, kept."""
    user_id = str(uuid.uuid4())
    reg = EntityRegistry(_FakeConn(), schema_name="faultline_test")
    resolved = reg.resolve(user_id, pronoun)
    assert resolved == user_id, f"{pronoun!r} should ground to user_id"


@pytest.mark.parametrize("pronoun", ["I", "Me", "MY", "Myself"])
def test_first_person_pronoun_case_insensitive(pronoun):
    """Capitalized/mixed-case pronouns also ground (resolve lowercases first)."""
    user_id = str(uuid.uuid4())
    reg = EntityRegistry(_FakeConn(), schema_name="faultline_test")
    assert reg.resolve(user_id, pronoun) == user_id


def test_user_literal_still_resolves_to_user():
    user_id = str(uuid.uuid4())
    reg = EntityRegistry(_FakeConn(), schema_name="faultline_test")
    assert reg.resolve(user_id, "user") == user_id


def test_non_pronoun_does_not_ground_to_user():
    """A real entity name must NOT collapse to user_id — it mints a surrogate."""
    user_id = str(uuid.uuid4())
    reg = EntityRegistry(_FakeConn(), schema_name="faultline_test")
    resolved = reg.resolve(user_id, "fence")
    assert resolved != user_id
    # Deterministic UUID v5 surrogate.
    assert uuid.UUID(resolved)  # valid UUID
