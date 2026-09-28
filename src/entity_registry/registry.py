import os
import re
import sys
import uuid
import psycopg2
import psycopg2.errorcodes
import psycopg2.errors
import structlog
from fastapi import HTTPException

from src.extraction.display_case import display_form_for
from src.entity_registry import weld_guard

try:  # pragma: no cover - logging_config is always present in-app
    from src.api.logging_config import log_crit
except Exception:  # noqa: BLE001 — never let an import shape the resolution path
    def log_crit(logger, msg, **kwargs):  # type: ignore[misc]
        logger.critical(msg, **kwargs)

log = structlog.get_logger()


# ── TENANT BINDING (registry-searchpath-fallthrough) ─────────────────────────────────────────
# Every SQL statement in this module names its tables UNQUALIFIED (`entity_aliases`, `entities`,
# `rel_types`) and relies on the connection's `search_path` to land in the tenant schema. That
# reliance was trusted, not enforced: the six inline search_path asserts guarded the WRITES
# only, and the alias-lookup READ in `resolve()` ran with whatever the connection happened to hold.
#
# Why "whatever it happened to hold" is not the tenant: PostgreSQL, SET —
#   "If SET (or equivalently SET SESSION) is issued within a transaction that is later aborted,
#    the effects of the SET command disappear when the transaction is rolled back."
#   (https://www.postgresql.org/docs/current/sql-set.html)
# and PostgreSQL, DDL — Schemas §5.10.3 "The Schema Search Path" —
#   "The first matching table in the search path is taken to be the one wanted." … default
#   `"$user", public` … "If no such schema exists, the entry is ignored."
#   (https://www.postgresql.org/docs/current/ddl-schemas.html)
# `/ingest` binds the tenant with a plain (session-level) SET inside its open, autocommit-OFF
# transaction and constructs this registry with auto_commit=False. One rollback on that
# connection — any failing statement cleared by `rollback()`, including this module's own
# aborted-transaction probe — discards the bind; the unmatched `"$user"` entry is skipped, and
# every later unqualified read resolves in `public`: the LEGACY seed-template `entity_aliases`
# that has no `display_form` column. Measured on pre-prod 2026-09-14: `/ingest` 500
# `UndefinedColumn: column "display_form" does not exist` on a tenant whose own table HAS it.
#
# The fix is ONE seam: `EntityRegistry._bind_tenant_schema()` is called on entry of every public
# DB-touching method (and after every rollback this module performs), so no statement here can
# run on a binding older than the method it sits in. A `SET LOCAL` migration is deliberately NOT
# started here — that is the separate grain-1b (PgBouncer) work; this seam is what makes that
# migration a one-line change later.
#
# FAIL LOUD: a registry constructed WITHOUT a schema_name cannot re-bind anything. Under
# per-tenant operation (ENTITY_REGISTRY_REQUIRE_SCHEMA=1) that is a defect at the
# construction site, and the registry REFUSES with a CRIT naming that site rather than reading
# whatever schema the connection drifted to. The legacy single-tenant path — the flag unset, the
# caller intends the connection's own search_path (a dev/single-user box, or a unit test with a
# fake connection) — is preserved: the registry proceeds unbound and logs ONE warning per
# instance naming the construction site, so an unbound registry is never silent anywhere.
_SCHEMA_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# MEASUREMENT-FAMILY COMPOUND REDIRECT (issue #21) + BARE family-noun refusal (issue #22) —
# ONE rollback lever for the whole mint-suppression seam, default ON. OFF → the redirect and
# the bare refusal never run and every surface mints byte-for-byte as before (the W1/df18
# lane-flag pattern). Env: REGISTRY_FAMILY_COMPOUND_REDIRECT.
REGISTRY_FAMILY_COMPOUND_REDIRECT: bool = os.environ.get(
    "REGISTRY_FAMILY_COMPOUND_REDIRECT", "true").strip().lower() not in ("0", "false", "no")


def _per_tenant_operation() -> bool:
    """Is this process running strictly per-tenant (schema-per-user) — must every registry be bound?

    Pure env read, imports nothing. `ENTITY_REGISTRY_REQUIRE_SCHEMA=1` opts a per-tenant
    deployment into the refusal; unset keeps the legacy unbound path (warned once per instance).
    """
    strict = str(os.environ.get("ENTITY_REGISTRY_REQUIRE_SCHEMA") or "").strip().lower() in ("1", "true", "yes")
    return strict


class RegistrySearchPathError(RuntimeError):
    """The registry could not bind (or was never given) the tenant schema.

    Raised INSTEAD of running an unqualified statement on a connection whose tenant binding is
    unknown — a refused request is strictly better than a read or write in `public` (the seed
    template) or in another tenant's schema. Callers that route through the MCP ingest seam keep
    the turn (`_ingest_with_retry` retains the body); nothing is silently lost.
    """


def _mint_caller_tag() -> str:
    """The immediate non-registry caller chain of a MINT, as 'fn<file:line>' segments.

    Issue #23 wiring: the #22 investigation could not tell WHICH lane minted the live 'quantity'
    alias from the logs (the registration line carried no caller). This bounded 3-frame walk
    (sys._getframe, never inspect.stack — mints are rare and this is not on any hot path) names
    the lane on every future mint so a stray mint is one grep away. Fail-safe → ''.
    """
    try:
        segs = []
        f = sys._getframe(1)  # the resolve() frame
        for _ in range(4):
            f = f.f_back
            if f is None:
                break
            co = f.f_code
            fn = co.co_name
            if fn == "resolve":  # skip the registry's own frames
                continue
            path = (co.co_filename or "").rsplit("/", 1)[-1]
            segs.append(f"{fn}<{path}:{f.f_lineno}>")
            if len(segs) >= 3:
                break
        return " <- ".join(segs)
    except Exception:  # noqa: BLE001 — a log tag must never break the mint path
        return ""


class MeasurementFamilySurfaceError(ValueError):
    """A BARE measurement-family surface was refused as an entity referent (issues #22+#23).

    The surface is the family CANONICAL itself ('quantity', 'dosage', 'level' — an ATTRIBUTE
    NAME), not a referent: minting it creates the 'Lisinopril is related to Quantity /
    Quantity's Quantity is 10 milligrams' island class. A ValueError subclass BY DESIGN: every
    /ingest resolution site already catches ValueError with a LOUD, recorded drop (the turn is
    retained in episodic_log; the response carries stage+reason), and every growth lane's
    resolve() hop is `except Exception → skip` — so the refusal degrades to a named drop
    everywhere, never a crash and never a junk row.

    DEFAULT-DENY (issue #23): the refusal fires at the surrogate-registration block for ANY
    caller that does not declare `name_intent=True` — the #22 opt-in (edge_object /
    edge_scalar_subject kwargs threaded at 5 adjudicated sites) missed the LIVE mint path
    (deploy-13: the harvest's pending-scalar-grounding concept queue resolved the bare
    canonical with no kwargs, minting the alias BEFORE /ingest's guarded sites ran; the alias
    probe's early return then made every later guarded resolve sail). A mint-seam guard at a
    seam with dozens of callers is a CENSUS problem, not a threading problem: refuse by
    default, the NAME frames opt out via `name_intent=True` (the naming rels' SCALAR tails
    keep naming objects string-kept and filing through register_alias — a different method
    this refusal never touches — so the genitive/relation-pattern/correction NAME frames are
    the only opt-outs). The AST census (tests/test_mint_guard_default_deny_x23.py) classifies
    EVERY resolve() call site so a new unclassified site fails the suite.
    """


class IndefiniteAnaphorSurfaceError(ValueError):
    """An INDEFINITE-ANAPHOR surface was refused as an entity referent (issue #30 — the #23
    default-deny family extended to the anaphor class).

    The surface is a CLOSED-CLASS FUNCTION WORD ('one'/'ones'/'each', or the pure-anaphor
    phrases 'each one'/'every one'/'each of them') — the distributive pronominal shape CGEL
    ch.17 treats as anaphoric to a preceding PLURAL set. It has NO lexical content: minting it
    creates the deploy-#15 junk row `(one, height, '90')` — a preferred alias 'one' on a
    nothing-entity, the measure stripped of its unit and unreachable from the referent. Same
    ValueError-subclass drop discipline as :class:`MeasurementFamilySurfaceError` (loud,
    recorded, turn retained); same DEFAULT-DENY shape: the alias probe above returns FIRST for
    any REGISTERED name — so a genuine thing NAMED 'One' ("My boat's name is One.", filed by
    the naming frames through ``register_alias``, a different method) still resolves forever
    after — and genuine NAMING frames that bind a name as a relational subject declare
    ``name_intent=True`` (the x23 census classification)."""

