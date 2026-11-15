"""IDENTITY-HONESTY BUG 3 - the spelling-variant fold, ENGINE-GROWN (no dialect dictionary).

Measured on the owner's live demo (2026-08-21): "My favourite colour is Blue" grew the rel
favourite_colour, but "what is my favorite colour" grounded a junk entity instead of
folding to the grown rel - because nothing folds spelling variants and normalize_rel is
spelling-preserving (favourite_colour and favorite_colour are different keys).

The shipped rail (subject-agnostic, metadata-driven - NO in-code en-GB/en-US dictionaries,
NO domain word lists, and NO generation of invented candidates):

  1. canonical.py carries a SINGLE-EDIT morphological-closeness test: a surface is a
     spelling sibling of a rel when both have the same token count and exactly one token
     differs by one character edit (favourite/favorite, organise/organize,
     analyse/analyze, colour/color, recieve/receive). A dialect RULE TABLE was built and
     REFUTED in-session: any rule strong enough to generate favourite->favorite also
     folds/generates works_for<->works_four (different lexemes) - see the canonical.py
     header for the refutation record;
  2. resolve_grown_spelling_variant folds a novel surface onto the UNIQUE live rel that
     is its sibling (targets = the tenant's OWN rel_types: DB metadata; ambiguous -> None);
  3. the ingest L4 placement seam calls it and GROWS a rel_type_aliases row
     (source='engine') on each fold - the deterministic alias rail every consumer already
     reads, so the fold persists;
  4. the query DOOR 1 compound-rel lookup folds via the same resolver when the alias row
     is missing (older seats).

DB-gated parts skip cleanly without WELD_GUARD_TEST_DSN; the mechanism tests run pure.
"""
import os
import uuid

import pytest

try:
    from src.ontology.canonical import (  # noqa: E402
        normalize_rel,
        resolve_grown_spelling_variant,
        spelling_variant_key,
        spelling_variant_siblings,
    )
    _HAVE_VARIANT_RAIL = True
except ImportError:  # pre-fix tree: the rail does not exist yet (the RED state)
    from src.ontology.canonical import normalize_rel  # noqa: E402

    resolve_grown_spelling_variant = None
    spelling_variant_key = None
    spelling_variant_siblings = None
    _HAVE_VARIANT_RAIL = False


def _require_rail():
    if not _HAVE_VARIANT_RAIL:
        pytest.fail("the spelling-variant rail must exist in canonical.py (bug 3)")


# -- The pure mechanism (no DB) ------------------------------------------------


def test_oxford_pairs_are_siblings():
    _require_rail()
    for a, b in [
        ("favourite_colour", "favorite_colour"),
        ("favourite_color", "favorite_color"),
        ("favourite_colour", "favourite_color"),   # colour/color alone
        ("organise", "organize"),
        ("analyse", "analyze"),
        ("recieve", "receive"),                    # plain typo - same mechanism
    ]:
        assert spelling_variant_siblings(a, b), f"{a} <-> {b} must be single-edit siblings"
        assert spelling_variant_siblings(b, a), f"siblinghood must be symmetric: {b} <-> {a}"


def test_distinct_rels_are_not_siblings():
    _require_rail()
    for a, b in [
        ("works_for", "has_pet"),
        ("works_for", "age"),
        ("favourite_colour", "favorite_food"),     # two tokens differ
        ("favourite_colour", "favorite"),          # token count differs
        ("favourite_colour", "favourite_colour"),  # identity is not a fold
    ]:
        assert not spelling_variant_siblings(a, b), f"{a} <-> {b} must NOT fold"


def test_key_is_the_surface_identity():
    """spelling_variant_key is the named seam for the fold surface; under single-edit
    closeness there is no canonical image, so the key IS the (lowercased) rel."""
    _require_rail()
    assert spelling_variant_key("Favourite_Colour") == "favourite_colour"


def test_normalize_rel_remains_spelling_preserving():
    """The fold is ENGINE-GROWN, not a normalize_rel change: normalize_rel must stay
    spelling-preserving (byte-identical keys) so existing rel identities never move."""
    assert normalize_rel("favourite colour") == "favourite_colour"
    assert normalize_rel("favorite colour") == "favorite_colour"


# -- No dictionary constraint (source-level) -----------------------------------


def test_no_dialect_dictionary_in_code():
    """The mechanism must not enumerate dialect words: canonical.py's EXECUTABLE string
    constants must not include colour/favourite/flavour/organise/realise/analyse (the
    forbidden en-GB/en-US dictionary shape). Occurrences inside comments/docstrings are
    documentation, not a dictionary - AST parse and inspect only real constants."""
    import ast
    import inspect
    from src.ontology import canonical
    tree = ast.parse(inspect.getsource(canonical))
    # collect string constants EXCEPT docstrings (sole Expr of a function/module/class body)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(body[0].value.value)
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    ]
    joined = "\n".join(literals).lower()
    for word in ("colour", "favourite", "flavour", "organise", "realise", "analyse"):
        assert word not in joined, (
            f"canonical.py carries the dialect word {word!r} as an executable string "
            "literal - the variant rail is single-edit closeness only, no dictionary "
            "(bug 3 constraint)")


