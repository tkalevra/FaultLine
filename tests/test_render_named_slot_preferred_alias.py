"""RENDER NAME-SLOT PREFERRED-ALIAS GUARD — the "a Kwame named Ptolemy" wound.

THE WOUND (pre-prod seat c3d62ac4): recall rendered "you have a Kwame named Ptolemy"
for an entity whose alias set is {child (non-preferred), kwame (non-preferred),
ptolemy (preferred)}. The named-instance composer's TYPE slot resolved to a word
that is one of the INSTANCE ENTITY'S OWN aliases ("kwame") — the prose template
bound the person to the wrong alias and rendered the person AS ITS OWN TYPE. The
correct render is "a son named Ptolemy" (a real type word) or the preferred alias
("Ptolemy") — never a non-preferred alias in a naming slot.

FIX (deterministic, metadata-driven, NO word lists):
  1. _type_word_is_instance_alias — the candidate type word is probed against
     entity_aliases FOR THE INSTANCE ENTITY ITSELF; a hit means the "type" is a
     NAME of the very entity being classified (a category error across THE HARD
     LINE: a memory is filed AT a place, never its own place). Both type-slot
     resolution sites (the object-side composer input and the inverse-composer
     input) decline such a word; the template path then renders the entity's
     PREFERRED display name (resolve_entity_display is preferred-first).
  2. resolve_display_name's non-preferred FALLBACK is now ordered
     "ORDER BY is_preferred DESC, alias" — the old unordered LIMIT 1 was
     nondeterministic and could surface an arbitrary non-preferred alias.

PIN ENVIRONMENT (honest disclosure): importing src.api.main needs the API
dependency set (fastapi/httpx/psycopg...) which is NOT installed on the local
code-only box, and the alias probe is DB-bound. So:
  - the STRUCTURAL pins below extract the source via ast (the
    test_bare_plural_kin_alias_precedence pattern) and run in seconds ANYWHERE;
  - the BEHAVIORAL pin (entity {child, kwame, ptolemy(pref)} renders with
    'ptolemy') needs the API deps + POSTGRES_DSN: it SKIPS locally and executes
    on pre-prod. Never a silent pass — the skip reason is explicit.
"""

import os

import pytest


# --------------------------------------------------------------------------- #
# 0. Structural pins — the fix shape itself, no imports (seconds anywhere)
# --------------------------------------------------------------------------- #

_MAIN = os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")


def _main_source():
    with open(_MAIN, "r", encoding="utf-8") as f:
        return f.read()


