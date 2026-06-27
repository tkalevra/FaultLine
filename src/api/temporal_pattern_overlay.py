"""Per-tenant temporal_patterns resolution (the GROWABLE relative-date cue engine).

ARCHITECTURE (see migration 103, CLAUDE.md "Temporal Determination" + the per-tenant
overlay sections, and the sibling modules `rel_type_overlay.py` / `taxonomy_overlay.py`):

FaultLine is per-tenant. `public.temporal_patterns` is a TEMPLATE / SEED-SOURCE ONLY,
read solely by provisioning. The seeder copies public → tenant at provisioning time, so
each tenant's own schema carries the EVIDENCED relative-cue seed PLUS any freq-gated grown
cues. Growth NEVER writes to public, and NO runtime read touches public on a bound tenant.

THE GAP THIS CLOSES:
`linguistics._span_is_relative` matched a span against a FROZEN in-code word-list
(`_RELATIVE_DATE_CUES`). A frozen list assumes a fixed temporal vocabulary and silently
mis-anchors anything outside it. This module resolves the relative-cue inventory from the
BOUND TENANT SCHEMA so a tenant's grown cues are honoured at runtime — exactly the
metadata-driven, per-tenant, growable contract the rel_type / taxonomy / extraction_patterns
layers already obey.

WHAT THIS RESOLVES:
    <tenant>.temporal_patterns  (seed-copied-at-provisioning ∪ grown)  WHERE anchor_type='relative'
The closed FORMAL absolute checks (month names / numeric / 4-digit year) + the
closest-to-reference YEAR anchoring stay in code — only the RELATIVE-cue recognition is data.

It deliberately MIRRORS `taxonomy_overlay.py` and REUSES the SAME request-schema ContextVar
(`rel_type_overlay._current_schema`, via `set_current_schema`) so a single per-request binding
governs ALL three resolvers. The only module-level state here is the unscoped-fallback cache and
the per-tenant cache, exactly as in the sibling modules.

FAIL-SAFE: a tenant schema that predates this migration (no `temporal_patterns` table) — or any
read failure — resolves to the BOOTSTRAP relative-cue set (the evidenced seed, hard-coded here as
a DB-DOWN safety net, NOT as the authority). It NEVER falls back to another tenant's rows.

HOT-PATH COST: identical contract to the sibling overlays — unscoped fallback cached with a TTL,
per-tenant data cached per schema, a warm hit is DB-free + the compiled regex list is cached too.
"""

import re
import time
import threading

import psycopg2
import structlog

# Reuse the SAME request-schema ContextVar binding as the rel_type/taxonomy overlays so ONE
# set_current_schema()/reset_current_schema() per request governs ALL overlays.
from src.api import rel_type_overlay

log = structlog.get_logger()

# TTL for both the global seed cache and per-schema overlays. Matches the sibling-overlay
# contract; explicit invalidation closes the loop faster than the TTL.
_TTL_SECONDS = 5.0

_lock = threading.RLock()

# Unscoped-fallback cache (public template) — used ONLY when no tenant schema is bound
# (boot / anonymous). NEVER consulted on a real tenant binding.
# {"cues": [(pattern_regex, compiled), ...], "loaded_at": float}
_seed_cache: dict = {"cues": [], "loaded_at": 0.0}

# Per-schema cache: {schema_name: {"cues": [(regex, compiled), ...], "loaded_at": float}}.
_overlay_cache: dict[str, dict] = {}

# ── GATE matcher cache (the LATENCY GATE in front of the whole date pipeline) ──────────
# One COMBINED regex per schema = the OR-union of ALL active temporal_patterns rows (relative
# cues + the formal-absolute class seeded by migration 104). A cheap single .search() answers
# "does this turn carry ANY date cue?" — a no-cue turn ("my name is Alexander") skips the
# entire spaCy DATE NER + dateparser pipeline. Same per-schema TTL/cache contract as the cues.
# {schema_name: {"matcher": compiled_union_or_None, "loaded_at": float}}; "" key = unscoped seed.
_gate_cache: dict[str, dict] = {}