# Maximum length for entity names and aliases stored in entity_aliases.alias.
# 256 chars eliminates injection payload viability (no coherent multi-sentence directive fits
# within 256 chars) while preserving all real-world personal data: full legal names with
# titles, addresses, employer names.  Truncation is used (not rejection) because legitimate
# long names are possible edge cases.  Mitigates TM-01.
_ENTITY_NAME_MAX_LEN = 256


# Stable namespace UUID for deriving surrogates when user_id is not a valid UUID
_FAULTLINE_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')


# First-person pronouns (and the possessive "my") always denote the REQUESTING
# user — never a distinct entity. They must resolve to the request's user_id, never
# mint a surrogate. This is the single resolution seam every ingest path flows
# through (subject AND object), so grounding here is subject-agnostic and complete.
# Deterministic, bounded language primitive (a closed pronoun set, not a domain list).
# Upstream main.py normalizers also rewrite these to "user"; this is the backstop
# that catches any path/object position they miss (the phantom "i" entity bug).
_FIRST_PERSON_PRONOUNS = frozenset({"i", "me", "my", "myself", "mine"})


# Alias preference provenance trust ordering (ALIAS-PROVENANCE-DESIGN).
# Higher rank = more trusted. A newly-preferred alias may only demote an
# incumbent preferred alias when its source rank is >= the incumbent's.
#
# "lexical" (migration-free, added for the arrival weld guard) ranks EQUAL to "inferred" —
# deliberately, not above it. Rank means ONE thing here: how much this source is trusted to
# win the DISPLAY preference. A WordNet co-synset lemma is exactly as (un)trusted for display
# as any other engine guess, so giving it a higher rank would be a lie that also let it
# out-rank "inferred" in the merge's preferred-alias recompute. What distinguishes it is a
# different axis entirely — see _WARRANTED_SOURCES below.
_PREFERENCE_RANK = {
    "user_stated": 5,
    "rel_default": 4,
    "inferred": 3,
    "lexical": 3,
    "merge": 2,
    "provisioned": 1,
    "unspecified": 0,
}


# ── THE WARRANT AXIS (orthogonal to rank; do not conflate the two) ────────────────────────
# Registering a SECOND, different surface on an entity asserts that both surfaces denote ONE
# referent. Rank answers "how much do I trust this source to pick the display name?"; it does
# NOT answer "was this co-reference claim licensed by anything?". Those are different
# questions and collapsing them into one number is what made the third weld-guard arm
# inexpressible: an engine synonym registered from a lexical resource and a bogus
# extraction-invented co-reference both arrived as "inferred" and were byte-identical.
#
# A WARRANTED source is one whose writer can NAME the external authority that licenses the
# co-reference. Today that is the offline WordNet co-synset lane (src/api/canonicalize.py):
# shared synset membership IS the definition of synonymy in Princeton WordNet, so the claim
# arrives with a stated, checkable licence. Everything else arrives with none.
#
# This is a property of the PROVENANCE VOCABULARY, declared once here beside the ladder it
# belongs to — not a special case inside the guard, and not a list of rel_types, entity
# names, types or domain words. Adding a future warranted lane means adding its source here.
_WARRANTED_SOURCES = frozenset({"lexical"})


def preference_rank(source: str) -> int:
    """Map an alias preference_source to its trust rank (higher = more trusted).

    Unknown sources and None map to 0 (lowest trust). Pure helper — no DB access.
    """
    if not source:
        return 0
    return _PREFERENCE_RANK.get(source, 0)


def preference_is_warranted(source: str) -> bool:
    """Did this alias provenance arrive with a RECORDED WARRANT for its co-reference claim?

    Orthogonal to :func:`preference_rank` — a warranted source is not thereby more trusted
    for display; it is licensed to assert that two surfaces denote one referent. Unknown or
    missing sources are UNWARRANTED (the safe default: no claim recorded means no licence).
    Pure helper — no DB access.
    """
    if not source:
        return False
    return source in _WARRANTED_SOURCES


def _make_surrogate(user_id: str, name: str) -> str:
    """Generate deterministic UUID v5 surrogate for an entity.

    Uses user_id directly as the namespace if it is a valid UUID.
    Falls back to a UUID v5 derived from a stable namespace + user_id
    when user_id is not a valid UUID (e.g., 'anonymous').
    """
    try:
        namespace = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        namespace = uuid.uuid5(_FAULTLINE_NAMESPACE, user_id)
    return str(uuid.uuid5(namespace, name.lower().strip())).lower()

