"""Growth signals — THE ERROR/RECALL ENHANCEMENT LOOP (capture → feed → record).

THE PRINCIPLE (owner): every recall miss (a question the store should answer but
abstains or serves wrong) and every ingest error (a turn that degraded / refused /
dropped) is a GROWTH SIGNAL — the engine's growth seeds exist to catch these, and
"any subject missed is grown into by the spine/engine and not dropped". Before this
module the misses vanished into log lines (or into no record at all) without
feeding the growth machinery systematically.

THE THREE LANES
---------------
* CAPTURE (``GROWTH_SIGNAL_CAPTURE``, default ON) — one durable per-tenant
  ``growth_signals`` row per DISTINCT miss fingerprint (kind | miss_class |
  normalized query tokens), occurrence-counted on re-occurrence, TTL-pruned,
  row-capped — plus ONE structured ``growth_signal.capture`` log line: the
  operator/cortex-readable record (miss class, the sentence, the tokens, the
  served-vs-expected gap, the growth-candidate state).
* FEED (``GROWTH_SIGNAL_FEED``, default ON) — the missed shape rides the
  EXISTING growth seams ONLY, never a new machinery:
    - the missed concept surface lands in ``ontology_evaluations`` in the EXACT
      row shape the LIVE reader consumes — ``classify_unknown_concepts``
      (src/re_embedder/embedder.py, wired on the poll loop) polls
      ``extraction_method='ingest_miss_pushback' AND re_embedder_decision IS
      NULL AND sample_object ...``, fires one bounded "what is X?" call per row
      and grounds ``<concept> subclass_of <parent>`` — the is-a ladder the
      recall walker traverses. The engine grows INTO the missed subject.
    - the admissibility hard line holds: every fed surface passes the SAME
      ``type_term_shape`` gate the ingest writers run (a value, a path, or a
      name never becomes a place). Fail-open on parser absence, mirroring
      ``_queue_concept_for_grounding``'s documented direction.
* RECORD (the dashboard seam) — ``report()`` + the ``GET /growth-signals/report``
  endpoint aggregate the miss/error classes and their growth-candidate state so
  the operator can read the loop at a glance.

THE OUT-PARAM DISCIPLINE (cortex lesson, growth-rails R1): growth candidates
that ride ContextVar writes are DARK on the live sentence pipeline (run_cpu
workers never flow back). This module never relies on ContextVars — it writes
the durable rows directly through the reader-verified seam (grep the consumer
FIRST; the consumer here is classify_unknown_concepts and it reads exactly the
row shape written by ``_feed_candidates``).

NEVER RAISES: every entry point is wrapped fail-safe — a broken capture or a
broken feed must never reshape the turn it observes. CAPTURE IS ADDITIVE: the
ingest/recall paths behave identically with the flags ON; with them OFF not one
byte, not one log line, and not one row changes (the OFF behavior is today's).
"""

import hashlib
import json
import logging
import os
import time

import psycopg2

try:  # structlog wherever the api modules are loaded; plain logging is the fallback
    import structlog
    log = structlog.get_logger()
except Exception:  # pragma: no cover — structlog is a hard dep in every real image
    log = logging.getLogger(__name__)


# ── Flags (env, read per call — an operator can move them without a restart) ──
CAPTURE_FLAG = "GROWTH_SIGNAL_CAPTURE"
FEED_FLAG = "GROWTH_SIGNAL_FEED"


def _flag_on(name: str, default: str = "true") -> bool:
    """The main._flag_on semantics: 0/false/no/off → off, anything else → on."""
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


# ── Bounds (env-live-read like every sibling seam) ────────────────────────────
def _ttl_days() -> int:
    try:
        v = int(os.environ.get("GROWTH_SIGNAL_TTL_DAYS", "30") or "30")
    except (TypeError, ValueError):
        return 30
    return v if v > 0 else 30


def _max_rows() -> int:
    try:
        v = int(os.environ.get("GROWTH_SIGNALS_MAX", "2000") or "2000")
    except (TypeError, ValueError):
        return 2000
    return v if v > 0 else 2000


