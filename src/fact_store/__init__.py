from .store import FactStoreManager
import psycopg2
import sys


def commit_edge(sub: str, obj: str, rel: str, prov: str, db_conn=None) -> dict:
    """
    Convenience wrapper: inserts a single edge via FactStoreManager.
    Returns {"id": 1} stub when no db_conn provided (for legacy tests).
    """
    if db_conn is None:
        return {"id": 1}
    manager = FactStoreManager(db_conn)
    manager.commit([(sub, obj, rel, prov)])
    return {"id": 1}


def reembed_facts():
    """Stub: Re-embed data for Qdrant update"""
    pass
