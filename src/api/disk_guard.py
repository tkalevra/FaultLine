"""Disk-pressure guard (#211): see a filling disk before Postgres hits ENOSPC.

An unattended install filled its host disk; /health stayed green until Postgres PANICked on the
shutdown checkpoint ("No space left on device"). This module measures free space on the
backend's data paths and turns it into three states:

  ok        free >= FAULTLINE_DISK_DEGRADE_PCT (default 10)
  degraded  free <  FAULTLINE_DISK_DEGRADE_PCT  → /health status degraded + console banner
  critical  free <  FAULTLINE_DISK_REFUSE_PCT (default 3) → ingest WRITES are refused with a
            clear 507 Insufficient Storage error; recall (/query) keeps working.

Which paths: FAULTLINE_DISK_PATHS, comma-separated (default "/"). In a container "/" is the
overlay filesystem, which lives on the Docker data root, the same disk as the named volumes
in a default install. If your volumes live elsewhere, mount them read-only into the backend
and list them here, e.g. FAULTLINE_DISK_PATHS=/,/pgdata. The tightest path decides.

Measuring is a statvfs per path (microseconds); results are cached for a few seconds so a
burst of writes costs one measurement.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Optional

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_CRITICAL = "critical"

_CACHE_TTL_S = 5.0
_cache: dict = {"ts": 0.0, "value": None}
_lock = threading.Lock()


def _pct_env(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return min(max(v, 0.0), 100.0)


def thresholds() -> tuple[float, float]:
    """(degrade_pct, refuse_pct). refuse is clamped to <= degrade."""
    degrade = _pct_env("FAULTLINE_DISK_DEGRADE_PCT", 10.0)
    refuse = _pct_env("FAULTLINE_DISK_REFUSE_PCT", 3.0)
    return degrade, min(refuse, degrade)


def data_paths() -> list[str]:
    raw = os.environ.get("FAULTLINE_DISK_PATHS", "") or "/"
    return [p.strip() for p in raw.split(",") if p.strip()]


def _measure() -> dict:
    degrade, refuse = thresholds()
    paths = []
    for p in data_paths():
        try:
            u = shutil.disk_usage(p)
        except OSError as e:
            paths.append({"path": p, "error": type(e).__name__})
            continue
        free_pct = round(100.0 * u.free / u.total, 2) if u.total else 0.0
        paths.append({"path": p, "free_pct": free_pct, "free_bytes": int(u.free),
                      "total_bytes": int(u.total)})
    measured = [x["free_pct"] for x in paths if "free_pct" in x]
    min_free = min(measured) if measured else None
    if min_free is None:
        status = "unknown"  # nothing measurable: never refuse writes on a probe failure
    elif min_free < refuse:
        status = STATUS_CRITICAL
    elif min_free < degrade:
        status = STATUS_DEGRADED
    else:
        status = STATUS_OK
    return {"status": status, "min_free_pct": min_free, "degrade_pct": degrade,
            "refuse_pct": refuse, "paths": paths}


def disk_status(max_age_s: Optional[float] = None) -> dict:
    """The current disk state (cached for a few seconds). Never raises."""
    ttl = _CACHE_TTL_S if max_age_s is None else max_age_s
    now = time.monotonic()
    with _lock:
        if _cache["value"] is not None and (now - _cache["ts"]) < ttl:
            return _cache["value"]
    try:
        value = _measure()
    except Exception as e:  # noqa: BLE001 — a probe must never break a request
        value = {"status": "unknown", "min_free_pct": None, "error": type(e).__name__,
                 "paths": []}
    with _lock:
        _cache["value"], _cache["ts"] = value, now
    return value


def reset_cache() -> None:
    with _lock:
        _cache["value"], _cache["ts"] = None, 0.0


def writes_refused() -> bool:
    return disk_status().get("status") == STATUS_CRITICAL


def refusal_detail(state: Optional[dict] = None) -> dict:
    s = state or disk_status()
    return {
        "error": "insufficient_storage",
        "detail": (f"Ingest refused: only {s.get('min_free_pct')}% disk free on the data volume "
                   f"(refuse threshold {s.get('refuse_pct')}%). Recall still works. Free disk "
                   f"space (see DEPLOYMENT.md, 'Disk full'), then retry."),
        "disk": s,
    }


# Ingest WRITE routes refused while the disk is critical. Reads (/query, /health, the console)
# and deletions that free space are never refused. Path-based on purpose: one choke point in
# the HTTP middleware, so a write route cannot forget the check.
INGEST_WRITE_PATHS = frozenset({
    "/ingest", "/extract", "/extract/rewrite", "/harvest-spans", "/store_context", "/learn",
    "/episodic/append", "/documents/enqueue", "/documents/retry", "/artefacts/retain",
})


def is_ingest_write(method: str, path: str) -> bool:
    return method.upper() == "POST" and path.rstrip("/") in INGEST_WRITE_PATHS