# ── BOOTSTRAP relative-cue set (DB-DOWN SAFETY NET ONLY — NOT the authority) ──────────
# This is the EVIDENCED seed inventory (the same rows migration 103 writes to public),
# hard-coded so a tenant schema lacking the table (pre-migration) or an unreadable read still
# classifies the common relative cues instead of silently treating everything as absolute. The
# DB rows are the authority; this is the fallback when the DB cannot be read. Sourced from
# dateparser's English locale data + HeidelTime reThisNextLast (see migration 103 header).
_BOOTSTRAP_RELATIVE_CUES: tuple[str, ...] = (
    # dateparser en: past / future markers + relative-type phrases + deictics + units
    r"\bago\b", r"\bbefore\b", r"\bfrom\s+now\b", r"\blater\b",
    r"\bday\s+before\s+yesterday\b", r"\bday\s+after\s+tomorrow\b",
    r"\btoday\b", r"\byesterday\b", r"\btomorrow\b", r"\btonight\b", r"\bnow\b",
    r"\bweek\b", r"\bmonth\b", r"\byear\b",
    # HeidelTime reThisNextLast + rePartWords + TIMEX3 deictics
    r"\blast\b", r"\bnext\b", r"\bthis\b", r"\bpast\b", r"\bprevious\b",
    r"\bcurrent\b", r"\blatest\b", r"\brecent\b", r"\bupcoming\b", r"\bcoming\b",
    r"\bearlier\b",
)

# ── BOOTSTRAP FORMAL-ABSOLUTE surface forms (DB-DOWN SAFETY NET for the GATE only) ────
# Mirrors migration 104's seeded formal_absolute rows so the combined GATE still recognizes
# absolute dates (month names / numeric shapes / 4-digit year) when the DB is unreadable. These
# do NOT drive the relative-vs-absolute CLASSIFIER (that stays in code); they only widen the gate
# so a real absolute date ("January 17th", "3/22", "2023") is never skipped by the latency gate.
_BOOTSTRAP_ABSOLUTE_CUES: tuple[str, ...] = (
    r"\b(?:19|20)\d{2}\b",                 # 4-digit year
    r"\b\d{4}/\d{1,2}/\d{1,2}\b",          # 2023/04/10
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",   # 3/22 or 03/22/2023
    r"\bjanuary\b", r"\bfebruary\b", r"\bmarch\b", r"\bapril\b", r"\bmay\b", r"\bjune\b",
    r"\bjuly\b", r"\baugust\b", r"\bseptember\b", r"\boctober\b", r"\bnovember\b", r"\bdecember\b",
    r"\bjan\.?\b", r"\bfeb\.?\b", r"\bmar\.?\b", r"\bapr\.?\b", r"\bjun\.?\b", r"\bjul\.?\b",
    r"\baug\.?\b", r"\bsep\.?\b", r"\bsept\.?\b", r"\boct\.?\b", r"\bnov\.?\b", r"\bdec\.?\b",
)


def _compile(pattern_regex: str):
    """Compile a relative-cue regex case-insensitively. Returns the compiled pattern or None
    (a bad/grown regex must NEVER crash the temporal layer — it is simply skipped)."""
    try:
        return re.compile(pattern_regex, re.IGNORECASE)
    except Exception as e:  # noqa: BLE001 — a malformed grown row → skip, fail-safe
        log.warning("temporal_pattern_overlay.bad_regex", pattern=pattern_regex[:120],
                    error=str(e)[:120])
        return None


def _bootstrap_cues() -> list:
    """The compiled BOOTSTRAP relative-cue list (DB-down safety net)."""
    out = []
    for pat in _BOOTSTRAP_RELATIVE_CUES:
        c = _compile(pat)
        if c is not None:
            out.append((pat, c))
    return out


def _fetch_relative_cues(dsn: str, schema_qualifier: str) -> list:
    """Read ACTIVE relative-cue rows from a single explicit schema. `schema_qualifier` is a bare,
    already-validated schema identifier ('public' or 'faultline_<slug>'). Returns
    [(pattern_regex, compiled), ...] for anchor_type='relative' AND is_active. Raises on a missing
    table / read error so the caller's fail-safe (bootstrap) applies."""
    cues: list = []
    # connect_timeout (CONNECTION guard, NOT an LLM/op timeout): a momentarily-slow PG must not
    # block a turn unboundedly on a cold cue read. On timeout/failure psycopg2 raises → the
    # caller's fail-safe (bootstrap cue set) applies; correctness is preserved.
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT pattern_regex FROM {schema_qualifier}.temporal_patterns "
                f"WHERE anchor_type = 'relative' AND is_active = true"
            )
            for (pattern_regex,) in cur.fetchall():
                if not pattern_regex:
                    continue
                c = _compile(pattern_regex)
                if c is not None:
                    cues.append((pattern_regex, c))
    return cues