class EntityRegistry:
    """
    Canonical entity store. All relationship facts must reference
    canonical entity IDs from this registry.

    Responsibilities:
    - Resolve any name/alias to its canonical entity ID
    - Register new entities
    - Store aliases and preferred names
    - Never allow aliases to appear as subject_id/object_id in facts
    """

    def __init__(self, db_conn, auto_commit=True, schema_name=None):
        self.db_conn = db_conn
        self.auto_commit = auto_commit
        # The schema name is interpolated into the `search_path` SET by the bind seam, so it is
        # validated ONCE here (same rule as gate.py / data_cursor.py `_SCHEMA_NAME_RE`). A
        # malformed name is a caller defect — refuse it at construction, never at the SET.
        if schema_name is not None and not _SCHEMA_NAME_RE.match(str(schema_name)):
            raise ValueError(f"EntityRegistry: invalid schema_name {schema_name!r} "
                             f"(expected ^[a-z0-9_]+$)")
        self.schema_name = schema_name
        # Where this registry was built — the ONE fact a refusal must name, because the defect
        # (an unbound registry) lives at the construction site, not at the method that trips.
        try:
            _f = sys._getframe(1)
            self._construction_site = (f"{os.path.basename(_f.f_code.co_filename)}:"
                                       f"{_f.f_lineno}::{_f.f_code.co_name}")
        except Exception:  # noqa: BLE001 — diagnostics only
            self._construction_site = "unknown"
        self._legacy_unbound_warned = False

    # ── THE ONE BIND SEAM ────────────────────────────────────────────────────────────────
    def _bind_tenant_schema(self, op: str) -> None:
        """Bind the tenant schema on THIS connection, now, before `op` runs a statement.

        Mirrors `src/wgm/gate.py::_reapply_search_path`: a session-level search_path SET is
        discarded when the transaction it was issued in rolls back (PostgreSQL SET docs, cited at
        the top of this module), so a bind is only trusted for the statements that follow it in
        the same method. Called on entry of every public DB-touching method and after every
        rollback this module performs.

        Outcomes — none of them silent:
          * schema_name set → `SET search_path TO <schema>` (no `public`). A connection sitting in
            an ABORTED transaction cannot run the SET (`InFailedSqlTransaction`); nothing in that
            transaction can commit any more, so it is rolled back (logged, with `op`) and the bind
            retried once. Any other failure → CRIT + `RegistrySearchPathError`.
          * no schema_name, per-tenant operation → CRIT naming the construction site +
            `RegistrySearchPathError`. Refuse; do not read `public`.
          * no schema_name, legacy single-tenant operation → proceed on the connection's own
            search_path; ONE warning per instance naming the construction site.

        Deliberately NO `SHOW search_path` read-back here: a fetch on the bind seam would change
        the row protocol every caller's cursor sees (fixtures and real cursors alike); the SET
        raising IS the failure signal, and the read-back is pinned by the poison test in
        tests/test_entity_registry_searchpath.py against a real PostgreSQL.
        """
        schema = getattr(self, "schema_name", None)
        site = getattr(self, "_construction_site", "unknown")
        if not schema:
            if _per_tenant_operation():
                msg = (f"entity_registry.unbound_registry_refused: EntityRegistry built at "
                       f"{site} has no schema_name; refusing {op} — an unqualified read/write on "
                       f"a connection with no tenant binding lands in public (seed template) or "
                       f"another tenant's schema. Pass schema_name at the construction site.")
                log_crit(log, msg, op=op, construction_site=site, component="search_path")
                raise RegistrySearchPathError(msg)
            if not getattr(self, "_legacy_unbound_warned", False):
                self._legacy_unbound_warned = True
                log.warning("entity_registry.unbound_legacy_path",
                            op=op, construction_site=site,
                            note="no schema_name; running on the connection's own search_path "
                                 "(legacy single-tenant). Set "
                                 "ENTITY_REGISTRY_REQUIRE_SCHEMA=1 to refuse instead.")
            return
        for attempt in (1, 2):
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query schema_name validated by _SCHEMA_NAME_RE ^[a-z0-9_]+$ in __init__
                return
            except psycopg2.errors.InFailedSqlTransaction as e:
                if attempt == 2:
                    _err = e
                    break
                # The connection is in an aborted transaction: nothing in it can commit, and the
                # SET cannot run until it is cleared. Clear it (the same thing resolve()'s old
                # aborted-transaction probe did) and re-bind — LOGGED, never silent.
                log.warning("entity_registry.aborted_transaction_cleared",
                            op=op, schema=schema, construction_site=site, error=str(e)[:160])
                try:
                    self.db_conn.rollback()
                except Exception as _re:  # noqa: BLE001
                    _err = _re
                    break
            except Exception as e:  # noqa: BLE001
                _err = e
                break
        msg = (f"entity_registry.search_path_bind_failed: could not bind search_path to tenant "
               f"schema '{schema}' for {op} (registry built at {site}): {_err}. Connection "
               f"tenant binding is unknown — refusing to continue (public-schema fallthrough / "
               f"cross-tenant pollution risk).")
        log_crit(log, msg, op=op, schema=schema, construction_site=site,
                 error=str(_err), component="search_path")
        raise RegistrySearchPathError(msg) from _err

    def _rebind_after_rollback(self, op: str) -> None:
        """Re-bind after a rollback THIS module performed, for the CALLER's next statement.

        The registry's own next method entry re-binds anyway; this exists because the caller
        (e.g. /ingest, auto_commit=False) keeps using the connection after the registry
        returns or raises. Best-effort by design — it runs inside except paths where the
        original error must propagate, and the seam has already logged CRIT on any refusal.
        """
        try:
            self._bind_tenant_schema(op)
        except Exception:  # noqa: BLE001 — logged CRIT by the seam; original error wins
            pass

    @staticmethod
    def _is_valid_uuid(value: str) -> bool:
        """Check if a string is a valid UUID."""
        try:
            uuid.UUID(value)
            return True
        except (ValueError, AttributeError):
            return False

    # ── MEASUREMENT-FAMILY COMPOUND REDIRECT (issue #21) ─────────────────────────────────────
    # THE LIVE WOUND (deploy #10/#11, alias rows verified twice): after the lisinopril take-frame
    # the tenant holds an entity + alias 'lisinopril dosage' though the stored quantity row is
    # correct — and the dosage QUERY then anchors the phrase-entity. The W1 harvest suppression
    # (main.py::_suppress_measurement_family_mints) filters EDGES beside a landed family scalar,
    # but the live mint lanes (the GLiNER2 candidate/relation surface, the LLM relation-fill, the
    # correction re-extraction, an atomizer reframe of a turn that does contain the family noun)
    # hand the '<substance> <family-noun>' surface DIRECTLY to this registry — no edge filter can
    # see them. This seam is where every lane converges (spine backbone attach, /ingest subject +
    # object resolution, GLiNER2 candidates): a surface becomes an entity + alias HERE or never.
    #
    # THE RULE (the issue's option (a), structural — no substance list): when the surface is a
    # compound whose TRAILING token is a measurement-family member (``dosage_noun`` ∪
    # ``measure_noun`` cue classes + their canonical values, floor ∪ tenant via the overlay —
    # THE established cue rails) and the STRIPPED PREFIX already resolves to a KNOWN entity in
    # this tenant (the substance: 'lisinopril dosage' → 'lisinopril'), the phrase is not a new
    # referent — it NAMES THE SUBSTANCE'S MEASURABLE ASPECT, whose value lives on
    # entity_attributes (the #14/#18 frame). resolve() returns the SUBSTANCE's id and never
    # registers the phrase alias: no island entity, no anchorable phrase, and the caller's edge
    # lands on the referent the phrase actually denotes (fail-toward-not-losing — the edge is
    # kept, correctly bound, not dropped). The prefix-known requirement is the guard against
    # refusing legitimate names: a surface whose prefix is NOT a known entity ('High Level' the
    # city, 'Perfect Dosage' a song title) mints exactly as today — first exposure of both
    # tokens is never suppressed. Genuine compound PROPNs ('Unifi Dream Machine',
    # 'blue-ringed octopus') are unaffected by construction: 'machine'/'octopus' are not family
    # members. Fail-safe: any miss → None → today's mint, byte-for-byte.
    def _measurement_family_compound_redirect(self, cur, user_id: str, name: str):
        """Redirect a '<substance> <family-noun>' compound to the KNOWN substance entity.

        Returns the substance's entity_id when (a) the surface has ≥2 tokens, (b) the trailing
        token is a measurement-family member (cue rails, floor ∪ tenant), and (c) the stripped
        prefix resolves to an existing alias/entity in this tenant. Returns None otherwise
        (fail-safe: any error → None → the caller mints as today)."""
        try:
            if not REGISTRY_FAMILY_COMPOUND_REDIRECT:
                return None
            _toks = [t for t in re.split(r"[^a-z0-9ñáéíóúü]+", str(name or "").strip()) if t]
            if len(_toks) < 2:
                return None
            _trailing = _toks[-1]
            _fam = self._measurement_family_members()
            if not _fam or _trailing not in _fam:
                return None
            _prefix = " ".join(_toks[:-1])
            # the prefix must be a KNOWN referent — the alias registry is authoritative (the
            # same probe shape as the alias lookup above, user-pinned first).
            cur.execute(
                "SELECT entity_id FROM entity_aliases "
                "WHERE alias = %s "
                "ORDER BY CASE WHEN entity_id = %s THEN 0 ELSE 1 END, is_preferred DESC "
                "LIMIT 1",
                (_prefix, user_id),
            )
            _row = cur.fetchone()
            if not _row:
                return None
            _eid = _row[0]
            if not _eid or (_eid.count('-') != 4 and _eid != 'user'):
                return None
            log.info("entity_registry.measurement_family_phrase_redirected",
                     surface=name, substance_prefix=_prefix, family_noun=_trailing,
                     entity_id=_eid, user_id=user_id,
                     note="'<substance> <family-noun>' compound resolves to the KNOWN substance "
                          "entity (the aspect's value lives on entity_attributes) — no phrase "
                          "entity/alias minted (issue #21 mint-lane class)")
            return _eid
        except Exception:  # noqa: BLE001 — fail-safe: the redirect never blocks the mint path
            return None

    def _measurement_family_members(self) -> frozenset:
        """dosage_noun ∪ measure_noun cue-class members ∪ their canonical values, resolved via
        the per-tenant overlay (floor ∪ tenant, tenant wins, suppressions honored) with the
        ContextVar bound to THIS registry's schema (the harvest's set/reset pattern). Fail-safe:
        the overlay's own bootstrap floors; any error → empty (caller mints as today)."""
        try:
            from src.api import linguistic_cue_overlay as _lco
            from src.api import rel_type_overlay as _rto
            _dsn = os.environ.get("POSTGRES_DSN", "")
            _schema = getattr(self, "schema_name", None) or ""
            _tok = None
            if _schema:
                _tok = _rto.set_current_schema(_schema)
            try:
                _members = set(_lco.resolve_dosage_nouns(_dsn)) \
                    | set(_lco.resolve_measure_nouns(_dsn))
                _canon = dict(_lco.resolve_dosage_canonical_map(_dsn))
                _canon.update(_lco.resolve_measure_canonical_map(_dsn))
            finally:
                if _tok is not None:
                    try:
                        _rto.reset_current_schema(_tok)
                    except Exception:  # noqa: BLE001 — never let the reset raise
                        pass
            return frozenset(_members | {v for v in _canon.values() if v})
        except Exception:  # noqa: BLE001 — DB/import down → empty → today's mint
            return frozenset()

    def _measurement_family_bare_surface_refused(self, name: str) -> bool:
        """Issue #22: a BARE single-token surface that IS a cue-rail family member is the
        family CANONICAL itself — an ATTRIBUTE NAME, never an entity referent.

        The live wound (deploy #12 smoke, rows verified): the #21 redirect killed the
        '<substance> dosage' phrase, but the dosage query then rendered 'Lisinopril is related
        to Quantity / Quantity's Quantity is 10 milligrams' — a bare 'quantity' entity (alias
        pref=false) minted from the canonical. The #21 gate needs a known-entity PREFIX; a
        bare surface has an empty prefix, so the redirect declined and the registry minted.
        MEASURED mint windows (probe on a fresh tenant): (a) the HARVEST-time backbone attach —
        the canonical's own rel_type only registers at /ingest, so a first-turn harvested edge
        carrying the bare noun resolves BEFORE any rel_type-name guard exists; (b) /ingest when
        the bare-noun edge precedes the rel-minting edge in the batch. Once the rel IS
        registered, the existing /ingest rel_type_name_collision pre-check and this registry's
        own rel-type HARD CONSTRAINT already refuse the surface — the bare refusal closes the
        pre-registration window only.

        THE GUARD (issue #23 default-deny — genuine names still mint, frozen bar 2):
        • full-surface-registered-first: resolve()'s alias probe returns EARLY before this
          refusal, so an already-registered name ('my cat is named Quantity' said once, then
          walked forever) resolves normally;
        • naming rels are SCALAR-tailed (pref_name/also_known_as — migration 039's own class),
          so a naming edge's object is string-kept at /ingest and never reaches resolve() at
          all — the name files through register_alias, a different method this refusal never
          touches;
        • the refusal fires by DEFAULT at the surrogate-registration block; the frames that
          bind a NAME as a RELATIONAL edge's subject (the genitive-name chain's /ingest site
          — 'my friend's name is Level' → (level, friend_of, user) — the relation-pattern name
          lane, and the correction re-ground lanes) declare `name_intent=True`, pinned by the
          AST census (tests/test_mint_guard_default_deny_x23.py).
        Fail-safe: any error → False → today's mint, byte-for-byte.
        """
        try:
            if not REGISTRY_FAMILY_COMPOUND_REDIRECT:
                return False
            _toks = [t for t in re.split(r"[^a-z0-9ñáéíóúü]+", str(name or "").strip()) if t]
            if len(_toks) != 1:
                return False
            _fam = self._measurement_family_members()
            if not _fam or _toks[0] not in _fam:
                return False
            return True
        except Exception:  # noqa: BLE001 — fail-safe: the refusal never blocks the mint path
            return False

    def _indefinite_anaphor_bare_surface_refused(self, name: str) -> bool:
        """Issue #30 (the #23 default-deny family extended): a surface that IS an
        indefinite-anaphor function word — 'one'/'ones'/'each', or the pure-anaphor phrases
        'each one'/'every one'/'each of them' — is never an entity referent.

        The live wound (deploy #15): the spine's copula-measure chain resolved the atomized
        clause 'Each one is roughly 90 centimeters tall.' with subject 'one' (the 'each of
        them' variant: 'each'), /ingest minted the surrogate, and the row
        ``(one, height, '90')`` landed — junk entity, UNIT STRIPPED, unreachable by the
        how-tall walk. These surfaces are CLOSED-CLASS GRAMMAR WORDS (the distributive
        quantifier + the indefinite pronoun — CGEL ch.17 'one-anaphora'), so refusing them can
        never lose user content; a genuine referent NAMED 'One' files through the naming
        frames (``register_alias``, SCALAR-tailed naming rels) and the alias probe above then
        resolves it forever after. Fail-safe: any error → False → today's mint, byte-for-byte."""
        try:
            _norm = " ".join(str(name or "").strip().lower().split())
            if not _norm:
                return False
            if _norm in ("each one", "every one", "each of them"):
                return True
            _toks = [t for t in re.split(r"[^a-z0-9ñáéíóúü]+", _norm) if t]
            return len(_toks) == 1 and _toks[0] in ("one", "ones", "each")
        except Exception:  # noqa: BLE001 — fail-safe: the refusal never blocks the mint path
            return False

    def resolve(self, user_id: str, name: str, name_intent: bool = False) -> str:
        """
        Resolve a name or alias to its canonical entity ID (UUID surrogate).
        If name is a known alias, returns the canonical ID.
        If name is already a UUID, returns it unchanged.
        If name is unknown, generates a UUID v5 surrogate and registers it.

        DEFAULT-DENY mint guard (issue #23): a BARE single-token measurement-family surface
        (the canonical itself — 'quantity', an ATTRIBUTE NAME, never a referent) REFUSES the
        mint and raises MeasurementFamilySurfaceError, for EVERY caller, unless this call is a
        NAMING FRAME that declares `name_intent=True`. This inverts the #22 opt-in (kwargs at
        5 adjudicated sites) which missed the live sixth mint path: the pending-scalar-grounding
        concept queue minted the alias during /harvest-spans, and the alias probe's EARLY
        RETURN then bypassed every guarded resolve downstream — an unguarded first mint
        poisons the whole seam, so the guard must hold by DEFAULT, at the registration block
        itself. Naming frames opt out: the SCALAR-tailed naming rels keep naming objects
        string-kept (register_alias, a different method), so the only resolve()-level opt-outs
        are the frames that bind a NAME as a RELATIONAL edge's subject (the genitive-name
        chain at /ingest, the relation-pattern name lane, the correction re-ground lanes) —
        each is classified in the AST census (tests/test_mint_guard_default_deny_x23.py).
        """
        original_name = name
        name = name.lower().strip()
        if len(name) > _ENTITY_NAME_MAX_LEN:
            log.warning(
                "entity_registry.name_truncated",
                name_length=len(name),
                user_id=user_id,
            )
            name = name[:_ENTITY_NAME_MAX_LEN]
        log.info("entity_registry.resolve_start", original_name=original_name, normalized_name=name, user_id=user_id)
        if not name:
            raise ValueError("Entity name cannot be empty")
        # THE BIND — precedes the alias-lookup READ below, which used to trust the connection.
        self._bind_tenant_schema("resolve")

        # Special case: 'user' and any first-person pronoun ("i"/"me"/"my"/"myself"/
        # "mine") resolve to the canonical user entity ID — never a fresh surrogate.
        # This is the deterministic grounding seam for first-person reference: the
        # pronoun "I" is the requesting user, so it must ground to user_id and never
        # mint a phantom "i" entity (subject-agnostic — applies in object position too).
        # If user_id is a valid UUID, use it directly.
        # If not (e.g., test user strings), derive a deterministic UUID surrogate.
        if name == "user" or name in _FIRST_PERSON_PRONOUNS:
            entity_id = user_id if self._is_valid_uuid(user_id) else _make_surrogate(user_id, user_id)
            log.info("entity_registry.resolve_user_special_case", entity_id=entity_id)
            # Ensure the user entity exists (per-user schema, no user_id column needed)
            with self.db_conn.cursor() as cur:
                # search_path bound at method entry (_bind_tenant_schema) — no inline re-assert.
                cur.execute(
                    "INSERT INTO entities (id, entity_type) "
                    "VALUES (%s, 'Person') "
                    "ON CONFLICT (id) DO NOTHING",
                    (entity_id,),
                )
            self.db_conn.commit()
            log.info("entity_registry.resolve_returning_user", return_value=entity_id)
            return entity_id

        with self.db_conn.cursor() as cur:
            # Check if it's a known alias (but only if it points to a valid UUID)
            cur.execute(
                "SELECT entity_id, display_form FROM entity_aliases "
                "WHERE alias = %s "
                "ORDER BY CASE WHEN entity_id = %s THEN 0 ELSE 1 END, is_preferred DESC",
                (name, user_id),
            )
            row = cur.fetchone()
            log.info("entity_registry.alias_query_executed", name=name, user_id=user_id, found=row is not None)
            if row:
                entity_id = row[0]
                # ── DISPLAY-CASE SELF-HEAL (migration 214) ──────────────────────────────
                # This is the path a KNOWN name takes, and it is the COMMON one: an alias row
                # is usually already present (minted by an earlier turn, or by the /harvest-spans
                # ±6 backbone attach before /ingest runs), so the INSERT below never executes
                # and casing captured at the INSERT alone would be recorded for almost nothing.
                # Measured: a full spine ingest of "…Diane lives in Toronto…" wrote every alias
                # through this early return, leaving display_form NULL for all of them.
                #
                # So the casing observed for THIS turn is written here, and ONLY when the stored
                # value is still NULL — never overwriting a form already captured (observation is
                # monotone; a name does not change case because one later turn saw it
                # differently). No extra round trip: display_form rides the SELECT above. Fires
                # at most once per name per tenant.
                _observed = display_form_for(name)
                if _observed and row[1] is None:
                    try:
                        cur.execute(
                            "UPDATE entity_aliases SET display_form = %s "
                            "WHERE alias = %s AND entity_id = %s AND display_form IS NULL",
                            (_observed, name, entity_id),
                        )
                    except Exception as _dfe:  # noqa: BLE001
                        # Casing is presentation; the resolution it rides on is not. Never let a
                        # cosmetic write break entity resolution — degrade to lowercase.
                        log.warning("entity_registry.display_form_heal_failed",
                                    alias=name, error=str(_dfe)[:160])
                log.info("entity_registry.alias_query_result", name=name, user_id=user_id, entity_id=entity_id, uuid_count=entity_id.count('-') if entity_id else 0)
                # Validate that entity_id is a UUID (not a corrupted string)
                # Corrupted entries should be skipped and treated as unknown
                if entity_id and (entity_id.count('-') == 4 or entity_id == 'user'):
                    log.info("entity_registry.resolve_alias_found", alias=name, entity_id=entity_id)
                    log.info("entity_registry.resolve_returning_alias", name=name, return_value=entity_id)
                    return entity_id
                # If entity_id is a string (corrupted), fall through to generate a proper UUID

            # Check if it's already a canonical UUID (exact match)
            # Per-user schema isolation: entities table has no user_id column; schema isolation is sufficient
            cur.execute(
                "SELECT id FROM entities WHERE id = %s",
                (name,),
            )
            row = cur.fetchone()
            log.info("entity_registry.uuid_query_executed", name=name, user_id=user_id, found=row is not None)
            if row:
                entity_id = row[0]
                # Validate that entity_id is actually a UUID (not corrupted string)
                # Corrupted entries (display name strings in id column) must be skipped
                if entity_id and (entity_id.count('-') == 4 or entity_id == 'user'):
                    log.info("entity_registry.resolve_uuid_found", name=name)
                    log.info("entity_registry.resolve_returning_uuid", name=name, return_value=entity_id)
                    return entity_id
                else:
                    # Corrupted: entity_id is a string, not a UUID - fall through to generate proper UUID
                    log.warning("entity_registry.corrupted_string_entity_id_in_entities",
                               entity_id=entity_id, name=name, user_id=user_id)

            # HARD CONSTRAINT: Reject rel_type names from being registered as entities
            # Check if this name is a known rel_type (prevents parent_of, instance_of, etc. from becoming entities)
            # rel_types exists in both per-user schema and public; search_path resolves correctly
            cur.execute(
                "SELECT rel_type FROM rel_types WHERE LOWER(rel_type) = %s",
                (name,),
            )
            rel_type_row = cur.fetchone()
            if rel_type_row:
                log.error("entity_registry.rel_type_as_entity_rejected",
                         name=name, rel_type=rel_type_row[0], user_id=user_id,
                         message="HARD CONSTRAINT: rel_type names cannot be registered as entities")
                raise ValueError(f"Cannot register rel_type '{name}' as an entity (HARD CONSTRAINT)")

            # MEASUREMENT-FAMILY COMPOUND REDIRECT (issue #21): '<substance> <family-noun>'
            # ('lisinopril dosage') never mints — it names the KNOWN substance's measurable
            # aspect, so resolve to the substance and keep the caller's edge on the true
            # referent. Runs at the MINT seam so every producer lane (spine backbone attach,
            # /ingest subject+object, GLiNER2 candidates, correction re-extract) sees it.
            # None → today's surrogate mint, byte-for-byte.
            _redirected = self._measurement_family_compound_redirect(cur, user_id, name)
            if _redirected:
                log.info("entity_registry.resolve_returning_family_redirect",
                         name=name, return_value=_redirected)
                return _redirected

            # BARE FAMILY-NOUN REFUSAL — DEFAULT-DENY (issues #22+#23): a single-token surface
            # that IS the family canonical ('quantity') is an ATTRIBUTE NAME, not a referent —
            # refuse the mint for EVERY caller that is not a declared NAMING FRAME
            # (name_intent=True). The #22 opt-in (edge kwargs at 5 sites) missed the LIVE sixth
            # mint path (the harvest's pending-scalar-grounding concept queue, deploy-13): the
            # unguarded mint registered the alias BEFORE /ingest ran, and the alias probe's
            # EARLY RETURN above then bypassed every guarded site — an unguarded FIRST mint
            # poisons the whole seam, so the guard holds by default at the registration block.
            # The alias probe above still returns first for any REGISTERED name ('my cat is
            # named Quantity' said once, walked forever). ValueError subclass → /ingest drops
            # the edge LOUDLY (recorded, turn retained); growth lanes skip the rung via their
            # existing except-Exception; the census pins every call site's classification.
            if self._measurement_family_bare_surface_refused(name) and not name_intent:
                log.warning("entity_registry.measurement_family_bare_surface_refused",
                            surface=name, user_id=user_id,
                            name_intent=name_intent,
                            note="the measurement-family canonical is an ATTRIBUTE NAME, not "
                                 "an entity referent — DEFAULT-DENY mint refusal (issue #23); "
                                 "the caller's lane drops/handles loudly, the turn is retained")
                raise MeasurementFamilySurfaceError(
                    f"the surface '{name}' is the measurement-family canonical itself (an "
                    f"attribute name), not an entity referent (issues #22+#23 bare family "
                    f"noun — default-deny mint guard; declare name_intent=True only from a "
                    f"genuine naming frame)")

            # INDEFINITE-ANAPHOR SURFACE REFUSAL — DEFAULT-DENY (issue #30, the #23 family
            # extended to the anaphor class): 'one'/'ones'/'each' (and the pure-anaphor
            # phrases 'each one'/'every one'/'each of them') are CLOSED-CLASS FUNCTION WORDS,
            # never referents — the deriver resolves the anaphor to the plural discourse topic
            # (or declines), and this seam refuses the bare surface should any other lane
            # (a GLiNER2 candidate, a drifted chain) reach the registry with it. The alias
            # probe above has ALREADY returned for registered names, so a genuine thing named
            # 'One' (filed by the naming frames through register_alias) is untouched.
            if self._indefinite_anaphor_bare_surface_refused(name) and not name_intent:
                log.warning("entity_registry.indefinite_anaphor_surface_refused",
                            surface=name, user_id=user_id,
                            name_intent=name_intent,
                            note="the surface is an indefinite-anaphor function word "
                                 "('each one'/'one'/'each of them' — distributive over a "
                                 "plural set, CGEL ch.17), not an entity referent — "
                                 "DEFAULT-DENY mint refusal (issue #30); the caller's lane "
                                 "drops/handles loudly, the turn is retained")
                raise IndefiniteAnaphorSurfaceError(
                    f"the surface '{name}' is an indefinite-anaphor function word (distributive "
                    f"over a plural set), not an entity referent (issue #30 default-deny mint "
                    f"guard; declare name_intent=True only from a genuine naming frame)")

            # Unknown — generate UUID v5 surrogate and register (per-user schema, no user_id column)
            surrogate = _make_surrogate(user_id, name)
            log.info("entity_registry.resolve_generating_surrogate", name=name, surrogate=surrogate, surrogate_has_dashes=surrogate.count('-'))
            try:
                # Probe for aborted transaction before attempting registration (the display_form
                # heal above may have aborted it). A rollback DISCARDS the entry bind, so the
                # write below re-binds through the seam — public has a different unique key
                # ((user_id, alias) vs (entity_id, alias)) and must never receive this INSERT.
                try:
                    cur.execute("SELECT 1")
                except Exception:
                    self.db_conn.rollback()
                    self._rebind_after_rollback("resolve.register")
                cur.execute(
                    "INSERT INTO entities (id, entity_type) "
                    "VALUES (%s, 'unknown') "
                    "ON CONFLICT (id) DO NOTHING",
                    (surrogate,),
                )
                # display_form: the OBSERVED casing of this name in the user's verbatim turn
                # (src/extraction/display_case.py). NULL when nothing was observed → the
                # renderer falls back to `alias`, i.e. today's lowercase output. COALESCE on
                # conflict so a later turn that happens not to observe the name never ERASES
                # casing already captured — observation is monotone.
                cur.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, is_preferred, display_form) "
                    "VALUES (%s, %s, true, %s) "
                    "ON CONFLICT (entity_id, alias) DO UPDATE SET "
                    "is_preferred = EXCLUDED.is_preferred, "
                    "display_form = COALESCE(EXCLUDED.display_form, entity_aliases.display_form)",
                    (surrogate, name, display_form_for(name)),
                )
                self.db_conn.commit()
                log.info("entity_registry.registered", surrogate=surrogate, alias=name, user_id=user_id,
                         caller=_mint_caller_tag())
                log.info("entity_registry.resolve_returning", name=name, return_value=surrogate, is_uuid=surrogate.count('-') == 4)
                return surrogate
            except Exception as e:
                log.error("entity_registry.resolve_registration_failed", name=name, error=str(e))
                raise

    def register_alias(
        self,
        canonical: str,
        alias: str,
        is_preferred: bool = False,
        entity_type: str = 'unknown',
        preference_source: str = 'unspecified',
    ) -> dict:
        """
        Register an alias for a canonical entity (UUID).
        canonical is a UUID string (already lowercase).
        alias is a display name.
        If is_preferred=True, clears other preferred aliases for this entity.
        entity_type: Entity type (e.g., "Person", "Animal", "Organization")
                    Default 'unknown' for backward compatibility
        preference_source: Provenance of the preference (ALIAS-PROVENANCE-DESIGN).
                    One of user_stated/rel_default/inferred/merge/provisioned/unspecified.
                    A newly-preferred alias may only DEMOTE an existing preferred alias
                    when preference_rank(new) >= preference_rank(incumbent). If the
                    incoming preference is weaker, the incumbent keeps is_preferred=true
                    and the new alias is stored as is_preferred=false (never lost).

        User-authoritative: if the alias already exists pointing to a corrupted
        (string) entity_id, delete it first so the correct UUID registration wins.

        Per-user schema isolation: no user_id parameter needed (schema itself provides isolation).
        """
        canonical = canonical.strip()
        alias = alias.lower().strip()
        if len(alias) > _ENTITY_NAME_MAX_LEN:
            log.warning(
                "entity_registry.alias_truncated",
                alias_length=len(alias),
                alias_prefix=alias[:20],
            )
            alias = alias[:_ENTITY_NAME_MAX_LEN]

        # THE BIND on entry. `_get_valid_entity_types` re-binds after any rollback it performs, so
        # the write cursor below is bound whichever path the type probe took.
        self._bind_tenant_schema("register_alias")
        # Validate entity_type against known types
        valid_types = self._get_valid_entity_types()
        if entity_type not in valid_types and entity_type != 'unknown':
            log.warning("entity_type_not_recognized",
                       entity_type=entity_type,
                       available_types=valid_types)
            # Use 'unknown' as fallback, but log the issue for re-embedder awareness
            actual_type = 'unknown'
        else:
            actual_type = entity_type

        try:
            with self.db_conn.cursor() as cur:
                # search_path bound at method entry (_bind_tenant_schema) — no inline re-assert.
                # ──────────────────────────────────────────────────────────────
                # ARRIVAL WELD GUARD (src/entity_registry/weld_guard.py).
                #
                # This is the ONE choke point every alias WELD flows through — a weld being an
                # ADDITIONAL, different surface added to an entity that already has labels,
                # which in the SKOS label model asserts the two surfaces label ONE entity. The
                # documented-but-unenforced invariant in src/api/canonicalize.py ("the caller
                # passes only common-noun TYPE-node surfaces — never a NAME") is enforced HERE,
                # for every caller, rather than in that wrapper: production measurement showed
                # the corrupting welds arrive through the ingest identity lane
                # (the main.py register_alias call site), not through the wrapper.
                #
                # Placed BEFORE the entities upsert: a label judged not to denote this entity
                # must not be able to TYPE it either, so a refusal writes NOTHING AT ALL. (An
                # earlier revision ran the upsert first; that left a refused claim still
                # setting entity_type, which is a smaller version of the same error.)
                #
                # `schema_name` is passed so the guard can derive the SEAT OWNER's entity
                # structurally from the bound schema and exempt it unconditionally — refusing a
                # label there would detach the user's own name from the first-person hook.
                # `is_preferred` is passed so an explicit prefLabel assertion (the naming /
                # correction lane) reaches the user-is-truth provenance override and demotion
                # guard below instead of being vetoed before them.
                #
                # Default mode is "enforce" → a refusal SKIPS THE WRITE. (This comment said
                # "observe → byte-identical to today"; that was true when the guard shipped and
                # is false now, sitting at the enforcement site itself.) What enforces by
                # default is ARM 1 ONLY: a user-stated, non-preferred label onto an entity the
                # USER has declared a class. ARMs 2 and 3 are built but OFF behind
                # ALIAS_WELD_GUARD_ARM2 / _ARM3 — neither met the zero-false-refusal bar; see
                # weld_guard.arm2_enabled / arm3_enabled.
                # ALIAS_WELD_GUARD=observe is the rollback lever (log only, write as before);
                # =off skips the probes entirely.
                # Fail-open and SAVEPOINT-guarded throughout; a guard error never blocks a write
                # and never poisons the caller's transaction.
                # ──────────────────────────────────────────────────────────────
                if weld_guard.refuse_weld(cur, canonical, alias, preference_source,
                                          is_preferred=is_preferred,
                                          schema_name=self.schema_name):
                    # IDENTITY-HONESTY RESULT (identity-honesty): a refused co-reference is NOT a
                    # write. Returning a shape (not None) lets /ingest count truthfully — refused
                    # writes are NOT counted as registered/preferred.
                    return {"written": False, "refused": True, "preferred": False,
                            "preference_source": preference_source}

                # Ensure canonical entity exists with validated type (per-user schema, no user_id column)
                cur.execute(
                    "INSERT INTO entities (id, entity_type) "
                    "VALUES (%s, %s) ON CONFLICT (id) DO UPDATE "
                    "SET entity_type = EXCLUDED.entity_type WHERE entities.entity_type = 'unknown'",
                    (canonical, actual_type),
                )

                # ──────────────────────────────────────────────────────────────
                # dprompt-121: Collision Detection & Staging
                # Check if this alias is already preferred for a DIFFERENT entity
                # ──────────────────────────────────────────────────────────────
                alias_lower = alias.lower()
                collision_entity = None

                collision_source = None
                if is_preferred:
                    # Only check for collisions if we're trying to set as preferred
                    cur.execute(
                        "SELECT entity_id, preference_source FROM entity_aliases "
                        "WHERE alias = %s AND is_preferred = true "
                        "AND entity_id != %s LIMIT 1",
                        (alias_lower, canonical),
                    )
                    collision_row = cur.fetchone()
                    if collision_row:
                        collision_entity = collision_row[0]
                        collision_source = collision_row[1]

                # ──────────────────────────────────────────────────────────────
                # USER-IS-TRUTH provenance override on the CROSS-ENTITY collision
                # path (ALIAS-PROVENANCE-DESIGN). The same-entity paths already use
                # _PREFERENCE_RANK (~397/~432) to stop a weak source clobbering a
                # strong one; this cross-entity collision path did NOT, so a
                # user_stated correction (rank 5) was demoted below an incumbent
                # rel_default/unspecified auto-capture and frozen in a pending
                # conflict — the user's real name could never win. When the incoming
                # source STRICTLY out-ranks the incumbent's preferred source, the
                # user wins deterministically: demote the incumbent's colliding
                # alias, keep the incoming preferred, and do NOT stage a pending
                # conflict (staging would re-block the correction via the dBug-076
                # gate). Only genuinely AMBIGUOUS ranks (incoming <= incumbent) fall
                # through to conflict staging. Deterministic (rank compare only), NO
                # fuzzy/LLM. Fail-safe: equal/unknown ranks → stage (today's path).
                # ──────────────────────────────────────────────────────────────
                if (collision_entity
                        and preference_rank(preference_source) > preference_rank(collision_source)):
                    cur.execute(
                        "UPDATE entity_aliases SET is_preferred = false "
                        "WHERE entity_id = %s AND alias = %s",
                        (collision_entity, alias_lower),
                    )
                    # Close any pending conflict already staged for this alias so the
                    # async re-embedder resolver does not re-litigate the settled name.
                    cur.execute(
                        "UPDATE entity_name_conflicts "
                        "SET status = 'resolved', resolved_by = 'provenance_override', "
                        "    resolved_at = NOW() "
                        "WHERE alias = %s AND status = 'pending'",
                        (alias_lower,),
                    )
                    log.info(
                        "entity_registry.alias_collision_provenance_override",
                        alias=alias,
                        incumbent_entity=str(collision_entity)[:8],
                        incumbent_source=collision_source,
                        winner_entity=str(canonical)[:8],
                        winner_source=preference_source,
                        note="user-is-truth: higher-trust preference wins deterministically "
                             "over a weaker incumbent on the cross-entity collision path",
                    )
                    is_pref = True
                    # Collision resolved in the user's favour — skip conflict staging.
                    collision_entity = None

                if collision_entity:
                    # ──────────────────────────────────────────────────────────────
                    # COLLISION DETECTED: Two entities claim same preferred name at
                    # EQUAL/AMBIGUOUS trust. Stage for resolution instead of silently
                    # overwriting (a strictly-higher incoming source was already
                    # resolved deterministically above).
                    # ──────────────────────────────────────────────────────────────
                    try:
                        # Get entity names for logging/context
                        cur.execute(
                            "SELECT alias FROM entity_aliases "
                            "WHERE entity_id = %s AND is_preferred = true LIMIT 1",
                            (collision_entity,),
                        )
                        collision_name_row = cur.fetchone()
                        collision_name = collision_name_row[0] if collision_name_row else collision_entity[:8]

                        canonical_name_row = None
                        if canonical != collision_entity:
                            cur.execute(
                                "SELECT alias FROM entity_aliases "
                                "WHERE entity_id = %s AND is_preferred = true LIMIT 1",
                                (canonical,),
                            )
                            canonical_name_row = cur.fetchone()
                        canonical_name = canonical_name_row[0] if canonical_name_row else canonical[:8]

                        # Stage collision for LLM resolution
                        # Migration 051 per-user schema: entity_id_a, entity_id_b, alias
                        cur.execute(
                            "INSERT INTO entity_name_conflicts "
                            "(entity_id_a, entity_id_b, alias, status, created_at) "
                            "VALUES (%s, %s, %s, 'pending', NOW()) "
                            "ON CONFLICT (alias) DO NOTHING",
                            (collision_entity, canonical, alias_lower),
                        )
                        self.db_conn.commit()

                        log.warning(
                            "entity_registry.name_collision_detected",
                            alias=alias,
                            entity_1_id=collision_entity[:8],
                            entity_1_name=collision_name,
                            entity_2_id=canonical[:8],
                            entity_2_name=canonical_name,
                            status="staged_for_llm_resolution"
                        )

                        # Register new entity's alias as NON-preferred (fallback)
                        # This prevents silent overwrite while collision is pending resolution
                        is_pref = False
                    except Exception as e:
                        log.error(
                            "entity_registry.collision_staging_failed",
                            alias=alias,
                            canonical=canonical,
                            collision_entity=collision_entity,
                            error=str(e)
                        )
                        # Rollback collision staging, then re-bind (the rollback discarded the SET).
                        self.db_conn.rollback()
                        self._rebind_after_rollback("register_alias.collision_rollback")
                        is_pref = is_preferred
                else:
                    # No collision: use requested preference
                    is_pref = is_preferred

                # ──────────────────────────────────────────────────────────────
                # Provenance-aware demotion guard (ALIAS-PROVENANCE-DESIGN).
                # A new preferred alias may only demote the incumbent preferred
                # alias when its source rank is >= the incumbent's. A weaker
                # source (e.g. rel_default) can no longer clobber a stronger one
                # (e.g. user_stated) — this is the wren-over-alex poisoning,
                # prevented at the source. The new alias is still stored (never
                # lost), just as is_preferred=false.
                # ──────────────────────────────────────────────────────────────
                _demoted_incumbent = False
                if is_pref:
                    cur.execute(
                        "SELECT alias, preference_source FROM entity_aliases "
                        "WHERE entity_id = %s AND is_preferred = true "
                        "AND alias != %s LIMIT 1",
                        (canonical, alias),
                    )
                    incumbent_row = cur.fetchone()
                    if incumbent_row:
                        incumbent_alias, incumbent_source = incumbent_row
                        if preference_rank(preference_source) < preference_rank(incumbent_source):
                            log.info(
                                "entity_registry.alias_preference_kept_incumbent",
                                canonical=canonical,
                                incumbent_alias=incumbent_alias,
                                incumbent_source=incumbent_source,
                                new_alias=alias,
                                new_source=preference_source,
                            )
                            # Incoming preference is weaker — keep incumbent, store
                            # the new alias as non-preferred.
                            is_pref = False

                if is_pref:
                    # ──────────────────────────────────────────────────────────────
                    # PARALLEL — SERIALISE THE DEMOTE+PROMOTE. There is a SECOND unique
                    # index on this table that this function's ON CONFLICT clause cannot
                    # see: `idx_entity_aliases_one_preferred ON entity_aliases(entity_id)
                    # WHERE is_preferred` (user_schema.sql). "Demote the others, then
                    # insert mine as preferred" is a read-modify-write across two
                    # statements, so two writers that both pass the demote before either
                    # insert BOTH try to hold the one preferred slot → the loser raises
                    # 23505 unique_violation → /ingest answers 400 → the MCP's
                    # `_INGEST_RETRY_STATUSES` excludes 400 as "a deterministic backend
                    # decision" → THE WHOLE CHUNK IS DROPPED.
                    #
                    # This is live TODAY: ingest_document already runs 3 concurrent chunks
                    # per document, and any two of them naming the same entity can hit it.
                    # It is also the reason concurrency could not simply be raised — a
                    # wider fan-out multiplies the collision probability, and every
                    # collision costs a chunk.
                    #
                    # A row lock on the OWNING ENTITY is the fix at the source: it is the
                    # database's own serialisation primitive, its granularity is exactly
                    # the contended resource (one entity's preferred slot), and it needs no
                    # new infrastructure — load-bearing, because Redis must stay OPTIONAL.
                    # Writers for DIFFERENT entities never contend.
                    #
                    # REJECTED — arbitrating the ON CONFLICT on the partial index instead:
                    #   an INSERT accepts exactly ONE conflict target, and the row already
                    #   needs `(entity_id, alias)` for its own idempotent re-registration.
                    #   The two cannot both be expressed, so this cannot be an ON CONFLICT.
                    # REJECTED — a Redis lock: it would make correctness depend on an
                    #   optional component, and it cannot serialise against a writer in a
                    #   process that is not using the queue.
                    # REJECTED — retry-the-whole-request: the caller's transaction has
                    #   already written facts by this point; replaying it is not free and
                    #   not always idempotent.
                    try:
                        cur.execute(
                            "SELECT 1 FROM entities WHERE id = %s FOR UPDATE",
                            (canonical,),
                        )
                        cur.fetchone()
                    except Exception as _lock_err:  # noqa: BLE001
                        # Never let the optimisation become the failure. If the lock cannot
                        # be taken (no entity row yet, older schema), fall through — the
                        # savepoint below still catches the race.
                        log.debug("entity_registry.alias_lock_skipped",
                                  canonical=canonical, error=str(_lock_err))

                    # Clear other preferred aliases for this entity
                    cur.execute(
                        "UPDATE entity_aliases SET is_preferred = false "
                        "WHERE entity_id = %s AND alias != %s",
                        (canonical, alias),
                    )
                    # IDENTITY-HONESTY RESULT: did THIS write displace an incumbent preferred
                    # alias? (rowcount > 0 ⇒ a demotion happened on this call). getattr:
                    # production cursors always expose rowcount, but seam test doubles
                    # (race-cursor fakes) may not — the flag is telemetry, never a gate.
                    _demoted_incumbent = (getattr(cur, "rowcount", 0) or 0) > 0

                # OBSERVED CASING (migration 214). `alias` is lowercase and STAYS the matching /
                # dedup / UUID-v5 / ON CONFLICT key; `display_form` is a pure casing overlay of it,
                # taken from the user's verbatim turn (src/extraction/display_case.py). NULL when
                # this turn observed no casing for the name — the renderer then falls back to
                # `alias`, byte-identical to today. Resolved once here so both INSERT arms below
                # (the normal one and the lost-preferred-race degrade) agree.
                _display = display_form_for(alias)

                # ⚠️ THIS RATCHET IS ALSO WHY A CO-REFERENCE WARRANT IS NOT DURABLE. READ THIS
                # BEFORE RELYING ON A STORED WARRANT ANYWHERE. `_WARRANTED_SOURCES` declares the
                # warrant to be an axis ORTHOGONAL to rank — but it is persisted in
                # `preference_source`, the column this ratchet arbitrates BY RANK, and
                # rank('lexical') = 3 < rank('rel_default') = 4. Measured against a throwaway
                # Postgres driving this very function:
                #     ['lexical']               -> 'lexical'     warranted=True
                #     ['rel_default','lexical'] -> 'rel_default'  warranted=False  (never persists)
                #     ['lexical','rel_default'] -> 'rel_default'  warranted=False  (erased later)
                # So a granted warrant is lost if the alias row already exists at `rel_default`,
                # and an ordinary later re-ingest erases one that did land.
                #
                # THE DIRECTION IS SAFE, WHICH IS WHY THIS IS DOCUMENTED AND NOT HOT-FIXED HERE:
                # a reader that has lost the warrant sees "unwarranted", so the weld guard
                # refuses MORE, never less. Nothing is corrupted; a licence is forgotten.
                # DO NOT "fix" it by raising rank('lexical') — rank means trust, and buying
                # durability with unearned trust is the exact conflation the warrant axis exists
                # to undo (it would also let `lexical` out-rank `inferred` in the merge's
                # preferred-alias recompute). The honest fix is a SEPARATE column merged by
                # UNION rather than by rank: written, deliberately UNAPPLIED, and NOT wired, at
                # migrations/244_alias_coreference_warrant.sql. See
                # the internal design record §9.6.
                #
                # Ratchet preference_source UP only — never downgrade an alias's own
                # provenance (ALIAS-PROVENANCE-DESIGN). Re-registering the SAME alias via
                # a weaker path (e.g. a later rel_default re-extraction of a name the user
                # originally stated) must not erode its user_stated trust, or a legal/dead
                # name could later demote it. A genuine downgrade only ever arrives through
                # the correction/retraction path, not normal re-encounter, so max-by-rank
                # is the correct merge of old and new provenance for the same alias.
                cur.execute(
                    "SELECT preference_source FROM entity_aliases "
                    "WHERE entity_id = %s AND alias = %s",
                    (canonical, alias),
                )
                _existing_src_row = cur.fetchone()
                effective_source = preference_source
                if _existing_src_row and preference_rank(_existing_src_row[0]) > preference_rank(preference_source):
                    effective_source = _existing_src_row[0]

                # Insert/update with proper constraint (per-user schema: unique on entity_id, alias)
                # Persist preference_source so downstream consumers (merge, re-embedder,
                # query) can read WHY this alias is preferred instead of guessing.
                #
                # PARALLEL — SAVEPOINT so a LOST RACE COSTS A FLAG, NOT THE CHUNK. The row
                # lock above serialises the common case, but it is best-effort (a brand-new
                # entity, a deadlock the server breaks with 40P01, an older schema), and a
                # 23505 here previously propagated out of this function, out of the /ingest
                # edge loop, and back to the caller as an HTTP 400 that the MCP does not
                # retry — i.e. the user's sentence was silently discarded over a NAMING
                # FLAG. That trade is backwards: the alias itself is user content and is
                # sacred; which of an entity's aliases is *preferred* is a presentation
                # detail the re-embedder's name-conflict resolver already arbitrates
                # asynchronously.
                #
                # So the failure is absorbed HERE, at the statement, and degraded: roll back
                # to the savepoint (the caller's transaction and every fact it has already
                # written survive — a bare rollback would destroy them) and store the alias
                # NON-preferred. Nothing is lost, nothing is fabricated, and the chunk lands.
                _sp = "fl_alias_pref"
                try:
                    cur.execute(f"SAVEPOINT {_sp}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query _sp is a module-level string constant, not user data
                except Exception:  # noqa: BLE001 — no savepoint (autocommit): legacy path
                    _sp = None
                try:
                    cur.execute(
                        "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source, display_form) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (entity_id, alias) DO UPDATE SET "
                        "is_preferred = EXCLUDED.is_preferred, "
                        "preference_source = EXCLUDED.preference_source, "
                        "display_form = COALESCE(EXCLUDED.display_form, entity_aliases.display_form)",
                        (canonical, alias, is_pref, effective_source, _display),
                    )
                except psycopg2.errors.UniqueViolation as _uv:
                    if not _sp:
                        raise
                    cur.execute(f"ROLLBACK TO SAVEPOINT {_sp}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query _sp is a module-level string constant
                    log.warning(
                        "entity_registry.alias_preferred_race_degraded",
                        canonical=canonical, alias=alias,
                        error=str(_uv)[:200],
                        note="another writer took this entity's preferred slot concurrently; "
                             "the alias is STORED non-preferred (capture kept) — the "
                             "name-conflict resolver arbitrates preference asynchronously",
                    )
                    is_pref = False
                    cur.execute(
                        "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source, display_form) "
                        "VALUES (%s, %s, false, %s, %s) "
                        "ON CONFLICT (entity_id, alias) DO UPDATE SET "
                        "preference_source = EXCLUDED.preference_source, "
                        "display_form = COALESCE(EXCLUDED.display_form, entity_aliases.display_form)",
                        (canonical, alias, effective_source, _display),
                    )
                if _sp:
                    try:
                        cur.execute(f"RELEASE SAVEPOINT {_sp}")  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query, python.lang.security.audit.formatted-sql-query.formatted-sql-query _sp is a module-level string constant
                    except Exception:  # noqa: BLE001
                        pass
                log.info("entity_registry.alias_registered",
                         canonical=canonical, alias=alias, preferred=is_pref)
                # IDENTITY-HONESTY RESULT (identity-honesty): report WHAT LANDED so /ingest can
                # count alias/preference effects truthfully (a name turn is a WRITE, never a
                # zero-capture no-op). written = the alias row landed (insert or upsert);
                # preferred = the effective display flag AFTER the demotion guard + race
                # degradation; demoted_incumbent = this write displaced another preferred alias.
                return {"written": True, "refused": False, "preferred": bool(is_pref),
                        "preference_source": effective_source,
                        "demoted_incumbent": bool(_demoted_incumbent)}
        except psycopg2.IntegrityError as err:
            # Always rollback on error — aborted transactions must be cleared regardless of auto_commit
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            self._rebind_after_rollback("register_alias.error_rollback")
            log.error("entity_registry.alias_constraint_violation",
                     error=str(err),
                     canonical=canonical,
                     alias=alias)
            raise
        except Exception as err:
            # Always rollback on error — aborted transactions must be cleared regardless of auto_commit
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            self._rebind_after_rollback("register_alias.error_rollback")
            log.error("entity_registry.alias_registration_failed",
                     error=str(err),
                     canonical=canonical,
                     alias=alias)
            raise

    def _get_valid_entity_types(self) -> set:
        """Query all valid entity types from database (metadata-driven).

        Returns a set of valid entity types from the entities table.
        Always includes 'unknown' as a valid fallback.
        Falls back to minimal set if DB query fails.
        """
        self._bind_tenant_schema("_get_valid_entity_types")
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("SELECT DISTINCT entity_type FROM entities WHERE entity_type IS NOT NULL")
                rows = cur.fetchall()
                types = {row[0] for row in rows} if rows else set()
                # Always include 'unknown' as valid fallback
                types.add('unknown')
                return types
        except Exception as err:
            # Rollback to clear any aborted transaction state before returning fallback — and
            # re-bind, because the rollback discarded the caller's SET (register_alias writes next).
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            self._rebind_after_rollback("_get_valid_entity_types.error_rollback")
            log.error("failed_to_load_entity_types", error=str(err))
            return {'unknown', 'Person', 'Animal', 'Organization', 'Location', 'Concept'}

    def get_preferred_name(self, canonical: str) -> str:
        """Return preferred display name for entity, or canonical if none set.

        DUMB EXTRACT LAYER: Returns whatever is in the database without validation.
        The /query layer (_populate_preferred_names in main.py) is responsible for
        filtering bad data. Do not add validation here — validate on READ, not WRITE.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        self._bind_tenant_schema("get_preferred_name")
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE entity_id = %s AND is_preferred = true "
                "LIMIT 1",
                (canonical,),
            )
            row = cur.fetchone()
            return row[0] if row else canonical

    def get_any_alias(self, entity_id: str) -> str | None:
        """Return first available alias for an entity (preferred or not).

        Used as fallback when get_preferred_name returns a UUID — ensures
        the entity has at least one human-readable name for display resolution.
        Returns None if no alias exists.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        self._bind_tenant_schema("get_any_alias")
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE entity_id = %s "
                "ORDER BY is_preferred DESC LIMIT 1",
                (entity_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_all_aliases(self, entity_id: str) -> list[str]:
        """Return all display name aliases for a surrogate entity_id.

        Per-user schema isolation: user_id parameter removed (schema itself provides isolation).
        """
        self._bind_tenant_schema("get_all_aliases")
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases "
                "WHERE entity_id = %s",
                (entity_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def get_surrogate_for_user(self, user_id: str) -> str:
        """Return the surrogate UUID for the user entity.
        If user_id is a valid UUID, returns it directly.
        Otherwise derives a deterministic UUID v5 surrogate.
        """
        if self._is_valid_uuid(user_id):
            return user_id
        return _make_surrogate(user_id, user_id)

    def get_canonical_for_user(self, user_id: str) -> str:
        """
        Return the canonical entity ID for this user.
        If user_id is a valid UUID, returns it directly.
        Otherwise derives a deterministic UUID v5 surrogate.
        """
        if self._is_valid_uuid(user_id):
            return user_id
        return _make_surrogate(user_id, user_id)