#!/usr/bin/env bash
# FaultLine quickstart launcher (Linux / macOS).
# Delegates to the cross-platform Python wizard so there's one real implementation.
set -euo pipefail
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  echo "Python 3.8+ is required. Install it (https://www.python.org/downloads/) and re-run." >&2
  exit 1
fi
exec "$PY" quickstart.py "$@"