def _get_seed(dsn: str) -> list:
    """Return the cached public.temporal_patterns relative-cue list (TTL-refreshed) for the
    UNSCOPED fallback path ONLY (no tenant bound). Returns the BOOTSTRAP set if public is
    unreadable. Callers must NOT mutate the returned list."""
    now = time.time()
    with _lock:
        if _seed_cache["cues"] and (now - _seed_cache["loaded_at"]) <= _TTL_SECONDS:
            return _seed_cache["cues"]
    try:
        fresh = _fetch_relative_cues(dsn, "public")
        if not fresh:
            fresh = _bootstrap_cues()
    except Exception as e:
        log.warning("temporal_pattern_overlay.seed_fetch_failed", error=str(e)[:160])
        with _lock:
            return _seed_cache["cues"] or _bootstrap_cues()  # last-known-good or bootstrap
    with _lock:
        _seed_cache["cues"] = fresh
        _seed_cache["loaded_at"] = time.time()
        return _seed_cache["cues"]


def _is_real_tenant_schema(schema_name) -> bool:
    if not schema_name:
        return False
    s = schema_name.strip().lower()
    return s not in ("", "public")


def resolve_relative_cues(dsn: str, schema_name) -> list:
    """Resolve the ACTIVE relative-cue list from the BOUND TENANT SCHEMA ONLY.

    Returns [(pattern_regex, compiled), ...] (do NOT mutate). Cache hit performs no DB query.

    schema_name None / "public" → unscoped fallback: read the public template.
    schema_name = real tenant   → read `<schema>.temporal_patterns` ONLY (seed-copied ∪ grown).
        If the tenant schema is unreadable / the table is missing (pre-migration) we FAIL SAFE to
        the BOOTSTRAP relative-cue set — we do NOT read public for a bound tenant (isolation) and
        we do NOT silently classify everything as absolute (that would mis-anchor relatives).
    """
    if not dsn:
        return _bootstrap_cues()

    if not _is_real_tenant_schema(schema_name):
        return list(_get_seed(dsn))

    schema_name = schema_name.strip()
    now = time.time()

    with _lock:
        entry = _overlay_cache.get(schema_name)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["cues"]

    try:
        tenant_cues = _fetch_relative_cues(dsn, schema_name)
        if not tenant_cues:
            # Table present but empty (mis-seeded) → bootstrap so relatives still anchor.
            tenant_cues = _bootstrap_cues()
    except Exception as e:
        # Tenant schema unreadable / table missing (pre-migration). FAIL SAFE to bootstrap;
        # do NOT read public for a bound tenant (would mask the failure / cross isolation).
        log.warning("temporal_pattern_overlay.tenant_fetch_failed",
                    schema=schema_name, error=str(e)[:160])
        return _bootstrap_cues()

    with _lock:
        _overlay_cache[schema_name] = {"cues": tenant_cues, "loaded_at": time.time()}
    return tenant_cues


def resolve_current(dsn: str) -> list:
    """Resolve relative cues for the ContextVar-bound current request schema (tenant-only).
    Uses the SAME binding as the rel_type / taxonomy resolvers."""
    return resolve_relative_cues(dsn, rel_type_overlay.get_current_schema())


# ── COMBINED GATE matcher (latency gate in front of the whole date pipeline) ───────────

def _bootstrap_gate_matcher():
    """Compiled OR-union of the bootstrap relative + absolute surface forms (DB-down net)."""
    parts = [p for p in (_BOOTSTRAP_RELATIVE_CUES + _BOOTSTRAP_ABSOLUTE_CUES) if p]
    if not parts:
        return None
    return _compile("|".join(parts))


def _fetch_gate_patterns(dsn: str, schema_qualifier: str) -> list:
    """Read ALL active pattern_regex rows (ANY anchor_type) from one explicit schema, for the
    combined gate. Raises on a missing table / read error so the caller's fail-safe applies."""
    out: list = []
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT pattern_regex FROM {schema_qualifier}.temporal_patterns "
                f"WHERE is_active = true"
            )
            for (pattern_regex,) in cur.fetchall():
                if pattern_regex:
                    out.append(pattern_regex)
    return out


