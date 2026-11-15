"""env_drift_check pins — the env-drift ledger gate (issue #231).

Bars straight from the issue:
  * plant an override WITHOUT a ledger row  -> flagged (exit 1)
  * plant an override WITH a matching row   -> passes (exit 0)
  * secrets NEVER printed: output redacts values matching key/token/password
    patterns (a raw leak through a pre-baked violation sentence was caught by this
    lane's own smoke test and is pinned here — see test_redaction_end_to_end)
  * the defaults universe is IMPORTED FROM SOURCE (live read at call time), not
    re-hardcoded in the tool
  * the shipped ops/env-ledger.json carries exactly the authorized current-prod rows

Runtime note: importing src.api.main (IngestTimeouts table) costs ~19s in a fresh
interpreter; the CLI kill-path subprocess tests each pay it once — the whole file runs
in ~90s, dominated by those imports, not by the checks.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO), str(REPO / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import env_drift_check as EDC  # noqa: E402  (needs the sys.path inserts above)

TOOL = REPO / "tools" / "env_drift_check.py"
LEDGER = REPO / "ops" / "env-ledger.json"

SECRET_SK = "sk-abc123SECRETxyz456789"
SECRET_NAME_VAL = "hunter2-name-value"


@pytest.fixture(scope="module")
def universe():
    return EDC.load_universe()


@pytest.fixture()
def tmp_ledger(tmp_path):
    def _write(rows):
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({"schema": 1, "rows": rows}))
        return path

    return _write


@pytest.fixture()
def tmp_env(tmp_path):
    def _write(lines):
        path = tmp_path / "env.txt"
        path.write_text("\n".join(lines) + "\n")
        return path

    return _write


def _row(variable, value, **kw):
    row = {
        "variable": variable,
        "value": value,
        "rationale": kw.get("rationale", "test rationale"),
        "date": kw.get("date", "2026-10-07"),
        "owner": kw.get("owner", "tester"),
    }
    row.update({k: v for k, v in kw.items() if k not in row})
    return row


# ── the defaults-import shape ───────────────────────────────────────────────────


def test_universe_imported_from_source_not_hardcoded(universe):
    """The universe is the four env-overridable _DEFAULTS tables read LIVE from the
    source — not a copy baked into the tool. Proven by mutating a table AFTER import
    and rebuilding: the tool must see the change."""
    from src.api import llm_calls as LC
    from src.api import main as M

    # known members present, defaults taken from the source tables
    assert universe["LLM_TIMEOUT_REFRAME"].default == LC.LLMTimeouts._DEFAULTS["REFRAME"]
    assert universe["LLM_MAX_TOKENS_EXTRACT"].default == LC.LLMMaxTokens._DEFAULTS["EXTRACT"]
    assert universe["INGEST_TIMEOUT_LOCK_WAIT"].default == M.IngestTimeouts._DEFAULTS["LOCK_WAIT"]

    # live read: a key added to the table after the tool module was imported must
    # appear in a freshly built universe (a re-hardcoded copy could not)
    LC.LLMTimeouts._DEFAULTS["SYNTHTEST"] = 11.0
    try:
        fresh = EDC.load_universe()
        assert "LLM_TIMEOUT_SYNTHTEST" in fresh
        assert fresh["LLM_TIMEOUT_SYNTHTEST"].default == 11.0
    finally:
        del LC.LLMTimeouts._DEFAULTS["SYNTHTEST"]
    assert "LLM_TIMEOUT_SYNTHTEST" not in EDC.load_universe()


def test_universe_covers_all_four_tables(universe):
    tables = {spec.table for spec in universe.values()}
    assert tables == {
        "src/api/llm_calls.py::LLMTimeouts._DEFAULTS",
        "src/api/llm_calls.py::LLMMaxTokens._DEFAULTS",
        "src/mcp/server.py::_BRAIN_TIMEOUT_DEFAULTS",
        "src/api/main.py::IngestTimeouts._DEFAULTS",
    }


# ── the flag/pass bars ──────────────────────────────────────────────────────────


def test_plant_override_without_row_flagged(universe, tmp_env, tmp_ledger):
    """THE WOUND, verbatim: planted LLM_TIMEOUT_REFRAME=30 (the archaeology value),
    empty ledger -> unrationaled_override violation."""
    env = tmp_env(["LLM_TIMEOUT_REFRAME=30"])
    report = EDC.check_drift(EDC.read_env_file(env), universe, [])
    assert [v["kind"] for v in report["violations"]] == ["unrationaled_override"]
    assert report["violations"][0]["variable"] == "LLM_TIMEOUT_REFRAME"
    assert report["violations"][0]["value"] == "30"


def test_plant_override_with_row_passes(universe, tmp_env, tmp_ledger):
    env = tmp_env(["LLM_TIMEOUT_REFRAME=8", "LLM_TIMEOUT_ENRICHMENT=6"])
    rows = [
        _row("LLM_TIMEOUT_REFRAME", "8"),        # deployed "8" vs ledger "8"
        _row("LLM_TIMEOUT_ENRICHMENT", "6.0"),   # numeric-aware: "6.0" covers "6"
    ]
    report = EDC.check_drift(EDC.read_env_file(env), universe, rows)
    assert report["violations"] == []
    statuses = {o["variable"]: o["status"] for o in report["overrides"]}
    assert statuses == {"LLM_TIMEOUT_REFRAME": "ledgered", "LLM_TIMEOUT_ENRICHMENT": "ledgered"}


def test_ledger_value_mismatch_is_unrationaled_drift(universe, tmp_env, tmp_ledger):
    """A row for the right var but the WRONG value does not cover the deployed value:
    the rationale is bound to what was observed, not to the variable name."""
    env = tmp_env(["LLM_TIMEOUT_REFRAME=30"])
    rows = [_row("LLM_TIMEOUT_REFRAME", "8")]
    report = EDC.check_drift(EDC.read_env_file(env), universe, rows)
    assert [v["kind"] for v in report["violations"]] == ["ledger_value_mismatch"]
    assert report["violations"][0]["ledger_values"] == ["8"]


def test_set_to_default_is_not_drift(universe, tmp_env, tmp_ledger):
    env = tmp_env(["LLM_TIMEOUT_EXTRACTION=30.0"])  # default is 30.0 — redundant echo
    report = EDC.check_drift(EDC.read_env_file(env), universe, [])
    assert report["violations"] == []
    assert [a["kind"] for a in report["advisories"]] == ["set_to_default"]


def test_stale_ledger_row_is_advisory(universe, tmp_env, tmp_ledger):
    env = tmp_env(["LLM_TIMEOUT_REFRAME=8"])
    rows = [_row("LLM_TIMEOUT_REFRAME", "8"), _row("LLM_TIMEOUT_ENRICHMENT", "6")]
    report = EDC.check_drift(EDC.read_env_file(env), universe, rows)
    assert report["violations"] == []
    kinds = [a["kind"] for a in report["advisories"]]
    assert kinds == ["stale_ledger_row"]
    assert report["advisories"][0]["variable"] == "LLM_TIMEOUT_ENRICHMENT"


# ── redaction (the leak pin) ────────────────────────────────────────────────────


def test_redact_value_shapes():
    # value-shape arms
    assert EDC.redact_value("ANY_NAME", SECRET_SK) == EDC.REDACTED
    assert EDC.redact_value("ANY_NAME", "eyJabc12345678.cdEfGh.ijk") == EDC.REDACTED
    assert EDC.redact_value("ANY_NAME", "flu_b064_DEADBEEFdeadbeef1234") == EDC.REDACTED
    assert EDC.redact_value("ANY_NAME", "a" * 40) == EDC.REDACTED               # entropy blob
    assert EDC.redact_value("ANY_NAME", "f" * 64) == EDC.REDACTED               # sha256 hex
    # name-segment arm (MCP_API_KEY masked; LLM_MAX_TOKENS_* is NOT — TOKENS != TOKEN)
    assert EDC.redact_value("MCP_API_KEY", "anything") == EDC.REDACTED
    assert EDC.redact_value("SOME_TOKEN", "anything") == EDC.REDACTED
    assert EDC.redact_value("LLM_MAX_TOKENS_EXTRACT", "2048") == "2048"
    # legit values render verbatim (numeric budgets never match secret shapes)
    assert EDC.redact_value("LLM_TIMEOUT_REFRAME", "8") == "8"
    # blind free-text pass: a credential PREFIX inside a longer string redacts it all
    assert EDC.redact_text(f"leading prose then {SECRET_SK} then more") == EDC.REDACTED


def test_redaction_end_to_end(tmp_env, tmp_ledger, capsys):
    """The lane's own smoke test caught a real leak: the raw sk- value rode a
    pre-baked violation 'detail' sentence past the redactor (anchored whole-string
    patterns missed it). This pin drives the FULL pipeline (env-file -> check ->
    render, text and JSON) with a secret-shaped override value and asserts the raw
    bytes never appear in output."""
    env = tmp_env([
        f"LLM_TIMEOUT_REFRAME={SECRET_SK}",
        f"MCP_API_KEY={SECRET_NAME_VAL}",   # non-universe: parsed but never rendered
    ])
    ledger = tmp_ledger([])
    for argv in (
        ["--env-file", str(env), "--ledger", str(ledger)],
        ["--env-file", str(env), "--ledger", str(ledger), "--json"],
    ):
        rc = EDC.main(argv)
        assert rc == EDC.EXIT_DRIFT
        out = capsys.readouterr().out
        assert SECRET_SK not in out, "raw credential-shaped value leaked to output"
        assert SECRET_NAME_VAL not in out
        assert EDC.REDACTED in out


# ── CLI kill-path (exit codes at the standalone door) ───────────────────────────


def test_cli_exit_codes(tmp_env, tmp_ledger):
    """The tool's contract is its EXIT CODE: non-zero on unrationaled drift, zero when
    ledged, exit 2 (not silent success) on operational failure."""
    env_planted = tmp_env(["LLM_TIMEOUT_REFRAME=30"])
    env_ledged = tmp_env(["LLM_TIMEOUT_REFRAME=8"])

    r_bad = subprocess.run(
        [sys.executable, str(TOOL), "--env-file", str(env_planted),
         "--ledger", str(tmp_ledger([]))],
        capture_output=True, text=True, timeout=180,
    )
    assert r_bad.returncode == 1, r_bad.stdout + r_bad.stderr
    assert "LLM_TIMEOUT_REFRAME" in r_bad.stdout

    r_ok = subprocess.run(
        [sys.executable, str(TOOL), "--env-file", str(env_ledged),
         "--ledger", str(tmp_ledger([_row("LLM_TIMEOUT_REFRAME", "8")]))],
        capture_output=True, text=True, timeout=180,
    )
    assert r_ok.returncode == 0, r_ok.stdout + r_ok.stderr

    r_op = subprocess.run(
        [sys.executable, str(TOOL), "--env-file", "/nonexistent/env.txt",
         "--ledger", str(LEDGER)],
        capture_output=True, text=True, timeout=180,
    )
    assert r_op.returncode == 2, r_op.stdout + r_op.stderr
    assert "OPERATIONAL FAILURE" in r_op.stderr


def test_malformed_ledger_is_operational_failure(tmp_ledger):
    """A ledger that cannot parse means rationales cannot be verified — exit-2 loud,
    never a vacuous clean."""
    bad = tmp_ledger([{"variable": "X", "value": "1"}])  # missing rationale/date/owner
    with pytest.raises(EDC.OperationalError):
        EDC.load_ledger(bad)


# ── the shipped artifact ────────────────────────────────────────────────────────


def test_shipped_ledger_rows(universe):
    """ops/env-ledger.json carries exactly the authorized current-prod rows
    (issue #231: REFRAME=8, ENRICHMENT=6, the 2026-10-07 NVIDIA degradation window),
    fully populated — and both ARE drift against the code defaults (necessary rows,
    not decoration)."""
    rows = EDC.load_ledger(LEDGER)
    got = {r["variable"]: r for r in rows}
    assert set(got) == {"LLM_TIMEOUT_REFRAME", "LLM_TIMEOUT_ENRICHMENT"}
    assert got["LLM_TIMEOUT_REFRAME"]["value"] == "8"
    assert got["LLM_TIMEOUT_ENRICHMENT"]["value"] == "6"
    for row in rows:
        for key in EDC._REQUIRED_ROW_KEYS:
            assert row[key].strip()
    assert "NVIDIA degradation window" in got["LLM_TIMEOUT_REFRAME"]["rationale"]
    # each shipped row covers a REAL override (value differs from the code default)
    for row in rows:
        spec = universe[row["variable"]]
        assert not EDC._values_equal(row["value"], spec.default)
