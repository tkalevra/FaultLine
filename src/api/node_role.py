"""node_role — the FIRST-CLASS value/place property on an entity node (THE HARD LINE, made a stamp).

Phase 1 of the spine-composition rethink (the internal design record, assessment finding
#3 / Q3). THE HARD LINE — a MEMORY (a specific user VALUE: ``blue`` in ``favorite_colour=blue``, an
address literal, ``Rex`` a pet name) must NEVER be treated as an L4 PLACE (a type node reachable by
``subclass_of``/``instance_of``). Today that predicate is re-derived ad-hoc in 30+ scattered
chain-local spots; when a layer forgets to re-derive it, a user value is laddered/typed/superseded
and buried (the ladder-on-value data-loss class). This module derives the predicate ONCE, STAMPS it
on ``entities.node_role``, and exposes ONE read (`is_protected_value`) every downstream consumer
calls instead of open-coding the guard.

DESIGN (see the doc):
  - Persisted carrier: ``entities.node_role TEXT`` ∈ {value, place, name, both}; NULL = unstamped
    (fall through to structural re-derivation — the flag-OFF default). Persisted because the ASYNC
    re_embedder growth has NO in-flight edge to re-derive from.
  - MONOTONIC, authority-ordered transitions (user > seed > growth): a ``value`` node never silently
    becomes a bare ``place``; a genuine type binding on a value node → ``both`` (OWL PUNNING — the
    one controlled cell, value protection STAYS); a user_stated value on a prior place node → ``both``
    (user wins). Growth (llm_inferred) can never demote user truth.
  - Grounding: RDF ``rdf:type`` vs ``rdfs:subClassOf`` (ABox individual vs TBox class); OWL 2 punning
    as the decided-once exception (https://www.w3.org/2007/OWL/wiki/Punning); WordNet instances have
    no hyponyms (Miller & Hearst, ACL J06-1001).

HARD CONSTRAINTS honored: deterministic (no cosine/LLM); metadata-driven (tail_types via the
TENANT-bound cursor — no rel/value literal); per-tenant search_path (reads the passed tenant-bound
``db_conn``, never a public connection); fail-loud/safe (SAVEPOINT-guarded, column-missing-safe; a
READ error → NOT protected = never over-suppress laddering; a WRITE error → no-op). Flag-gated
``VALUE_PLACE_FIRST_CLASS`` (default OFF = byte-identical to today).

Leaf module: stdlib + structlog only, so both ``src/api/main.py`` and ``src/re_embedder/embedder.py``
import it without a circular dependency.
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger()

# ── node_role domain ──────────────────────────────────────────────────────────────────────────────
VALUE = "value"   # a user memory: an arbitrary value at an unconstrained/scalar slot
NAME = "name"     # a proper name (also_known_as/pref_name subject) — a value-side memory
PLACE = "place"   # an L4 type node (object of instance_of/subclass_of or a constrained-typed rel)
BOTH = "both"     # OWL punning — legitimately value AND place; value protection STILL applies

# Roles on the VALUE side of the firewall — the set every value-consuming guard protects.
_PROTECTED_ROLES = frozenset({VALUE, NAME, BOTH})

# The canonical SKOS naming rels whose SUBJECT is a NAME (a memory). Same fixed pair the rest of the
# codebase pins (main._ALIAS_BACKED_NAME_RELS, embedder._NAMING_RELS); NOT a growable ontology axis.
_NAMING_RELS = ("also_known_as", "pref_name")

_FLAG = "VALUE_PLACE_FIRST_CLASS"


def enabled() -> bool:
    """True when the first-class value/place property is switched ON. Pure ``os.getenv`` (default
    OFF) — when OFF every read short-circuits to 'not protected' and every write is a no-op, so the
    codebase behaves byte-for-byte as it does today (the per-layer guards run unchanged)."""
    return os.getenv(_FLAG, "false").strip().lower() not in ("0", "false", "no", "off")


# ── derivation (subject-agnostic, metadata-driven) ──────────────────────────────────────────────────

def _read_tail_types(db_conn, rel_type: str) -> set[str] | None:
    """Read a rel's ``tail_types`` from the TENANT-bound ``db_conn`` (its search_path scopes to the
    per-tenant schema, so a GROWN per-tenant rel resolves — a public connection would miss it).
    Returns the upper-cased tail set, or **None** when the rel is UNKNOWN (missing row / error /
    missing table). SAVEPOINT-guarded so a lookup miss never poisons the in-flight ingest txn.
    None (unknown) is the AMBIGUOUS signal — the caller declines to stamp rather than guess."""
    rt = (rel_type or "").lower().strip()
    if not rt or db_conn is None:
        return None
    row = None
    try:
        with db_conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_node_role_tail")
            try:
                cur.execute("SELECT tail_types FROM rel_types WHERE rel_type = %s LIMIT 1", (rt,))
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_node_role_tail")
            except Exception:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_node_role_tail")
                return None
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    return {str(t).strip().upper() for t in (row[0] or []) if str(t).strip()}


def _tail_files_a_value(tail: set[str] | None) -> bool:
    """True when a KNOWN ``tail`` files its object as an arbitrary VALUE, not a typed KIND —
    UNCONSTRAINED (empty or only the ``ANY`` wildcard: favorite_colour/likes/prefers/dislikes) OR
    SCALAR (an address/ip/age literal). A CONSTRAINED kind set ({Animal}/{Object}/{Concept,emotion}/…)
    is a genuine typed kind → NOT a value (owns→car→vehicle, feels→worried→emotion keep laddering).
    ``tail`` None (unknown rel) → False (the caller treats unknown separately)."""
    if tail is None:
        return False
    return (not tail) or tail == {"ANY"} or ("SCALAR" in tail and tail <= {"SCALAR", "ANY"})


def derive_object_role(db_conn, rel_type: str | None, provenance: str | None) -> str | None:
    """Derive the role an edge's OBJECT node should carry from the edge binding — the single place the
    value/place predicate is computed for an object.

    - ``instance_of``/``subclass_of`` object → PLACE (a declared type binding).
    - a ``user_stated`` edge whose rel files an arbitrary value (unconstrained/scalar tail) → VALUE.
    - a constrained-typed rel's object (owns/has_pet/feels…) → PLACE (a genuine typed kind, laddered).
    - anything AMBIGUOUS (an unknown rel; or a value-filing slot bound by ENGINE growth, not the user)
      → None (no opinion; leave the node unstamped so it falls to structural re-derivation).

    Deterministic, metadata-driven (no rel/value literal beyond the closed structural hierarchy rels
    instance_of/subclass_of — W3C-canonical seeds, not a growable axis). None on ambiguity is the
    fail-safe: an unstamped node is never over-suppressed from laddering — the read guard treats it
    as 'not protected'."""
    rt = (rel_type or "").lower().strip()
    if not rt:
        return None
    if rt in ("instance_of", "subclass_of"):
        return PLACE
    tail = _read_tail_types(db_conn, rt)
    if tail is None:
        return None  # unknown rel → ambiguous, do not guess
    if _tail_files_a_value(tail):
        # A value-filing slot is a MEMORY only under USER authority; an engine-inferred edge there is
        # ambiguous (could be growth) → None, never a stamped value.
        return VALUE if (provenance or "").lower().strip() == "user_stated" else None
    # A KNOWN constrained-tail typed rel → a place-eligible typed kind (keeps laddering).
    return PLACE


# ── monotonic authority-ordered transition ──────────────────────────────────────────────────────────

def _merge_role(current: str | None, incoming: str | None) -> str | None:
    """Apply the MONOTONIC, authority-ordered transition (DESIGN §3 table). A ``value``/``name`` node
    never silently becomes a bare ``place`` — a place binding on it yields ``both`` (punning; value
    protection stays). A value binding on a prior ``place`` node → ``both`` (user wins). Growth never
    demotes user truth. Pure function; None incoming leaves current unchanged."""
    if incoming is None:
        return current
    if current is None:
        return incoming
    if current == incoming:
        return current
    # NAME and VALUE are both value-side; a node stamped one then bound the other stays value-side.
    value_side = {VALUE, NAME}
    if current in value_side and incoming in value_side:
        return current  # keep the first value-side role (name/value distinction is preserved)
    if current == BOTH or incoming == BOTH:
        return BOTH
    # value-side ↔ place → punning cell (protection stays). Covers both directions.
    if (current in value_side and incoming == PLACE) or (current == PLACE and incoming in value_side):
        return BOTH
    return current


# ── persistence (SAVEPOINT-guarded, column-missing-safe) ────────────────────────────────────────────

def read_role(db_conn, node_id: str) -> str | None:
    """Read the persisted ``entities.node_role`` for a node. None on unstamped / missing column /
    error (safe default). SAVEPOINT-guarded so a missing-column error never poisons the txn."""
    if db_conn is None or not node_id:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_node_role_read")
            try:
                cur.execute("SELECT node_role FROM entities WHERE id = %s LIMIT 1", (str(node_id),))
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_node_role_read")
            except Exception:  # noqa: BLE001 — column may not exist on a pre-migration tenant
                cur.execute("ROLLBACK TO SAVEPOINT sp_node_role_read")
                return None
    except Exception:  # noqa: BLE001
        return None
    return (row[0] if row and row[0] else None)


def _is_naming_subject(db_conn, node_id: str) -> bool:
    """True when ``node_id`` is the SUBJECT of a committed naming edge (also_known_as/pref_name) — a
    proper NAME (a memory). The structural name signal every name-guard in the codebase already uses;
    folded into the effective role so names are covered even when the column is unstamped. SAVEPOINT-
    guarded; fail-safe False."""
    if db_conn is None or not node_id:
        return False
    try:
        with db_conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_node_role_name")
            try:
                cur.execute(
                    "SELECT 1 FROM facts"
                    " WHERE subject_id = %s AND rel_type = ANY(%s)"
                    "   AND superseded_at IS NULL AND archived_at IS NULL"
                    " UNION ALL"
                    " SELECT 1 FROM staged_facts"
                    " WHERE subject_id = %s AND rel_type = ANY(%s)"
                    "   AND promoted_at IS NULL AND deleted_at IS NULL"
                    " LIMIT 1",
                    (str(node_id), list(_NAMING_RELS), str(node_id), list(_NAMING_RELS)),
                )
                hit = cur.fetchone() is not None
                cur.execute("RELEASE SAVEPOINT sp_node_role_name")
                return hit
            except Exception:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_node_role_name")
                return False
    except Exception:  # noqa: BLE001
        return False


def stamp_node_role(db_conn, node_id: str, role: str | None) -> str | None:
    """Stamp ``node_id`` with ``role`` under the MONOTONIC transition. No-op (returns None) when the
    flag is OFF, the role is None, or the node is the user/'user'. Reads the current stamp, merges
    per authority order, writes back only on a change. SAVEPOINT-guarded + column-missing-safe; never
    raises, never blocks the commit. Returns the resulting role (or None on no-op/error)."""
    if not enabled() or db_conn is None or not node_id or role is None:
        return None
    if str(node_id) in ("user", ""):
        return None
    current = read_role(db_conn, node_id)
    merged = _merge_role(current, role)
    if merged is None or merged == current:
        return current
    try:
        with db_conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_node_role_write")
            try:
                cur.execute(
                    "UPDATE entities SET node_role = %s WHERE id = %s",
                    (merged, str(node_id)),
                )
                cur.execute("RELEASE SAVEPOINT sp_node_role_write")
            except Exception:  # noqa: BLE001 — column may not exist on a pre-migration tenant
                cur.execute("ROLLBACK TO SAVEPOINT sp_node_role_write")
                return None
    except Exception:  # noqa: BLE001
        return None
    log.info("node_role.stamped", node=str(node_id)[:16], role=merged,
             prior=(current or "null"))
    return merged


def stamp_edge_object(db_conn, node_id: str, rel_type: str | None, provenance: str | None) -> str | None:
    """Convenience stamp at an ingest object-placement seam: derive the object's role from the
    in-flight edge and apply it. No-op when the flag is OFF. Returns the resulting role or None."""
    if not enabled():
        return None
    return stamp_node_role(db_conn, node_id, derive_object_role(db_conn, rel_type, provenance))


# ── the ONE read every consumer calls ──────────────────────────────────────────────────────────────

def effective_role(
    db_conn,
    node_id: str,
    *,
    inflight_rel_type: str | None = None,
    inflight_provenance: str | None = None,
) -> str | None:
    """The effective role of a node = authority-max of (persisted stamp) ∪ (in-flight binding hint) ∪
    (naming-edge membership). The single centralized read — replaces the 30+ scattered re-derivations.

    - persisted: ``entities.node_role`` (covers the ASYNC tier that has no in-flight edge).
    - in-flight: derive from a same-transaction edge's rel_type+provenance (preserves the ladder
      guard's ordering-independence — the row may not be stamped yet at ingest time).
    - naming: a committed also_known_as/pref_name subject is a NAME (covers names with no new write).

    Returns None when the flag is OFF (callers treat None as 'not protected' → their existing guard
    runs). Fail-safe by construction (each sub-read returns a safe default on error)."""
    if not enabled():
        return None
    role = read_role(db_conn, node_id)
    if inflight_rel_type is not None:
        role = _merge_role(role, derive_object_role(db_conn, inflight_rel_type, inflight_provenance))
    if role not in _PROTECTED_ROLES and _is_naming_subject(db_conn, node_id):
        role = _merge_role(role, NAME)
    return role


def is_protected_value(
    db_conn,
    node_id: str,
    *,
    inflight_rel_type: str | None = None,
    inflight_provenance: str | None = None,
) -> bool:
    """THE consumer-facing predicate: True iff ``node_id``'s effective role is on the VALUE side of
    the firewall (value/name/both) → the node is a MEMORY and must NOT be laddered/typed/superseded
    by an engine-inferred type. Returns False when the flag is OFF (→ the consumer's existing
    per-layer guard runs unchanged) or on any error (never over-suppress laddering)."""
    if not enabled():
        return False
    return effective_role(
        db_conn, node_id,
        inflight_rel_type=inflight_rel_type,
        inflight_provenance=inflight_provenance,
    ) in _PROTECTED_ROLES
