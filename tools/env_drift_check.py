#!/usr/bin/env python3
"""env_drift_check — the env-override rationale ledger gate (issue #231).

THE WOUND (measured 2026-10-07, issue #231): prod ran ``LLM_TIMEOUT_REFRAME=30`` against
a code default of 6s — an override nobody could explain, that stretched demo-day turn
times 5x. The rollback was archaeology. ``LLM_TIMEOUT_ENRICHMENT=60`` likewise. An env
override of a code default is a DEPLOYMENT DECISION, and a decision with no recorded
rationale is a wound waiting for the next person who has to touch that box.

WHAT THIS TOOL DOES: compares a DEPLOYED container's environment against the CODE
DEFAULTS TABLES imported from the source (at call time — never re-hardcoded here) and
flags every override that lacks a rationale row in ``ops/env-ledger.json``.

THE DEFAULTS TABLES (the whole universe; there are deliberately no env values in this
file beyond the table addresses):

    src/api/llm_calls.py::LLMTimeouts._DEFAULTS      -> LLM_TIMEOUT_<OP>        (seconds)
    src/api/llm_calls.py::LLMMaxTokens._DEFAULTS     -> LLM_MAX_TOKENS_<OP>     (tokens)
    src/mcp/server.py::_BRAIN_TIMEOUT_DEFAULTS       -> MCP_BRAIN_TIMEOUT_<OP>  (seconds)
    src/api/main.py::IngestTimeouts._DEFAULTS        -> INGEST_TIMEOUT_<OP>     (seconds)

Out of scope BY DESIGN: env vars whose value IS the config (``WGM_LLM_MODEL``,
``LLM_MODEL_<OP>``, ``POSTGRES_DSN``, ...) — they have no code default to drift FROM;
drift semantics only exist where a table default can be silently overridden.

SECRETS: the deployed env is read ONLY via anchored single-variable greps
(``docker exec <container> printenv | grep -E '^VAR='``) — never a dump — and EVERY
value this tool prints (text or --json) passes a credential-shape redactor first: a
value that looks like a key/token/password, or belongs to a variable NAMED like one,
renders as ***REDACTED***. Pinned by tests/test_env_drift_check.py.

EXIT CODES (the kill-path):
    0  clean — every override carries a matching ledger row
    1  UNRATIONALED DRIFT — an override with no row, or whose deployed value matches
       no row's value for that variable
    2  OPERATIONAL FAILURE — ssh / docker / env-file / ledger / defaults-import broke.
       This means "not checked", NEVER "clean"; fail loud.

DEPLOY RECIPE — PRE-DEPLOY STEP (issue #231 first ship: REPORT-ONLY, not a blocker):
    Before promoting a build, run the check against the target box:

        python3 tools/env_drift_check.py --ssh <deploy-host> [--container faultline-api]

    and paste the (redacted) output into the deploy notes. Exit 1 means either add a
    row to ops/env-ledger.json (variable / value / rationale / date / owner — write the
    row BEFORE setting the override in the deploy environment) or unset the override,
    then re-run. Exit 2 means the check itself failed — investigate; do not ship a
    "clean" you did not verify. See DEV/OPS-env-drift-ledger.md for the full ritual.

Usage:
    python3 tools/env_drift_check.py --ssh faultline-prod
    python3 tools/env_drift_check.py --ssh faultline-prod --container faultline-api --json
    python3 tools/env_drift_check.py --env-file printenv-style-dump.txt
    python3 tools/env_drift_check.py --ledger ops/env-ledger.json --env-file env.txt -v
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

REDACTED = "***REDACTED***"
EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_OPERATIONAL = 2

LEDGER_DEFAULT = REPO / "ops" / "env-ledger.json"

# Identifiers are shell-facing (ssh remote command) — validate before interpolation,
# refuse anything unexpected. Anchored + character-classed: no injection surface.
_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ── Credential redaction ────────────────────────────────────────────────────────
# Two arms, both pinned by tests/test_env_drift_check.py:
#   (a) variable NAME segment (API_KEY / SECRET / PASSWORD / ... / TOKEN / KEY as a
#       _-delimited segment — so LLM_MAX_TOKENS_REFRAME is safe, MCP_API_KEY is not);
#   (b) credential VALUE shape (sk-/rk- keys, FaultLine flk_/flu_/fl_op_ tokens, JWTs,
#       ghp_/github_pat_/xox* tokens, PEM private-key headers, 32+ chars of opaque
#       hex/base64 entropy). Numeric timeout/token-budget values never match these.
_SECRET_NAME_SEGMENT_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|SECRET|SECRETS|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|"
    r"CREDENTIALS|AUTH|TOKEN|KEY)(?:$|_)",
    re.IGNORECASE,
)
# Two shape classes:
#   SUBSTRING (unanchored — a credential PREFIX inside any larger string redacts the
#   whole string; prose never legitimately contains sk-…/flk_…/eyJ…/PEM headers);
#   WHOLE (anchored ^…$ — opaque entropy, 32+ chars of hex/base64: only redacted when
#   the ENTIRE value is entropy, so English/numeric values never false-positive).
_SECRET_SUBSTRING_RES = tuple(
    re.compile(p)
    for p in (
        r"(?:sk|rk|pk|gsk)-[A-Za-z0-9_-]{8,}",          # OpenAI-style API keys
        r"fl[ku]_[A-Za-z0-9_-]{8,}",                    # FaultLine flk_/flu_ tokens
        r"fl_op_[A-Za-z0-9_-]{8,}",                     # FaultLine operator tokens
        r"ghp_[A-Za-z0-9]{16,}",                        # GitHub PAT (classic)
        r"github_pat_[A-Za-z0-9_]{16,}",                # GitHub PAT (fine-grained)
        r"xox[bpas]-[A-Za-z0-9-]{10,}",                 # Slack tokens
        r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.",   # JWT (header.payload.…)
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",          # PEM private key
    )
)
_SECRET_WHOLE_RES = tuple(
    re.compile(p)
    for p in (
        r"^[A-Fa-f0-9]{32,}$",        # hex entropy (sha256 etc.)
        r"^[A-Za-z0-9+/=_-]{32,}$",   # opaque base64/urlsafe blob
    )
)


def _value_looks_secret(value: str) -> bool:
    return any(rx.search(value) for rx in _SECRET_SUBSTRING_RES) or any(
        rx.match(value) for rx in _SECRET_WHOLE_RES
    )


def redact_value(name: str, value) -> str:
    """Render ``value`` for output: credential-shaped values (or values of variables
    named like credentials) become ``***REDACTED***``. Applied to EVERY value this tool
    prints, text or JSON — the redaction IS the contract (issue #231)."""
    text = value if isinstance(value, str) else str(value)
    if name and _SECRET_NAME_SEGMENT_RE.search(name):
        return REDACTED
    if _value_looks_secret(text):
        return REDACTED
    return text


def redact_text(text: str) -> str:
    """Blind (name-less) pass for free text (rationales, notes): a credential-shaped
    substring anywhere in the text redacts the WHOLE string — better one over-redacted
    line than one leaked token."""
    if not isinstance(text, str):
        return str(text)
    if _value_looks_secret(text):
        return REDACTED
    return text


# ── Defaults universe — IMPORTED FROM SOURCE at call time ──────────────────────


class OperationalError(Exception):
    """The check itself could not run (ssh/ledger/defaults import). Exit 2 — fail
    loud; this is 'not checked', never 'clean'."""


@dataclass(frozen=True)
class VarSpec:
    variable: str
    default: object          # the CODE default, from the source table
    table: str               # provenance, e.g. src/api/llm_calls.py::LLMTimeouts._DEFAULTS


def load_universe() -> dict[str, VarSpec]:
    """Build the env-var universe from the env-overridable defaults tables IN THE
    SOURCE. Read live at call time (not at this module's import) so a test — or a
    future table — is seen without edits here. Never re-hardcode values in this file."""
    specs: list[tuple[str, dict, str]] = []
    try:
        from src.api.llm_calls import LLMMaxTokens, LLMTimeouts

        specs.append((
            "LLM_TIMEOUT_",
            LLMTimeouts._DEFAULTS,
            "src/api/llm_calls.py::LLMTimeouts._DEFAULTS",
        ))
        specs.append((
            "LLM_MAX_TOKENS_",
            LLMMaxTokens._DEFAULTS,
            "src/api/llm_calls.py::LLMMaxTokens._DEFAULTS",
        ))
        from src.mcp import server as _mcp_server

        specs.append((
            "MCP_BRAIN_TIMEOUT_",
            _mcp_server._BRAIN_TIMEOUT_DEFAULTS,
            "src/mcp/server.py::_BRAIN_TIMEOUT_DEFAULTS",
        ))
        from src.api.main import IngestTimeouts

        specs.append((
            "INGEST_TIMEOUT_",
            IngestTimeouts._DEFAULTS,
            "src/api/main.py::IngestTimeouts._DEFAULTS",
        ))
    except Exception as exc:  # ImportError or anything the import drags in — loud, never silent
        raise OperationalError(
            f"defaults-import failed (cannot read the code defaults tables): "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    universe: dict[str, VarSpec] = {}
    for prefix, table, provenance in specs:
        for op, default in table.items():
            var = f"{prefix}{op}"
            if not _VAR_NAME_RE.match(var):
                raise OperationalError(f"non-uppercase op key {op!r} in {provenance}")
            if var in universe:
                raise OperationalError(f"universe collision on {var} ({provenance})")
            universe[var] = VarSpec(variable=var, default=default, table=provenance)
    if not universe:
        raise OperationalError("defaults universe is empty — refusing to pass vacuously")
    return universe


def _values_equal(a, b) -> bool:
    """Numeric-aware equality: '8' == 8 == 8.0 (env strings vs table floats); anything
    non-numeric compares as stripped strings."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


# ── Env sources ─────────────────────────────────────────────────────────────────


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a printenv-style dump (``VAR=value`` lines; ``export `` prefix and
    whole-value quotes tolerated). Values are used only for the universe diff;
    anything printed passes redact_value."""
    if not path.is_file():
        raise OperationalError(f"env file not found: {path}")
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not _VAR_NAME_RE.match(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[name] = value
    return env


def _ssh_grep(host: str, remote_cmd: str, timeout: float) -> str | None:
    """One ssh round-trip. ``None`` = grep's documented not-found (var unset);
    anything else nonzero (or ssh-level failure) is an OperationalError — a transport
    blip must never read as 'no drift'."""
    try:
        proc = subprocess.run(
            ["ssh", host, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OperationalError(f"ssh to {host!r} timed out after {timeout}s") from exc
    except OSError as exc:
        raise OperationalError(f"ssh failed to execute: {exc}") from exc
    if proc.returncode == 0:
        return proc.stdout
    if proc.returncode == 1 and not proc.stdout:
        return None  # grep: no anchored match — the variable is unset
    raise OperationalError(
        f"ssh grep failed (rc={proc.returncode}): {proc.stderr.strip()[:200] or 'no stderr'}"
    )


def read_remote_env(
    host: str,
    container: str,
    variables: list[str],
    batch: bool = False,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Read the deployed container's env over ssh. READ-ONLY, and the only shape it
    ever issues is the anchored single-variable grep ``docker exec <c> printenv |
    grep -E '^VAR='`` (the never-dump-env-unfiltered rule) — one per variable by
    default; ``--batch-grep`` collapses them into ONE anchored alternation of exactly
    the universe variables (still anchored per name, still no dump)."""
    if not _SSH_HOST_RE.match(host or ""):
        raise OperationalError(f"bad ssh host {host!r}")
    if not _CONTAINER_RE.match(container or ""):
        raise OperationalError(f"bad container name {container!r}")
    for var in variables:
        if not _VAR_NAME_RE.match(var):
            raise OperationalError(f"bad variable name {var!r}")

    env: dict[str, str] = {}
    if batch:
        alternation = "|".join(variables)
        out = _ssh_grep(
            host,
            f"docker exec {container} printenv | grep -E '^({alternation})='",
            timeout,
        )
        if out:
            wanted = set(variables)
            for line in out.splitlines():
                name, _, value = line.partition("=")
                if name in wanted:
                    env[name] = value
    else:
        for var in variables:
            out = _ssh_grep(
                host, f"docker exec {container} printenv | grep -E '^{var}='", timeout
            )
            if out is not None:
                env[var] = out.splitlines()[0].partition("=")[2]
    return env


# ── Ledger ──────────────────────────────────────────────────────────────────────

_REQUIRED_ROW_KEYS = ("variable", "value", "rationale", "date", "owner")


def load_ledger(path: Path) -> list[dict]:
    """Load + shape-validate the rationale ledger. A malformed ledger is an
    OperationalError (exit 2): if rationales cannot be trusted to parse, the check
    cannot vouch for anything — fail loud, never pass vacuously."""
    if not path.is_file():
        raise OperationalError(f"ledger not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise OperationalError(f"ledger {path} is not valid JSON: {exc}") from exc
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise OperationalError(f"ledger {path}: top-level object must have a 'rows' array")
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise OperationalError(f"ledger {path} row {i}: not an object")
        for key in _REQUIRED_ROW_KEYS:
            val = row.get(key)
            if not isinstance(val, str) or not val.strip():
                raise OperationalError(
                    f"ledger {path} row {i}: missing/empty '{key}' "
                    f"(need variable/value/rationale/date/owner)"
                )
        if not _VAR_NAME_RE.match(row["variable"]):
            raise OperationalError(f"ledger {path} row {i}: bad variable {row['variable']!r}")
        if not _DATE_RE.match(row["date"]):
            raise OperationalError(f"ledger {path} row {i}: date must be YYYY-MM-DD")
        pair = (row["variable"], row["value"])
        if pair in seen:
            raise OperationalError(f"ledger {path} row {i}: duplicate row for {pair}")
        seen.add(pair)
    return rows


# ── The diff ────────────────────────────────────────────────────────────────────


def check_drift(
    env: dict[str, str], universe: dict[str, VarSpec], ledger_rows: list[dict]
) -> dict:
    """Deterministic diff: deployed env vs the IMPORTED code defaults, projected
    through the ledger. Violations = overrides with no row carrying the deployed
    value. Set-to-default echoes and stale rows are advisories (no behavior change /
    no live drift), never violations."""
    rows_by_var: dict[str, list[dict]] = {}
    for row in ledger_rows:
        rows_by_var.setdefault(row["variable"], []).append(row)

    overrides: list[dict] = []
    violations: list[dict] = []
    advisories: list[dict] = []

    for var in sorted(universe):
        spec = universe[var]
        if var not in env:
            continue
        value = env[var]
        if _values_equal(value, spec.default):
            advisories.append({
                "kind": "set_to_default",
                "variable": var,
                "value": value,
                "default": spec.default,
                "note": "set but EQUALS the code default — no behavior change, no row required",
            })
            continue
        covering = [r for r in rows_by_var.get(var, []) if _values_equal(r["value"], value)]
        entry = {
            "variable": var,
            "value": value,
            "default": spec.default,
            "table": spec.table,
        }
        if covering:
            entry["status"] = "ledgered"
            entry["rationale"] = covering[0]["rationale"]
            entry["ledger_date"] = covering[0]["date"]
            entry["ledger_owner"] = covering[0]["owner"]
            overrides.append(entry)
        else:
            entry["status"] = "unrationaled"
            overrides.append(entry)
            other = [r["value"] for r in rows_by_var.get(var, [])]
            # Structured fields only — the human sentence is built at RENDER time with
            # redact_value, so a credential-shaped value can never ride a pre-baked
            # string past the redactor (that exact leak was caught by the lane's own
            # redaction smoke test).
            violation = {
                "kind": "ledger_value_mismatch" if other else "unrationaled_override",
                "variable": var,
                "value": value,
                "default": spec.default,
            }
            if other:
                violation["ledger_values"] = other
            violations.append(violation)

    for var in sorted(rows_by_var):
        if var not in universe:
            advisories.append({
                "kind": "ledger_row_outside_universe",
                "variable": var,
                "note": "ledger row for a variable no defaults table reads anymore",
            })
        elif var not in env:
            advisories.append({
                "kind": "stale_ledger_row",
                "variable": var,
                "note": "ledger row present but the variable is not overridden here",
            })

    return {
        "universe_size": len(universe),
        "set_count": sum(1 for v in universe if v in env),
        "overrides": overrides,
        "violations": violations,
        "advisories": advisories,
    }


# ── Rendering (ALL values redacted) ─────────────────────────────────────────────


def render_json(report: dict) -> str:
    def _redact(obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(v, str):
                    out[k] = redact_value(str(k), v) if k in ("value", "default") else redact_text(v)
                else:
                    out[k] = _redact(v)
            return out
        if isinstance(obj, list):
            return [_redact(i) for i in obj]
        return obj

    return json.dumps(_redact(report), indent=2, sort_keys=False)


def render_text(report: dict) -> str:
    lines: list[str] = []
    src = report.get("source", {})
    lines.append("env-drift check — issue #231 (env overrides of code defaults)")
    lines.append(f"  source   : {src.get('mode', '?')} {src.get('detail', '')}".rstrip())
    lines.append(f"  ledger   : {report.get('ledger', '?')} ({report.get('ledger_rows', '?')} rows)")
    lines.append(
        f"  universe : {report['universe_size']} env-overridable defaults "
        f"imported from the code tables; {report['set_count']} set in the deployed env"
    )
    lines.append("")

    overrides = report["overrides"]
    lines.append(f"OVERRIDES OF CODE DEFAULTS ({len(overrides)}):")
    if not overrides:
        lines.append("  (none — deployed env matches the code defaults)")
    for o in overrides:
        value = redact_value(o["variable"], o["value"])
        default = redact_value(o["variable"], o["default"])
        flag = "LEDGERED    " if o["status"] == "ledgered" else "UNRATIONALED "
        lines.append(f"  {o['variable']} = {value}  (default {default})  [{flag}]")
        if o["status"] == "ledgered":
            lines.append(
                f"      {o['ledger_date']} {o['ledger_owner']}: {redact_text(o['rationale'])}"
            )
        else:
            lines.append(f"      from {o['table']}")
    lines.append("")

    violations = report["violations"]
    lines.append(f"VIOLATIONS ({len(violations)}):")
    if not violations:
        lines.append("  (none)")
    for v in violations:
        var = v["variable"]
        value = redact_value(var, v["value"])
        default = redact_value(var, v["default"])
        sentence = (
            f"  ! [{v['kind']}] deployed {var}={value} (code default {default}) "
            f"has no ops/env-ledger.json row"
        )
        if v.get("ledger_values"):
            covered = ", ".join(redact_value(var, lv) for lv in v["ledger_values"])
            sentence += (
                f" — ledger covers {covered}; the DEPLOYED value is unrationaled"
            )
        lines.append(sentence)
    lines.append("")

    advisories = report["advisories"]
    lines.append(f"ADVISORIES ({len(advisories)}) — informational, exit code unaffected:")
    if not advisories:
        lines.append("  (none)")
    for a in advisories:
        value = redact_value(a["variable"], a.get("value", "")) if a.get("value") else ""
        suffix = f" = {value}" if value else ""
        lines.append(f"  - [{a['kind']}] {a['variable']}{suffix} {redact_text(a.get('note', ''))}".rstrip())
    lines.append("")

    if violations:
        lines.append(
            "RESULT: DRIFT — add rows to ops/env-ledger.json (variable/value/rationale/"
            "date/owner) or unset the overrides, then re-run. See --help → DEPLOY RECIPE STEP."
        )
    else:
        lines.append("RESULT: CLEAN — every override carries a rationale row.")
    lines.append(f"exit {EXIT_DRIFT if violations else EXIT_CLEAN}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────────

_PREDEPLOY_EPILOG = """\
DEPLOY RECIPE — PRE-DEPLOY STEP (issue #231, first ship = REPORT-ONLY, not a blocker):
  Before promoting a build, run the check against the target box and paste the
  (redacted) output into the deploy notes:

      python3 tools/env_drift_check.py --ssh <deploy-host> [--container faultline-api]

  exit 0  clean — every override of a code default carries a ledger row
  exit 1  unrationaled drift — add a row to ops/env-ledger.json (variable / value /
          rationale / date / owner; write the row BEFORE setting the override) or
          unset the override, then re-run
  exit 2  the check itself failed (ssh/docker/ledger/defaults import) — this is
          "not checked", NEVER "clean"; investigate before shipping

  Secrets: the deployed env is only ever read via anchored single-variable greps and
  every printed value passes a credential-shape redactor (***REDACTED***).
  Full ritual: DEV/OPS-env-drift-ledger.md
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="env_drift_check",
        description=(
            "Compare a deployed container's env against the code defaults tables "
            "(imported from source) and flag overrides lacking a rationale row in "
            "ops/env-ledger.json. Issue #231."
        ),
        epilog=_PREDEPLOY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--ssh",
        metavar="HOST",
        help="read the deployed container env over ssh (READ-ONLY, anchored "
        "single-variable greps only — never a dump)",
    )
    src.add_argument(
        "--env-file",
        metavar="PATH",
        help="read a printenv-style dump file instead of ssh (offline / pin mode)",
    )
    ap.add_argument(
        "--container",
        default="faultline-api",
        help="container name for --ssh mode (default: %(default)s)",
    )
    ap.add_argument(
        "--ledger",
        default=str(LEDGER_DEFAULT),
        help="rationale ledger path (default: %(default)s)",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--batch-grep",
        action="store_true",
        help="one ssh round-trip with an anchored alternation of exactly the universe "
        "variables (default: one anchored single-variable grep per variable)",
    )
    ap.add_argument(
        "--ssh-timeout", type=float, default=30.0, help="per-ssh-call timeout (default: %(default)ss)"
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="echo the source command shape")
    args = ap.parse_args(argv)

    try:
        universe = load_universe()
        ledger_path = Path(args.ledger)
        ledger_rows = load_ledger(ledger_path)
        if args.env_file:
            env = read_env_file(Path(args.env_file))
            source = {"mode": "env-file", "detail": str(args.env_file)}
        else:
            if args.verbose:
                print(
                    f"[env_drift_check] reading via: ssh {args.ssh} "
                    f"'docker exec {args.container} printenv | grep -E ^<VAR>=' "
                    f"(anchored single-variable greps, one per universe variable)",
                    file=sys.stderr,
                )
            env = read_remote_env(
                args.ssh,
                args.container,
                sorted(universe),
                batch=args.batch_grep,
                timeout=args.ssh_timeout,
            )
            how = (
                "one anchored alternation of the universe variables"
                if args.batch_grep
                else "anchored per-variable greps"
            )
            source = {
                "mode": "ssh",
                "detail": f"{args.ssh} (container {args.container}, {how})",
            }
    except OperationalError as exc:
        print(f"[env_drift_check] OPERATIONAL FAILURE (exit 2): {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL

    report = check_drift(env, universe, ledger_rows)
    report["source"] = source
    report["ledger"] = str(ledger_path)
    report["ledger_rows"] = len(ledger_rows)

    print(render_json(report) if args.json else render_text(report))
    return EXIT_DRIFT if report["violations"] else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
