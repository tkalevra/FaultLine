#!/bin/sh
# FaultLine quickstart launcher (Linux / macOS).
# Delegates to the cross-platform Python wizard so there's one real implementation.
# POSIX sh only (FOSS-003): no bash required — minimal Docker hosts and NAS distros
# often ship none. Run "python3 quickstart.py" directly to skip this file entirely.
set -eu
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  echo "Python 3.8+ is required. Install it (https://www.python.org/downloads/)" >&2
  echo "and re-run ./setup.sh — or run: python3 quickstart.py" >&2
  exit 1
fi
exec "$PY" quickstart.py "$@"
