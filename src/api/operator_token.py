"""The operator credential (``FAULTLINE_ADMIN_TOKEN``): resolve, persist, compare.

SPDX-License-Identifier: AGPL-3.0-only

One operator bearer gates the console (``/api/dashboard/*``) and every ``/admin/*`` and operator
``/internal/*`` route. Two sources, in order:

1. **``FAULTLINE_ADMIN_TOKEN`` in the environment.** The operator pinned it; nothing is stored.
2. **The persisted token (``public.operator_admin_token``).** With the env unset, the backend
   mints ONE random token on its first boot, stores only its SHA-256, and prints the plaintext
   once to the container log. Every later boot finds the row and prints nothing, so the token
   is stable across ``docker compose restart``, ``.env`` edits and host reboots (#145: it used
   to live only in ``os.environ`` and rotated on every process start, locking the operator out
   and printing a fresh live credential to the log on every boot).

Lost the first-boot token? Mint a replacement (printed once, the old one stops working)::

    docker compose exec faultline python -m src.api.operator_token --rotate

Only the hash is persisted, so a database dump does not hand out the operator credential.
Fails closed: no env value and no readable row means every operator route answers 401.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import time
from typing import Optional

ADMIN_TOKEN_ENV = "FAULTLINE_ADMIN_TOKEN"

# The persisted hash is re-read at most every _CACHE_TTL_S, so a `--rotate` from another
# process takes effect without a restart. A failed re-read keeps the last known hash.
_CACHE_TTL_S = 30.0
_cached_hash: Optional[str] = None
_cached_at: float = 0.0


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def _env_token() -> Optional[str]:
    return (os.environ.get(ADMIN_TOKEN_ENV) or "").strip() or None


def _dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or "").strip()


def _read_persisted_hash() -> Optional[str]:
    global _cached_hash, _cached_at
    if _cached_hash and (time.monotonic() - _cached_at) < _CACHE_TTL_S:
        return _cached_hash
    dsn = _dsn()
    if not dsn:
        return None
    try:
        import psycopg2
        with psycopg2.connect(dsn, connect_timeout=3) as db, db.cursor() as cur:
            cur.execute("SELECT token_sha256 FROM public.operator_admin_token WHERE id = 1")
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 — unreachable store: keep the last known hash, else fail closed
        return _cached_hash
    _cached_hash = row[0] if row and row[0] else None
    _cached_at = time.monotonic()
    return _cached_hash


def operator_auth_configured() -> bool:
    """True when some operator credential exists (env value or persisted token)."""
    return bool(_env_token() or _read_persisted_hash())


def operator_token_ok(presented: Optional[str]) -> bool:
    """Constant-time check of a presented bearer. Never raises; False when unconfigured."""
    presented = (presented or "").strip()
    if not presented:
        return False
    env = _env_token()
    if env:
        return hmac.compare_digest(_sha256_hex(presented), _sha256_hex(env))
    stored = _read_persisted_hash()
    if not stored:
        return False
    return hmac.compare_digest(_sha256_hex(presented), stored)


def _print_once(token: str, headline: str) -> None:
    # The ONE documented place the plaintext appears: stdout, on first mint or explicit rotate.
    print(
        "====================================================================\n"
        f"FAULTLINE_ADMIN_TOKEN ({headline}):\n"
        f"  {token}\n"
        "Paste this into the operator console sign-in (http://localhost:8000/).\n"
        "It is stored hashed and survives restarts; it is NOT printed again.\n"
        "Lost it? docker compose exec faultline python -m src.api.operator_token --rotate\n"
        "To pin a known token instead, set FAULTLINE_ADMIN_TOKEN and restart.\n"
        "====================================================================",
        flush=True,
    )


def ensure_operator_token() -> str:
    """Backend boot. Returns ``"env"``, ``"persisted"``, ``"minted"`` or ``"unavailable"``.

    First boot wins (``ON CONFLICT DO NOTHING``), so two racing processes mint one token; only
    the process whose insert landed prints it."""
    global _cached_hash, _cached_at
    if _env_token():
        # #164: a pinned env token RETIRES the stored first-boot token. Otherwise removing the
        # pin later (or a .env that loses it) would silently revive the old token, which was
        # printed to the container log and is often exactly why the operator pinned one.
        # Un-pinning then mints a fresh token on the next boot, printed once.
        _cached_hash, _cached_at = None, 0.0
        dsn = _dsn()
        if dsn:
            try:
                import psycopg2
                with psycopg2.connect(dsn, connect_timeout=5) as db, db.cursor() as cur:
                    cur.execute("DELETE FROM public.operator_admin_token")
            except Exception:  # noqa: BLE001 — the env token works regardless
                pass
        return "env"
    dsn = _dsn()
    if not dsn:
        return "unavailable"
    token = secrets.token_urlsafe(32)
    try:
        import psycopg2
        with psycopg2.connect(dsn, connect_timeout=5) as db, db.cursor() as cur:
            cur.execute(
                "INSERT INTO public.operator_admin_token (id, token_sha256) VALUES (1, %s) "
                "ON CONFLICT (id) DO NOTHING RETURNING id",
                (_sha256_hex(token),),
            )
            inserted = cur.fetchone() is not None
            cur.execute("SELECT token_sha256 FROM public.operator_admin_token WHERE id = 1")
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return "unavailable"
    _cached_hash = row[0] if row and row[0] else None
    _cached_at = time.monotonic()
    if not _cached_hash:
        return "unavailable"
    if inserted:
        _print_once(token, "auto-generated on first boot")
        return "minted"
    return "persisted"


def rotate_operator_token() -> str:
    """Replace the persisted token with a fresh one and print it once. Returns the outcome."""
    global _cached_hash
    if _env_token():
        return "env"
    dsn = _dsn()
    if not dsn:
        return "unavailable"
    token = secrets.token_urlsafe(32)
    import psycopg2
    with psycopg2.connect(dsn, connect_timeout=5) as db, db.cursor() as cur:
        cur.execute(
            "INSERT INTO public.operator_admin_token (id, token_sha256) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET token_sha256 = EXCLUDED.token_sha256, "
            "created_at = NOW()",
            (_sha256_hex(token),),
        )
    _cached_hash = None
    _print_once(token, "rotated")
    return "rotated"


def _reset_cache_for_tests() -> None:
    global _cached_hash, _cached_at
    _cached_hash = None
    _cached_at = 0.0


def main(argv: list[str]) -> int:
    if argv[1:] != ["--rotate"]:
        print("usage: python -m src.api.operator_token --rotate", file=sys.stderr)
        return 2
    outcome = rotate_operator_token()
    if outcome == "env":
        print("FAULTLINE_ADMIN_TOKEN is set in the environment; change it there and restart.",
              file=sys.stderr)
        return 1
    if outcome == "unavailable":
        print("POSTGRES_DSN is not set; cannot rotate the persisted token.", file=sys.stderr)
        return 1
    print(f"The running backend picks up the new token within {int(_CACHE_TTL_S)} seconds.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
