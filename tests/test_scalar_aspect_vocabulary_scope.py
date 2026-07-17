"""VOCABSCOPE — scalar-aspect scope binds by NAME, not by stored-value coincidence.

Companion to ATTRSCOPE (tests/test_attr_scope_grounding.py). ATTRSCOPE grounds a
scalar aspect ONLY when the anchor ALREADY stored that entity_attribute. This closes
the sibling gap:

  A bare scalar-aspect question ("what is my gender", "what is my nationality",
  "what is my weight") must bind scope even before ANY value is stored — otherwise
  scope stays inert (scope_active False) and the query falls to the UNSCOPED fetch-all
  FIREHOSE. Today it binds ONLY by the lucky coincidence that the aspect word EQUALS
  the rel_type NAME ("nationality" == the `nationality` rel); the structurally
  IDENTICAL "what is my gender" firehosed because the word ("gender") != the rel NAME
  ("has_gender").

FIX (determine_path SCALAR-ASPECT VOCABULARY GROUNDING): admit a SCALAR rel_type to
path.scalar_rels when a query content-word equals the rel_type NAME or a whole content
word of its natural_language template ("gender" in "X's gender is Y" -> has_gender).
Scalar-only (projects the anchor's OWN entity_attributes -> no cross-group leak),
narrows (not a taxonomy group -> aspect firewall held), deterministic + metadata-driven
(rel_types.tail_types + natural_language), subject-agnostic (no attribute/domain literal).

REAL CURSOR: this family has had mock-drift false-greens (see ATTRSCOPE docstring), so
this runs determine_path against a LIVE tenant. It needs only the STANDARD seed
(has_gender / nationality / weight as SCALAR rels) — NO stored values — so any
standard-provisioned tenant works. Requires POSTGRES_DSN.
"""

import os
import uuid as _uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
import src.api.main as main  # noqa: E402


_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql://faultline:faultline@172.20.0.2:5432/faultline",
)

# The scalar aspects proven below — chosen so the aspect word is NOT the rel NAME
# (has_gender) as well as one where it IS (nationality/weight): the fix must bind BOTH.
_REQUIRED_SCALAR_RELS = {"has_gender", "nationality", "weight"}


@pytest.fixture(scope="module")
def db():
    try:
        conn = psycopg2.connect(_DSN)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Postgres at POSTGRES_DSN: {e}")
    conn.autocommit = True
    # Find a standard-seeded tenant schema whose scalar vocabulary carries the rels
    # under test. Vocabulary-only — no stored values needed.
    with conn.cursor() as c:
        c.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\\_%' "
            "ORDER BY nspname"
        )
        schemas = [r[0] for r in c.fetchall()]
    chosen = None
    for s in schemas:
        with conn.cursor() as c:
            c.execute(f"SET search_path TO {s}")
            try:
                c.execute(
                    "SELECT rel_type FROM rel_types WHERE 'SCALAR' = ANY(tail_types) "
                    "AND rel_type = ANY(%s)",
                    (sorted(_REQUIRED_SCALAR_RELS),),
                )
            except Exception:  # noqa: BLE001 — schema missing rel_types etc.
                continue
            have = {r[0] for r in c.fetchall()}
        if _REQUIRED_SCALAR_RELS <= have:
            chosen = s
            break
    if not chosen:
        pytest.skip("no standard-seeded tenant with has_gender/nationality/weight SCALAR rels")
    _CHOSEN["schema"] = chosen
    yield conn
    conn.close()


_CHOSEN: dict = {}


def _dp(db, query):
    schema = _CHOSEN["schema"]
    with db.cursor() as c:
        c.execute(f"SET search_path TO {schema}")
    # Neutralise the GLiNER2 taxonomy fallback (infra-free determinism): the point is
    # the DETERMINISTIC vocabulary bind, never a fuzzy zero-shot guess.
    _orig = main.get_gliner_model
    main.get_gliner_model = lambda: None
    try:
        # Anchor = a random UUID with NO stored attributes — proves the aspect binds
        # from the VOCABULARY, independent of any stored value (the ATTRSCOPE case).
        anchor = str(_uuid.uuid4())
        return main.determine_path(
            query, db, user_id=anchor,
            anchor_is_concrete_entity=False,
            anchor_uuid=None,
            anchor_resolved_uuid=anchor,
        )
    finally:
        main.get_gliner_model = _orig


def test_gender_binds_by_template_word_not_rel_name(db):
    """'what is my gender' -> has_gender via the natural_language template word
    ('gender' in "X's gender is Y"), even though 'gender' != the rel NAME. This is the
    exact firehose repro: today it fell to the unscoped dump."""
    path = _dp(db, "what is my gender")
    assert "has_gender" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False, "must NOT collapse to the fetch-all firehose"
    # Aspect-precise: a gender question does not drag in unrelated scalars.
    assert "nationality" not in path.scalar_rels
    assert "weight" not in path.scalar_rels


def test_nationality_still_binds(db):
    """'what is my nationality' -> nationality (rel NAME == aspect word). Regression
    guard for the case that worked by name-coincidence."""
    path = _dp(db, "what is my nationality")
    assert "nationality" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert path.fetch_all_details is False


def test_weight_binds_from_vocabulary(db):
    """'what is my weight' -> weight, bound from the scalar vocabulary with NO stored
    value on the anchor."""
    path = _dp(db, "what is my weight")
    assert "weight" in path.scalar_rels, path.scalar_rels
    assert path.scope_active is True
    assert "nationality" not in path.scalar_rels


def test_broad_self_query_stays_unbound(db):
    """The regression boundary: a genuinely broad query names NO scalar aspect, so the
    vocabulary grounding admits nothing and scope stays broad (fetch-all fallback is the
    HONEST behaviour when no aspect binds)."""
    path = _dp(db, "tell me about myself")
    assert not path.scalar_rels, path.scalar_rels
    assert not path.taxonomy_groups
    assert path.scope_active is False


def test_no_scalar_aspect_word_admits_nothing(db):
    """A query whose content word matches no scalar rel name/template admits no scalar
    aspect (proves the bind is vocabulary-grounded, not a blanket 'any question ->
    scalar')."""
    path = _dp(db, "what is my favorite movie")
    # 'favorite'/'movie' are no scalar rel name and no scalar template content word.
    for _r in ("has_gender", "nationality", "weight", "height", "occupation"):
        assert _r not in path.scalar_rels, (path.scalar_rels, _r)
