from .gate import WGMValidationGate, SEED_ONTOLOGY


def validate_edge(subject_id, obj_id, rel_type, db_conn=None) -> dict:
    """
    Module-level convenience wrapper for WGMValidationGate.
    For unit tests that don't need a real DB, pass db_conn=None and
    provide a mock via the WGMValidationGate class directly.
    """
    gate = WGMValidationGate(db_conn)
    return gate.validate_edge(subject_id, obj_id, rel_type)


def store_pending_type(entity_data):
    """Stub: Insert into pending_types table"""
    pass


def flag_conflict(alert_webhook, edge_data):
    """Stub: Trigger alert for CONFLICT_FLAGGED"""
    pass