def _fn_source(name):
    import ast
    with open(_MAIN, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            with open(_MAIN, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return "".join(lines[node.lineno - 1:node.end_lineno])
    pytest.fail("function %s not found in src/api/main.py" % name)


def test_type_word_alias_probe_exists_and_is_metadata_bound():
    """The guard helper exists and probes entity_aliases for THE INSTANCE ITSELF —
    metadata (the alias registry + its is_preferred rows), never a word list."""
    src = _fn_source("_type_word_is_instance_alias")
    assert "FROM entity_aliases" in src
    assert "lower(alias) = lower(%s)" in src
    # fail-safe direction: a probe error keeps today's behaviour (False)
    assert "return False" in src


def test_both_type_slot_resolution_sites_decline_an_instance_alias():
    """Both named-instance TYPE-slot resolution sites (object-side composer input
    and the inverse-composer mirror) apply the guard and demote the type word to
    None — so the "... named ..." render can never fire with an alias of the
    instance entity itself."""
    src = _main_source()
    assert src.count("_type_word_is_instance_alias(") >= 3  # def + 2 call sites
    obj_site = src.find("convert_to_prose.type_word_was_instance_alias")
    subj_site = src.find("convert_to_prose.subject_type_word_was_instance_alias")
    assert obj_site != -1 and subj_site != -1
    for site_at in (obj_site, subj_site):
        window = src[site_at:site_at + 600]
        assert "None" in window  # the slot is demoted after the guard fires


def test_display_name_nonpreferred_fallback_is_ordered_preferred_first():
    """The any-alias fallback in resolve_display_name is deterministic and
    preferred-first — never an unordered LIMIT 1 that can surface an arbitrary
    non-preferred alias."""
    src = _fn_source("resolve_display_name")
    assert "ORDER BY is_preferred DESC, alias LIMIT 1" in src
    assert "FROM entity_aliases WHERE entity_id = %s LIMIT 1" not in src


def test_type_slot_db_fallback_still_guards_self_loops():
    """The guard ADDS to the existing self-loop protection (instance_of target != the
    entity) — it must not have replaced it."""
    src = _main_source()
    assert src.count("_ni_type_id != object_id") >= 1
    assert src.count("_st_id != subject_id") >= 1


# --------------------------------------------------------------------------- #
# 1. Behavioral pin — needs the API deps + a live Postgres (pre-prod executes;
#    SKIPS locally, disclosed, never a silent pass).
# --------------------------------------------------------------------------- #

def _api_deps_available():
    try:
        import fastapi  # noqa: F401
        import psycopg2  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not (_api_deps_available() and os.environ.get("POSTGRES_DSN")),
    reason="API deps + POSTGRES_DSN required (executes on pre-prod; skipped on the "
           "local code-only box — disclosed, not a silent pass)")
def test_entity_with_preferred_alias_renders_with_the_preferred_name():
    """THE PIN: an entity whose alias set is {child (non-preferred), kwame
    (non-preferred), ptolemy (preferred)} renders with 'ptolemy' — the naming
    slots never surface a non-preferred alias, and the "named" composer never
    fires with a type word that is an alias of the instance itself."""
    from src.api.main import _type_word_is_instance_alias, resolve_entity_display

    dsn = os.environ["POSTGRES_DSN"]
    import psycopg2
    import uuid as _uuid

    conn = psycopg2.connect(dsn)
    try:
        schema = "pin_renderslot_%s" % _uuid.uuid4().hex[:12]
        entity_id = str(_uuid.uuid4())
        with conn.cursor() as c:
            c.execute("CREATE SCHEMA " + schema)
            c.execute("SET search_path TO " + schema)
            # structurally clone the three tables the fix reads (LIKE public INCLUDING ALL
            # — structure + uniques, no seed rows; the test_bare_plural pattern).
            # FIXTURE FIX (owed upstream, assertions untouched): (a) entities carries a NOT
            # NULL user_id — the entity row is inserted with it, as the live write path does;
            # (b) migration 214 adds entity_aliases.display_form PER-TENANT and the public
            # TEMPLATE lacks the column, so the LIKE-public clone ADDS it explicitly (the
            # per-tenant shape) and the rows carry their display_form — the fixture must
            # mirror the per-tenant schema the fix actually reads, not the bare template.
            for tbl in ("entities", "entity_aliases", "facts"):
                c.execute("CREATE TABLE %s (LIKE public.%s INCLUDING ALL)" % (tbl, tbl))
            c.execute("ALTER TABLE entity_aliases ADD COLUMN IF NOT EXISTS display_form TEXT")
            c.execute(
                "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, 'Person')",
                (entity_id, schema))
            for alias, pref in (("child", False), ("kwame", False), ("ptolemy", True)):
                c.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, is_preferred, display_form) "
                    "VALUES (%s, %s, %s, %s)",
                    (entity_id, alias, pref, alias))
        conn.commit()

        with conn.cursor() as c:
            c.execute("SET search_path TO " + schema)
            # (a) the alias probe: 'kwame' and 'child' ARE aliases of the instance —
            #     they are NAMES, never a type word; 'son' is not an alias (a real
            #     type word passes).
            assert _type_word_is_instance_alias(conn, entity_id, "kwame") is True
            assert _type_word_is_instance_alias(conn, entity_id, "child") is True
            assert _type_word_is_instance_alias(conn, entity_id, "son") is False
            # (b) display resolution is preferred-first: the entity renders with
            #     its preferred name, never a non-preferred alias.
            assert resolve_entity_display(entity_id, None, None, conn) == "ptolemy"
    finally:
        conn.close()
