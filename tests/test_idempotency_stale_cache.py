"""Focused tests for the idempotency stale-cache hardening.

Covers:
  - flush_idempotency_keys() deletes only the idempotent:/lock: namespaces, fail-safe.
  - _idempotency_cached_facts_present() guard branches that must re-ingest WITHOUT
    touching the DB (no schema / empty facts / non-UUID subject / scalar-only object /
    cross-version payload). The DB-corroboration path is validated live.
"""
import src.api.idempotency as idem


class _FakeRedis:
    def __init__(self, keys):
        self._store = dict(keys)
        self.raise_on_delete = False

    def scan_iter(self, match=None, count=None):
        # crude glob: only the trailing '*' form is used by the flush helper
        prefix = match[:-1] if match and match.endswith("*") else match
        for k in list(self._store):
            if prefix is None or k.startswith(prefix):
                yield k

    def delete(self, key):
        if self.raise_on_delete:
            raise RuntimeError("boom")
        self._store.pop(key, None)


def test_flush_idempotency_keys_targets_only_idempotent_namespaces(monkeypatch):
    fake = _FakeRedis({
        "idempotent:aaa": "1",
        "idempotent:bbb": "1",
        "lock:ccc": "1",
        "_embedding_model_version": "nomic-v1.5",  # unrelated — must survive
    })
    monkeypatch.setattr(idem.redis, "from_url", lambda *a, **k: fake)
    deleted = idem.flush_idempotency_keys("redis://x")
    assert deleted == 3
    assert "_embedding_model_version" in fake._store
    assert not any(k.startswith(("idempotent:", "lock:")) for k in fake._store)


def test_flush_idempotency_keys_failsafe_on_redis_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(idem.redis, "from_url", _boom)
    # Must never raise; returns 0 so a tenant wipe is never blocked by cache hygiene.
    assert idem.flush_idempotency_keys("redis://x") == 0


# --- _idempotency_cached_facts_present guard branches (no DB) ---
from src.api.main import _idempotency_cached_facts_present as _present  # noqa: E402

_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "a46a246a-c013-5184-9816-f8f1f1d68342"


def test_present_false_when_no_schema():
    assert _present(None, {"facts": [{"subject": _UUID_A, "object": _UUID_B, "rel_type": "owns"}]}) is False


def test_present_false_when_no_facts():
    assert _present("faultline_x", {"facts": []}) is False
    assert _present("faultline_x", {}) is False


def test_present_false_when_subject_not_uuid():
    # cross-version payload: subject is a display name, not a resolved UUID surrogate
    assert _present("faultline_x", {"facts": [{"subject": "diane", "object": _UUID_B, "rel_type": "owns"}]}) is False


def test_present_false_when_only_scalar_objects():
    # scalar-valued object (not a UUID) → no relational rows to corroborate → re-ingest
    assert _present("faultline_x", {"facts": [{"subject": _UUID_A, "object": "62", "rel_type": "age"}]}) is False


def test_present_false_on_malformed_fact():
    assert _present("faultline_x", {"facts": [{"subject": _UUID_A, "rel_type": "owns"}]}) is False
    assert _present("faultline_x", {"facts": ["not-a-dict"]}) is False