# -- The fold resolver (DB-gated) ----------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("WELD_GUARD_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres to run the variant-fold DB tests",
)


@pytest.fixture()
def tenant():
    sch = "vf_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(f"CREATE SCHEMA {sch}")
        c.execute(f"SET search_path TO {sch}")
        c.execute("CREATE TABLE rel_types (rel_type text PRIMARY KEY, source text)")
        c.execute(
            "CREATE TABLE rel_type_aliases ("
            "alias text PRIMARY KEY, canonical_rel_type text NOT NULL, "
            "requires_inversion boolean DEFAULT false, source text DEFAULT 'engine')")
    conn.commit()
    yield conn, sch
    conn.rollback()
    with conn.cursor() as c:
        c.execute(f"DROP SCHEMA {sch} CASCADE")
    conn.commit()
    conn.close()


def _reset_caches():
    from src.ontology import canonical
    canonical.reset_caches()


def test_fold_finds_the_grown_twin(tenant):
    _require_rail()
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favourite_colour', 'engine')")
    conn.commit()
    _reset_caches()
    hit = resolve_grown_spelling_variant("favorite_colour", DSN, sch)
    assert hit == "favourite_colour", (
        "a US-spelled surface must fold onto the grown British rel (bug 3)")


def test_fold_is_symmetric(tenant):
    _require_rail()
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favorite_color', 'engine')")
    conn.commit()
    _reset_caches()
    hit = resolve_grown_spelling_variant("favourite_color", DSN, sch)
    assert hit == "favorite_color", (
        "a British-spelled surface must fold onto a grown US rel (the rail is not "
        "dialect-preferential - direction is decided by which rel EXISTS)")


def test_no_fold_when_both_twins_live(tenant):
    _require_rail()
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favourite_colour', 'engine')")
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favorite_colour', 'engine')")
    conn.commit()
    _reset_caches()
    assert resolve_grown_spelling_variant("favourite_colour", DSN, sch) is None
    assert resolve_grown_spelling_variant("favorite_colour", DSN, sch) is None, (
        "when both twins are already live the surface resolves EXACTLY - the resolver "
        "must never return the surface itself or shadow a live rel")


def test_no_fold_without_a_twin(tenant):
    _require_rail()
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('works_for', 'wikidata')")
    conn.commit()
    _reset_caches()
    assert resolve_grown_spelling_variant("favourite_colour", DSN, sch) is None


def test_no_fold_when_two_live_rels_are_both_siblings(tenant):
    """Genuine ambiguity (two live rels each one edit away) -> None, never a coin-flip."""
    _require_rail()
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favorite_colour', 'engine')")
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favourite_color', 'engine')")
    conn.commit()
    _reset_caches()
    assert resolve_grown_spelling_variant("favourite_colour", DSN, sch) is None, (
        "two live siblings = ambiguous; a wrong fold is worse than a miss")


# -- Mint-time cache invalidation (found LIVE on the fl-g2 throwaway stack) -------------
# canonical._RELTYPE_CACHE has NO TTL: a rel minted by request N was invisible to the
# fold resolver in request N+1 because the cache still held the pre-mint frozenset, so
# the junk twin was minted anyway. Unit rigs never saw it - they reset_caches() between
# setup and probe. The invariant: EVERY mint site drops canonical's caches for the schema.


def test_warm_stale_cache_blocks_the_fold_until_reset(tenant):
    """Documents the exact live failure shape: a warm cache that predates the mint makes
    the resolver MISS - this is why mint sites MUST invalidate (next test)."""
    _require_rail()
    conn, sch = tenant
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('works_for', 'wikidata')")
    conn.commit()
    _reset_caches()
    from src.ontology import canonical
    # warm the cache WITHOUT favourite_colour
    canonical._load_reltypes(DSN, sch)
    # ... a later request mints favourite_colour (direct INSERT stands in for the mint)
    with conn.cursor() as c:
        c.execute("INSERT INTO rel_types (rel_type, source) VALUES ('favourite_colour', 'engine')")
    conn.commit()
    # NO reset: the stale cache hides the rel from the resolver
    assert resolve_grown_spelling_variant("favorite_colour", DSN, sch) is None, (
        "pre-condition: the stale cache hides the mint (if this FAILS the cache gained a "
        "TTL - update this suite accordingly)")
    # reset (what the mint sites now do): the fold finds the twin
    canonical.reset_caches(sch)
    assert resolve_grown_spelling_variant("favorite_colour", DSN, sch) == "favourite_colour"


def test_every_mint_site_invalidates_canonical_caches():
    """Source-level pin (mutation-checked): both mint sites must drop canonical's caches
    for the request schema - found live where the in-flow mint alone was covered by NO
    invalidation and the junk twin survived a second ingest."""
    import inspect
    from src.api import main as api_main
    src = inspect.getsource(api_main)
    for fn in ("_create_rel_type_in_flow", "_insert_novel_rel_type"):
        i = src.index(f"def {fn}(")
        body = src[i:i + 20000]
        assert "reset_caches(rel_type_overlay.get_current_schema())" in body, (
            f"{fn} must invalidate canonical's caches at the mint "
            "(identity-honesty bug 3, live-found)")
