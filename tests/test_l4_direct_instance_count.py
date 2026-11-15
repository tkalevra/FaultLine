"""Regression: direct L4 count(*) for "how many <type>" — count DISTINCT instance_of the type
straight from the store, not the fragile walk snapshot.

Live-confirmed bug: three (item, instance_of, vase) edges exist and a direct SQL count(*) returns 3,
but the walk-snapshot count_over_walk returned 0 (the item->type edges sit one hop past the anchor and
never entered the snapshot). _l4_direct_instance_count reads L4 directly. Fake cursor dispatches on the
SQL shape (avoids the dead-mock trap).
"""
import pytest
from src.api import main as m

VASE = "vase-type-uuid"
INST = ["a-uuid", "b-uuid", "c-uuid"]


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        # the UNION direct-count query: facts + staged, DISTINCT subject_id where object_id=ANY, rel=ANY
        if "FROM facts" in s and "object_id = ANY" in s and "rel_type = ANY" in s and "UNION" in s:
            self._result = [(r,) for r in self._rows]
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)


def test_direct_l4_count_counts_distinct_instances():
    out = m._l4_direct_instance_count(FakeDB(INST), {VASE}, {"instance_of", "is_a"})
    assert out["miss"] is False and out["held"] is False
    assert out["count"] == 3
    assert set(out["instance_ids"]) == set(INST)


def test_direct_l4_count_empty_type_is_miss():
    out = m._l4_direct_instance_count(FakeDB(INST), set(), {"instance_of"})
    assert out["miss"] is True and out["count"] == 0


def test_direct_l4_count_no_instances_is_zero_not_miss():
    out = m._l4_direct_instance_count(FakeDB([]), {VASE}, {"instance_of"})
    assert out["miss"] is False and out["count"] == 0


def test_direct_l4_count_failsafe_on_db_error():
    class Boom:
        def cursor(self):
            raise RuntimeError("db down")
    out = m._l4_direct_instance_count(Boom(), {VASE}, {"instance_of"})
    assert out["miss"] is True and out["count"] == 0  # fail-safe: miss, never a fabricated number
