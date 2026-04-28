from .store import FactStoreManager
import psycopg2
import sys


def commit_edge(sub: str, obj: str, rel: str, prov: str, db_conn=None,
                user_id: str = "anonymous", confidence: float = 1.0, source_weight: float = 1.0) -> dict:
    """
    Convenience wrapper: inserts a single edge via FactStoreManager.
    Returns {"id": 1} stub when no db_conn provided (for legacy tests).
    """
    if db_conn is None:
        return {"id": 1}
    manager = FactStoreManager(db_conn)
    manager.commit([(user_id, sub, obj, rel, prov)], confidence=confidence, source_weight=source_weight)
    return {"id": 1}


def reembed_facts():
    """Stub: Re-embed data for Qdrant update"""
    pass
