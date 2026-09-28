from .gate import WGMValidationGate, SEED_ONTOLOGY


def validate_edge(subject_id, obj_id, rel_type, db_conn=None) -> dict:
    """
    Module-level convenience wrapper for WGMValidationGate.
    For unit tests that don't need a real DB, pass db_conn=None and
    provide a mock via the WGMValidationGate class directly.
    """
    gate = WGMValidationGate(db_conn)
    return gate.validate_edge(subject_id, obj_id, rel_type)


# REMOVED 2026-08-12 — `store_pending_type(entity_data)` and `flag_conflict(alert_webhook,
# edge_data)`, both bare `pass` bodies with a "Stub:" docstring and zero callers anywhere in
# the repo. Neither behaviour is missing: `pending_types` is written by the ingest ontology
# lane and CONFLICT_FLAGGED alerts land in `system_alerts`. An empty stub that is never
# called is worse than no function — it reads as a wired seam and returns None silently if
# anyone ever does call it.
