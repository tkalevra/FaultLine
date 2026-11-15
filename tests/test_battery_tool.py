"""Unit tests for tools/battery.py — spec parser + check evaluator (no live calls)."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


def _battery():
    spec = importlib.util.spec_from_file_location("battery_tool", REPO / "tools" / "battery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _battery()


def _write_spec(tmp_path, data):
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


GOOD = {
    "subject": "t",
    "steps": [
        {"name": "s1", "tool": "remember_facts", "text": "hello",
         "check": {"kind": "status_is", "value": "stored"}, "sleep_after_s": 0},
        {"name": "s2", "tool": "recall_memory", "query": "where?",
         "check": {"kind": "contains", "needle": "toronto"}},
    ],
}


class TestLoadSpec:
    def test_good_spec_roundtrip_with_defaults(self, tmp_path):
        data = {"subject": "fam", "steps": [
            {"name": "a", "tool": "learn_facts", "text": "learn law",
             "check": {"kind": "contains", "needle": "ok"}}]}
        spec = B.load_spec(_write_spec(tmp_path, data))
        assert spec["subject"] == "fam"
        step = spec["steps"][0]
        assert step["sleep_after_s"] == 0
        assert step["timeout_s"] is None

    def test_missing_file_raises(self):
        with pytest.raises(B.SpecError):
            B.load_spec("/nonexistent/spec.yaml")

    def test_missing_steps_raises(self, tmp_path):
        with pytest.raises(B.SpecError):
            B.load_spec(_write_spec(tmp_path, {"subject": "x"}))

    def test_bad_tool_raises(self, tmp_path):
        data = {"steps": [{"name": "a", "tool": "teleport", "text": "x",
                           "check": {"kind": "contains", "needle": "y"}}]}
        with pytest.raises(B.SpecError, match="tool"):
            B.load_spec(_write_spec(tmp_path, data))

    def test_text_or_query_required(self, tmp_path):
        data = {"steps": [{"name": "a", "tool": "remember_facts",
                           "check": {"kind": "status_is", "value": "stored"}}]}
        with pytest.raises(B.SpecError, match="text"):
            B.load_spec(_write_spec(tmp_path, data))

    def test_contains_needs_needle(self, tmp_path):
        data = {"steps": [{"name": "a", "tool": "remember_facts", "text": "x",
                           "check": {"kind": "contains"}}]}
        with pytest.raises(B.SpecError, match="needle"):
            B.load_spec(_write_spec(tmp_path, data))

    def test_unknown_check_kind_rejected(self, tmp_path):
        data = {"steps": [{"name": "a", "tool": "remember_facts", "text": "x",
                           "check": {"kind": "vibes", "needle": "y"}}]}
        with pytest.raises(B.SpecError, match="unknown check kind"):
            B.load_spec(_write_spec(tmp_path, data))

    def test_field_is_bad_op_rejected(self, tmp_path):
        data = {"steps": [{"name": "a", "tool": "remember_facts", "text": "x",
                           "check": {"kind": "field_is", "field": "committed", "op": "approx", "value": 1}}]}
        with pytest.raises(B.SpecError, match="field_is"):
            B.load_spec(_write_spec(tmp_path, data))


class TestEvaluateCheck:
    def _ev(self, check, body):
        parsed = json.loads(body) if isinstance(body, str) else body
        return B.evaluate_check(check, 200, body if isinstance(body, str) else json.dumps(body), parsed)

    def test_contains_is_case_insensitive(self):
        ok, detail = self._ev({"kind": "contains", "needle": "TORONTO"}, '{"memory": "Your sister lives in Toronto."}')
        assert ok and "toronto" in detail

    def test_contains_miss(self):
        ok, _ = self._ev({"kind": "contains", "needle": "vancouver"}, '{"memory": "nope"}')
        assert not ok

    def test_not_contains_pins_cross_topic_bleed(self):
        body = '{"memory": "Your router is a Unifi Dream Machine. Your recipe is 800 grams of fish."}'
        ok, detail = self._ev({"kind": "not_contains", "needle": "bouillabaisse"}, body)
        assert ok and "bleed" not in detail
        ok, detail = self._ev({"kind": "not_contains", "needle": "recipe"}, body)
        assert not ok and "CROSS-TOPIC BLEED" in detail

    def test_status_is(self):
        ok, _ = self._ev({"kind": "status_is", "value": "stored"}, '{"status": "stored", "committed": 1}')
        assert ok
        ok, detail = self._ev({"kind": "status_is", "value": "stored"}, '{"status": "no_ingest"}')
        assert not ok and "no_ingest" in detail

    def test_status_is_any_of_list(self):
        ok, _ = self._ev({"kind": "status_is", "value": ["valid", "statements_dropped"]},
                         '{"status": "statements_dropped"}')
        assert ok
        ok, _ = self._ev({"kind": "status_is", "value": ["valid"]}, '{"status": "stored"}')
        assert not ok

    def test_field_is_ge_and_dotted_path(self):
        body = '{"status": "stored", "committed": 2, "result": {"count": 5}}'
        ok, _ = self._ev({"kind": "field_is", "field": "committed", "op": "ge", "value": 1}, body)
        assert ok
        ok, _ = self._ev({"kind": "field_is", "field": "result.count", "op": "ge", "value": 5}, body)
        assert ok
        ok, _ = self._ev({"kind": "field_is", "field": "committed", "op": "ge", "value": 3}, body)
        assert not ok

    def test_field_is_missing_or_nonnumeric_fails_not_crashes(self):
        ok, _ = self._ev({"kind": "field_is", "field": "absent", "op": "ge", "value": 1}, '{"status": "stored"}')
        assert not ok
        ok, _ = self._ev({"kind": "field_is", "field": "status", "op": "ge", "value": 1}, '{"status": "stored"}')
        assert not ok


class TestMarkdown:
    def test_table_shape_and_score(self):
        results = [
            {"subject": "ex", "step": "ingest", "verdict": "PASS", "request_body": {"text": "hi"},
             "response_body": json.dumps({"status": "stored"})},
            {"subject": "ex", "step": "recall", "verdict": "FAIL", "request_body": {"query": "q?"},
             "response_body": json.dumps({"memory": "No relevant facts found."}), "detail": "contains 'x'"},
        ]
        md = B.render_markdown(results)
        assert md.splitlines()[0] == "| step | asked | responded | verdict |"
        assert "| ex/ingest" in md and "| FAIL |" in md
        assert "**1/2 checks passed**" in md
