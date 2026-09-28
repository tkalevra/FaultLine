"""canonicalize-at-capture — the ONE helper for the INDEX (never the TRUTH) operation.

See the internal design record. Canonicalization collapses surface variants onto a
single concept node so a walk that grounds ANY surface reaches the members hanging off all of
them. It is an **INDEX / reachability** operation, NEVER a truth one:

  * The engine/canonical form (WordNet co-synset lemma, head-noun reduction) registers as a
    **NON-preferred** alias — findability only. It is NEVER what recall says back.
  * The USER's own surface stays **preferred** and is what ``convert_to_prose`` renders — the
    helper never overwrites or demotes it.
  * Corrections win via the EXISTING preference-rank ladder (``registry._PREFERENCE_RANK``,
    ``user > seed > growth``). This helper adds NO new authority logic.
  * THE HARD LINE: the caller passes only common-noun TYPE-node surfaces — never a NAME. A name
    is a memory filed AT a place; canonicalization operates on the PLACE (the type/index).
    **This invariant is no longer only documented.** It is enforced downstream at the ONE choke
    point every alias weld flows through — ``EntityRegistry.register_alias`` →
    ``src/entity_registry/weld_guard.py`` — because production measurement showed the violating
    welds arrive through OTHER callers of ``register_alias``, not through this wrapper, so a
    guard placed here would have covered one path out of several. See
    the internal design record.

STEP-1 ROLLOUT (behavior-preserving refactor): this is a pure wrapper over the exact
``EntityRegistry.register_alias`` template used inline by the cross-synonym unification site
(``main.py:7031-7034``). The invariant is HARDWIRED — ``is_preferred=False`` is forced and there
is no parameter to override it, so an engine form can never be registered preferred through this
door and can never demote a ``user_stated`` surface (rank 5) with its ``inferred`` rank (3).

Deterministic, subject-agnostic. NOT fail-safe by itself: exactly like the inline template, a
``register_alias`` failure PROPAGATES so the CALLER's existing rollback / search_path re-bind
handles it (byte-identical to today) — the caller owns the transaction, never this helper.
"""
from __future__ import annotations


def canonicalize_alias(registry, node_uuid, surfaces, *, preference_source="lexical"):
    """Register each engine/canonical SURFACE as a NON-preferred alias of ``node_uuid``.

    ``is_preferred=False`` is HARDWIRED (no override) — the load-bearing invariant. Copies the
    ``main.py:7031-7034`` template exactly: ``register_alias(str(node_uuid), surface,
    is_preferred=False, preference_source="inferred")``.

    Args:
      registry         : an ``EntityRegistry`` (per-tenant, schema-bound by the caller).
      node_uuid        : the concept node UUID the surfaces are aliases OF.
      surfaces         : iterable of engine/canonical surface strings (TYPE-node lemmas).
      preference_source: alias provenance (default ``"lexical"``). NEVER ``user_stated``.
                         ``lexical`` ranks EQUAL to ``inferred`` (``registry._PREFERENCE_RANK``)
                         so this lane's display trust is byte-for-byte what it always was; what
                         the distinct source records is the WARRANT — shared WordNet synset
                         membership is a stated, checkable licence for the co-reference this
                         registration asserts. ``registry._WARRANTED_SOURCES`` is read by the
                         arrival weld guard's ARM 3, which refuses an UNWARRANTED growth
                         co-reference claim; without this recorded warrant that arm could not
                         tell this lane apart from an extraction-invented weld, because both
                         used to arrive as ``inferred``. See the internal design record §7.

    Returns the number of aliases registered. Propagates any ``register_alias`` exception to the
    caller (which owns rollback), preserving the inline site's behavior byte-for-byte.
    """
    if not node_uuid or not surfaces:
        return 0
    registered = 0
    for surface in surfaces:
        if not surface:
            continue
        # INVARIANT: is_preferred is FORCED False here — engine growth is findability-only and can
        # never clobber the user's preferred surface (registry preference-rank guard does the rest).
        registry.register_alias(str(node_uuid), surface, is_preferred=False,
                                preference_source=preference_source)
        registered += 1
    return registered