def _build_gate_matcher(dsn: str, schema_qualifier: str):
    """Build the combined OR-union matcher for one schema. Returns a compiled regex or the
    bootstrap matcher on any failure / empty result (fail-safe: never a None-skip-everything gate)."""
    try:
        patterns = _fetch_gate_patterns(dsn, schema_qualifier)
    except Exception as e:  # noqa: BLE001 — table missing / read failure → bootstrap net
        log.warning("temporal_pattern_overlay.gate_fetch_failed",
                    schema=schema_qualifier, error=str(e)[:160])
        return _bootstrap_gate_matcher()
    if not patterns:
        return _bootstrap_gate_matcher()
    # Validate each row compiles on its own; drop bad grown rows, then OR the survivors into ONE
    # union regex so the whole-turn gate is a single .search() (warm = DB-free, one regex op).
    good = [p for p in patterns if _compile(p) is not None]
    if not good:
        return _bootstrap_gate_matcher()
    union = _compile("|".join(good))
    return union if union is not None else _bootstrap_gate_matcher()


def resolve_gate_matcher(dsn: str, schema_name):
    """Resolve the COMBINED gate matcher (compiled OR-union of all active temporal_patterns rows)
    for the bound tenant schema. Cache hit performs no DB query (one regex per schema, TTL-refreshed).

    schema_name None / "public" → unscoped seed (public template).
    schema_name = real tenant   → <schema>.temporal_patterns (seed-copied ∪ grown), tenant-only.
    Any read failure / pre-migration tenant → BOOTSTRAP matcher (relative + absolute surface forms)
    so the gate stays SAFE (never a gate that skips a real date). Returns a compiled regex or None
    (None only if even the bootstrap fails to compile → caller treats as 'no gate' = run pipeline).
    """
    if not dsn:
        return _bootstrap_gate_matcher()

    schema_qualifier = "public"
    cache_key = ""
    if _is_real_tenant_schema(schema_name):
        schema_qualifier = schema_name.strip()
        cache_key = schema_qualifier

    now = time.time()
    with _lock:
        entry = _gate_cache.get(cache_key)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return entry["matcher"]

    matcher = _build_gate_matcher(dsn, schema_qualifier)
    with _lock:
        _gate_cache[cache_key] = {"matcher": matcher, "loaded_at": time.time()}
    return matcher


def resolve_gate_matcher_current(dsn: str):
    """Resolve the combined gate matcher for the ContextVar-bound current request schema
    (SAME binding as the cue/rel_type/taxonomy resolvers)."""
    return resolve_gate_matcher(dsn, rel_type_overlay.get_current_schema())


def text_has_date_cue(text: str, dsn: str) -> bool:
    """THE LATENCY GATE. True iff `text` matches ANY active temporal_patterns row for the
    current tenant (one combined .search()). False → the turn carries no date cue → the caller
    SKIPS the entire date pipeline (spaCy DATE NER + dateparser). Fail-safe: on ANY resolution
    error or a null matcher we return True (run the pipeline) so we never silently lose a date."""
    if not text:
        return False
    try:
        matcher = resolve_gate_matcher_current(dsn)
    except Exception as e:  # noqa: BLE001 — fail-safe: never block the date pipeline on a gate error
        log.warning("temporal_pattern_overlay.gate_resolve_failed", error=str(e)[:160])
        return True
    if matcher is None:
        return True  # no usable gate → do not skip (run the pipeline)
    try:
        return matcher.search(text) is not None
    except Exception:  # noqa: BLE001 — fail-safe
        return True


def invalidate(schema_name=None) -> None:
    """Invalidate caches.

    schema_name given  → drop that tenant's cache (next read rebuilds it). What a grown-cue
                          approval / refresh calls so only that tenant's cache is rebuilt.
    schema_name None   → drop ALL per-tenant caches AND the unscoped public-template fallback
                          cache (full reset).
    """
    with _lock:
        if _is_real_tenant_schema(schema_name):
            _overlay_cache.pop(schema_name.strip(), None)
            _gate_cache.pop(schema_name.strip(), None)
        else:
            _overlay_cache.clear()
            _gate_cache.clear()
            _seed_cache["cues"] = []
            _seed_cache["loaded_at"] = 0.0
