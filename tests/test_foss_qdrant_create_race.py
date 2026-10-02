"""#154: losing the Qdrant collection-create race on a fresh boot is success, not an ERROR.

The API lifespan and the re-embedder (both started by docker-entrypoint.sh) each create the
default collection on empty volumes; the loser's PUT answered 409 and logged
``collection_create_failed`` + ``startup.qdrant_collection_failed`` as ERROR on every install.
"""
from __future__ import annotations

import types

import pytest


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


_GOOD = {"result": {"config": {"params": {"vectors": {"size": 768, "distance": "Cosine"}}}}}
_WRONG = {"result": {"config": {"params": {"vectors": {}}}}}


@pytest.fixture()
def emb(monkeypatch):
    from src.re_embedder import embedder
    from src.api import qdrant_partition
    monkeypatch.setattr(qdrant_partition, "shared_mode", lambda: False)
    # First existence check: not there yet (the other process is about to create it).
    monkeypatch.setattr(embedder, "_http_client", types.SimpleNamespace(get=lambda *a, **k: _Resp(404)))
    monkeypatch.setattr(embedder.httpx, "put", lambda *a, **k: _Resp(409))
    return embedder


def test_409_with_the_right_schema_is_success(emb, monkeypatch, caplog):
    monkeypatch.setattr(emb.httpx, "get", lambda *a, **k: _Resp(200, _GOOD))
    with caplog.at_level("INFO"):
        assert emb.ensure_collection("faultline", "http://qdrant:6333") is True
    assert not [r for r in caplog.records if r.levelname == "ERROR"], "a lost race is not an error"


def test_409_with_a_foreign_schema_still_fails_loud(emb, monkeypatch):
    monkeypatch.setattr(emb.httpx, "get", lambda *a, **k: _Resp(200, _WRONG))
    assert emb.ensure_collection("faultline", "http://qdrant:6333") is False


def test_409_while_the_winner_is_still_creating(emb, monkeypatch, caplog):
    """Measured on a fresh install: right after our 409, Qdrant answered the GET with 500 while
    the winning create was still in flight; the collection appears a moment later."""
    answers = [_Resp(500), _Resp(404), _Resp(200, _GOOD)]
    monkeypatch.setattr(emb.httpx, "get", lambda *a, **k: answers.pop(0) if answers else _Resp(200, _GOOD))
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    with caplog.at_level("INFO"):
        assert emb.ensure_collection("faultline", "http://qdrant:6333") is True
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_409_and_the_collection_never_appears_fails_loud(emb, monkeypatch):
    monkeypatch.setattr(emb.httpx, "get", lambda *a, **k: _Resp(404))
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    assert emb.ensure_collection("faultline", "http://qdrant:6333") is False