#: In-process throttle: at most one capture attempt per (tenant, fingerprint) per
#: window — a hot retry loop or a litmus replay pays ONE connect, not one per ask.
#: The SQL upsert stays authoritative for dedup; the throttle only bounds
#: connection churn. Keyed PER TENANT: two seats asking the same question are two
#: signals, not one.
_MIN_CAPTURE_INTERVAL_S = 30.0
_last_capture: dict = {}
_LAST_CAPTURE_MAX = 4096


def _throttled(schema_name: str, fp: str) -> bool:
    now = time.monotonic()
    key = (str(schema_name), fp)
    if _last_capture.get(key, 0.0) and (now - _last_capture[key]) < _MIN_CAPTURE_INTERVAL_S:
        return True
    _last_capture[key] = now
    if len(_last_capture) > _LAST_CAPTURE_MAX:
        for k in list(_last_capture)[: _LAST_CAPTURE_MAX // 4]:
            _last_capture.pop(k, None)
    return False


# ── The missed-shape tokens ───────────────────────────────────────────────────
#: Interrogative scaffold nouns — what the QUESTION format contributes, never the
#: missed subject. A feed row for "name" or "fact" would classify the question's
#: own grammar, not the user's world. This prunes the FEED and the fingerprint
#: tokens; it is not an admissibility gate (type_term_shape stays that authority).
_SCAFFOLD_NOUNS = frozenset({
    "name", "age", "date", "time", "fact", "facts", "thing", "things", "detail",
    "details", "information", "question", "type", "kind", "list", "one",
})

_STOP_TOKENS = frozenset({
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "is", "are", "was",
    "were", "do", "does", "did", "what", "which", "who", "whom", "whose", "when",
    "where", "why", "how", "my", "me", "i", "you", "your", "and", "or", "with",
    "about", "tell", "show", "give", "have", "has", "had", "be", "been", "it",
    "that", "this", "there", "their", "they", "them", "he", "she", "we", "us",
})


def normalize_tokens(text: str) -> list:
    """Deterministic miss-shape tokens: lowercase word tokens, stopword/scaffold
    pruned, length ≥ 2, capped at 12. Grammar-light on purpose — this rides in
    the fingerprint and the operator record, never in a gate."""
    out, seen = [], set()
    for ch in ".,;:!?\"'`()[]{}":
        text = text.replace(ch, " ")
    for tok in (text or "").lower().split():
        if len(tok) < 2 or tok in _STOP_TOKENS or tok in _SCAFFOLD_NOUNS:
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
        if len(out) >= 12:
            break
    return out


def fingerprint(kind: str, miss_class: str, tokens: list) -> str:
    """The dedup key: one row per (kind, miss class, token shape). Paraphrases
    that normalize to the same tokens bump ONE row's occurrence count."""
    return hashlib.sha1(
        "|".join([str(kind), str(miss_class)] + [str(t) for t in (tokens or [])])
        .encode("utf-8")).hexdigest()[:16]


def noun_candidates(query_text: str, limit: int = 3) -> list:
    """The CONCEPT surfaces a missed query named: spaCy NOUN/PROPN lemmas
    (the SAME notion the abstention gates use), scaffold-pruned, deduped,
    order-preserving, capped. Each candidate still has to pass type_term_shape
    before it feeds — this only proposes. Fail-safe → [] (no parse, no feed)."""
    if not query_text or not query_text.strip():
        return []
    try:
        from src.extraction.linguistics import _parse, linguistics_available
        if not linguistics_available():
            return []
        doc = _parse(query_text)
        if doc is None:
            return []
        out, seen = [], set()
        for tok in doc:
            if tok.pos_ not in ("NOUN", "PROPN"):
                continue
            surf = (tok.lemma_ or tok.text or "").strip().lower()
            if len(surf) < 3 or surf in _SCAFFOLD_NOUNS or surf in seen:
                continue
            seen.add(surf)
            out.append(surf)
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001 — a parse fault never breaks the capture
        return []


def _admissible(concept: str) -> bool:
    """THE HARD LINE — term admissibility, the same structural gate the ingest
    concept writers run (type_term_shape): a value, a path, or a name never
    becomes a place. Fail-open on parser absence, mirroring the documented
    direction of _queue_concept_for_grounding's gate."""
    try:
        from src.extraction.linguistics import type_term_shape
        ok, _why = type_term_shape(concept)
        return bool(ok)
    except Exception:  # noqa: BLE001 — fail-open (today's writer behavior)
        return True


# ── The feed target: sample_subject_id markers (the dedup namespaces) ─────────
#: Recall-side misses: one freq-counted candidate row per (rel, concept).
FEED_MARKER_RECALL = "__recall__"
#: Ingest-side type refusals: the refused TYPE noun (the missing admission).
FEED_MARKER_TYPE_REFUSED = "__type_refused__"


def _feed_candidates(cur, schema_name: str, concepts: list, snippet: str,
                     marker: str, rel: str) -> list:
    """Upsert the missed shapes into ontology_evaluations in the EXACT row shape
    the live classify_unknown_concepts reader selects:
        WHERE extraction_method='ingest_miss_pushback'
          AND re_embedder_decision IS NULL AND sample_object IS NOT NULL
    (the ON CONFLICT key is the house (candidate_rel_type, sample_subject_id,
    sample_object) — one occurrence-counted row per concept). Returns the list
    of surfaces actually fed. NEVER raises — one bad row is rolled back to its
    savepoint and skipped (the sp_cue_cand discipline)."""
    fed = []
    _snip = (snippet or "").strip()[:500] or None
    for concept in (concepts or []):
        c = (concept or "").strip().lower()
        if not c or not _admissible(c):
            continue
        try:
            cur.execute("SAVEPOINT sp_gs_feed")
            cur.execute(
                f"INSERT INTO {schema_name}.ontology_evaluations"
                " (candidate_rel_type, candidate_subject_type, candidate_object_type,"
                "  sample_subject_id, sample_object, extraction_method,"
                "  first_text_snippet, decision_reason, occurrence_count, last_seen_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, now())"
                " ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)"
                " DO UPDATE SET"
                "   occurrence_count = ontology_evaluations.occurrence_count + 1,"
                "   last_seen_at = now(),"
                "   extraction_method = EXCLUDED.extraction_method,"
                "   first_text_snippet = COALESCE("
                "       ontology_evaluations.first_text_snippet, EXCLUDED.first_text_snippet)",
                ((rel or "category").lower(), "unknown",
                 # un-typed concept → the classifier knows to type it
                 "unknown", marker, c, "ingest_miss_pushback", _snip,
                 "growth-signal feed: a missed/recall-abstained surface queued for "
                 "background 'what is X?' classification so the is-a ladder grows "
                 "into the missed subject"),
            )
            cur.execute("RELEASE SAVEPOINT sp_gs_feed")
            fed.append(c)
        except Exception as _fe:  # noqa: BLE001 — one bad candidate never aborts the batch
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sp_gs_feed")
            except Exception:  # noqa: BLE001
                pass
            log.debug("growth_signal.feed_row_failed", concept=c[:40],
                      error=str(_fe)[:120])
    return fed


def _prune(cur) -> None:
    """TTL + row cap, the refused_edges discipline: the record stays bounded
    without starving the loop (an operator needing history has the log lines)."""
    try:
        cur.execute(
            "DELETE FROM growth_signals WHERE last_seen_at < now() - "
            f"INTERVAL '{_ttl_days()} days'")
        cur.execute(
            "DELETE FROM growth_signals WHERE id NOT IN ("
            "  SELECT id FROM growth_signals"
            "  ORDER BY last_seen_at DESC, id DESC LIMIT "
            f"{_max_rows()})")
    except Exception as _pe:  # noqa: BLE001 — a prune fault never breaks the capture
        log.debug("growth_signal.prune_failed", error=str(_pe)[:120])


def capture_signal(schema_name, *, kind: str, miss_class: str, query_text: str = "",
                   served: str = "", expected: str = "", context=None,
                   feed_concepts=None, feed_marker: str = FEED_MARKER_RECALL,
                   feed_rel: str = "category") -> dict:
    """Record ONE miss/error signal and feed its missed shape into the growth
    machinery. CAPTURE IS ADDITIVE AND FAIL-SAFE: flags OFF → returns None
    before any write and any log line (today's behavior, byte-for-byte); any
    fault → a debug log and an empty verdict, never a raise into the caller.

    kind       'recall_miss' | 'ingest_error'
    miss_class 'honest_empty' | 'served_vs_asked_gap' | 'escalated' |
               'walk_shed' | 'type_refused' | 'edge_dropped' |
               'nothing_captured' | 'extraction_degraded' | 'chunk_failures'
    served     what the door actually served (the abstention, the refusal, the
               degraded envelope) — the SERVED half of the record.
    expected   what the door SHOULD have served — the EXPECTED half.
    context    door-specific extras (alerts, counts, the refusal dict), JSON.
    feed_concepts   the missed surfaces proposed for growth (each still has to
               pass type_term_shape; the feed flag gates the write).

    Returns {'fingerprint', 'growth_state', 'fed'} on a recorded signal, else {} —
    {} (falsy) on every non-recorded path: flag off, no tenant scope, throttle,
    no DSN, or any fault.
    """
    try:
        if not _flag_on(CAPTURE_FLAG):
            return {}  # OFF = today's behavior: no write, no log, nothing
        if not schema_name or not str(schema_name).startswith("faultline_"):
            return {}  # no per-tenant scope → no public fallthrough, ever
        _schema = str(schema_name)
        toks = normalize_tokens(query_text)
        fp = fingerprint(kind, miss_class, toks)
        if _throttled(_schema, fp):
            return {}
        dsn = os.environ.get("POSTGRES_DSN", "")
        if not dsn:
            return {}
        ctx = {}
        for k, v in (context or {}).items():
            if v is not None:
                ctx[str(k)[:40]] = str(v)[:300]
        conn = None
        try:
            conn = psycopg2.connect(dsn, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {_schema}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
                # THE FEED first: the growth_state records what actually happened.
                fed = []
                if _flag_on(FEED_FLAG) and feed_concepts:
                    fed = _feed_candidates(cur, _schema, feed_concepts,
                                           query_text, feed_marker, feed_rel)
                growth_state = "fed" if fed else "capture_only"
                growth_target = ("ontology_evaluations.concept/" + feed_marker) if fed else None
                cur.execute(
                    "INSERT INTO growth_signals"
                    " (signal_kind, miss_class, fingerprint, query_text,"
                    "  query_tokens, expected, served, context, growth_state,"
                    "  growth_target, occurrence_count, first_seen_at, last_seen_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 1, now(), now())"
                    " ON CONFLICT (fingerprint) DO UPDATE SET"
                    "   occurrence_count = growth_signals.occurrence_count + 1,"
                    "   last_seen_at = now(),"
                    "   served = EXCLUDED.served,"
                    "   context = EXCLUDED.context,"
                    "   growth_state = EXCLUDED.growth_state,"
                    "   growth_target = EXCLUDED.growth_target,"
                    "   query_text = COALESCE(growth_signals.query_text, EXCLUDED.query_text)",
                    (str(kind)[:40], str(miss_class)[:60], fp,
                     (query_text or "").strip()[:500] or None,
                     " ".join(toks)[:300] or None,
                     (expected or "").strip()[:300] or None,
                     (served or "").strip()[:300] or None,
                     json.dumps(ctx),
                     growth_state, growth_target),
                )
                _prune(cur)
            conn.commit()
            # THE CORTEX/OPERATOR-READABLE RECORD — one line per capture: the miss
            # class, the sentence, the tokens, the served-vs-expected, the growth
            # state. `docker logs` + the /growth-signals/report endpoint are the
            # two read doors.
            log.info(
                "growth_signal.capture",
                kind=str(kind), miss_class=str(miss_class), fingerprint=fp,
                schema=_schema[:40],
                sentence=(query_text or "").strip()[:160] or None,
                tokens=toks[:12] or None,
                served=(served or "").strip()[:160] or None,
                expected=(expected or "").strip()[:160] or None,
                growth=growth_state,
                growth_target=growth_target,
                fed=fed[:3] or None,
                occurrence="bumped",
            )
            if fed:
                log.info(
                    "growth_signal.feed",
                    miss_class=str(miss_class), fingerprint=fp,
                    marker=feed_marker, rel=feed_rel,
                    note="missed shape queued to ontology_evaluations/"
                         "ingest_miss_pushback — classify_unknown_concepts grows "
                         "the is-a ladder into the missed subject",
                )
            return {"fingerprint": fp, "growth_state": growth_state, "fed": fed}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as _e:  # noqa: BLE001 — the capture NEVER breaks the observed turn
        log.debug("growth_signal.capture_failed", kind=str(kind),
                  miss_class=str(miss_class), error=str(_e)[:140])
    return {}


def report(schema_name, limit: int = 20) -> dict:
    """THE OPERATOR'S VIEW: counts by kind / miss class / growth state, plus the
    recent records. Soft-fails to the empty payload (a dashboard read must never
    500) — the /structure/grown contract."""
    empty = {"counts_by_kind": {}, "counts_by_miss_class": {},
             "counts_by_growth_state": {}, "recent": []}
    if not schema_name or not str(schema_name).startswith("faultline_"):
        return empty
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        return empty
    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.autocommit = True
        out = {**empty}
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {str(schema_name)}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query — schema from UUID-derived source with validation
            cur.execute("SELECT signal_kind, count(*), COALESCE(sum(occurrence_count), 0) "
                        "FROM growth_signals GROUP BY signal_kind ORDER BY 2 DESC")
            out["counts_by_kind"] = {r[0]: {"signals": int(r[1]), "occurrences": int(r[2])}
                                     for r in cur.fetchall()}
            cur.execute("SELECT miss_class, count(*), COALESCE(sum(occurrence_count), 0) "
                        "FROM growth_signals GROUP BY miss_class ORDER BY 2 DESC")
            out["counts_by_miss_class"] = {r[0]: {"signals": int(r[1]), "occurrences": int(r[2])}
                                           for r in cur.fetchall()}
            cur.execute("SELECT growth_state, count(*) FROM growth_signals "
                        "GROUP BY growth_state ORDER BY 2 DESC")
            out["counts_by_growth_state"] = {str(r[0]): int(r[1]) for r in cur.fetchall()}
            try:
                lim = max(1, min(int(limit or 20), 100))
            except (TypeError, ValueError):
                lim = 20
            cur.execute(
                "SELECT signal_kind, miss_class, query_text, query_tokens, expected,"
                " served, growth_state, growth_target, occurrence_count,"
                " first_seen_at, last_seen_at, context"
                " FROM growth_signals ORDER BY last_seen_at DESC, id DESC LIMIT %s",
                (lim,))
            for row in cur.fetchall():
                out["recent"].append({
                    "kind": row[0], "miss_class": row[1],
                    "sentence": row[2], "tokens": row[3],
                    "expected": row[4], "served": row[5],
                    "growth_state": row[6], "growth_target": row[7],
                    "occurrence_count": int(row[8]),
                    "first_seen_at": row[9].isoformat() if row[9] else None,
                    "last_seen_at": row[10].isoformat() if row[10] else None,
                    "context": row[11],
                })
        return out
    except Exception as _e:  # noqa: BLE001 — soft-fail empty, never a 500
        log.debug("growth_signal.report_failed", error=str(_e)[:140])
        return empty
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
