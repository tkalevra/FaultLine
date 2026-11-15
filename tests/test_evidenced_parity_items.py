# -*- coding: utf-8 -*-
"""THE EVIDENCED-PARITY ITEMS (DEV/BUGS/competitive-eval-20261110/
EVIDENCED-DIRECTIONS-TO-RAG-PARITY.md) — the five implementations, each pinned with the
MEASURED sentence, each flag-gated default ON with OFF = byte-parity, each deterministic
(ZERO LLM anywhere in a fix path):

  ITEM 1 — the DATE-NATURAL SURFACE (RECALL_DATE_NATURAL_SURFACE): the walk's DATED-EVENT
           render emits the natural surface beside the ISO slice. MEASURED: A20/A22-L qT1
           "when did we ship v2.3 and v2.4?" served "You are shipped with V2.3
           (on 2026-06-14)" against gold "June 14" — the fact IS served, the gold tokens
           never matched. Also the Wren kindergarten class (gold 'september').
  ITEM 2 — RECALL RRF FUSION (RECALL_RRF_FUSION) on the band's candidates: the measured
           qF3 race ("what's the safe way to force-push the release branch?") — the
           register seed turn matches ALL THREE sel words and was skipped by the
           shape-blind echo arm; the band served the lease-check fragment. Fusion ranks
           the register row first (overlap + recency + anchor-specificity, k=60,
           arXiv 2501.13956); the echo arm becomes intent-aware.
  ITEM 3 — the DECLARED IDENTIFIER/SPEC SHELF (RECALL_SPEC_SHELF + migration 312): the
           detector's identifier tier already captures (vm, vmid, "112"); the walk
           admission lays the declared shelf into scalar_rels when the question names it
           and the anchor holds a live row, and the existing #13 tail seam exempts it.
           LITMUS: "what's the home assistant vm id?" serves (vm, vmid, '112').
  ITEM 4 — PLURAL-NORMAL-FORM at the novel-rel mint (REL_TYPE_PLURAL_NORMAL_FORM): the
           junk sibling (user, cats, two) must not register the plural 'cats' as
           vocabulary — the singular class form mints instead and the class anchor stays
           available. LITMUS: pet1's "what are our cats' names?" reaches miso/juniper
           via the class anchor.
  ITEM 5 — the QUERY-VARIANT NET (RECALL_ANCHOR_QUERY_VARIANTS) at the anchor-probe
           boundary: dotted→spaced ("v2.3" → "v2 3"), hyphen→space, plural→singular —
           deterministic expansion of the QUERY, never the store.

CONTRACTS held by construction (pinned below where decidable): every fix path is
render/probe/admission-only — the WGM gate is never bypassed (item 3 grows vocabulary
PROPERLY, via the seeded migration; item 4 mints through the same gate with a normalized
name), the negation scan-back contract is untouched (no detector change), and every flag
OFF path is byte-for-byte today's.
"""
import importlib.util
import os
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sc = _load("deterministic_scorer", os.path.join(
    REPO, "benchmarks", "harness", "deterministic_scorer.py"))

import src.api.main as M  # noqa: E402
import src.mcp.server as S  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


# ═════════════════════════════════ ITEM 1 ══════════════════════════════════════════

class TestItem1DateNaturalSurface:
    def test_flag_defaults_on(self):
        assert M.RECALL_DATE_NATURAL_SURFACE is True
        assert os.environ.get("RECALL_DATE_NATURAL_SURFACE", "true") == "true"

    def test_month_gran_names_the_month(self):
        assert M._natural_date_surface("2026-09-01", "month") == "September 2026"

    def test_day_gran_names_the_human_day(self):
        # the measured qT1 shape: the server-side event-line convention (ordinal suffix)
        assert M._natural_date_surface("2026-06-14", "day") == "June 14th, 2026"

    def test_ordinal_english_rule(self):
        assert M._natural_date_surface("2026-06-01", "day") == "June 1st, 2026"
        assert M._natural_date_surface("2026-06-02", "day") == "June 2nd, 2026"
        assert M._natural_date_surface("2026-06-03", "day") == "June 3rd, 2026"
        assert M._natural_date_surface("2026-06-11", "day") == "June 11th, 2026"
        assert M._natural_date_surface("2026-06-13", "day") == "June 13th, 2026"
        assert M._natural_date_surface("2026-06-21", "day") == "June 21st, 2026"

    def test_year_gran_fabricates_nothing(self):
        assert M._natural_date_surface("1998-01-01", "year") is None

    def test_fail_safe_on_garbage(self):
        assert M._natural_date_surface(None, "day") is None
        assert M._natural_date_surface("garbage", "day") is None
        assert M._natural_date_surface("2026-13-01", "month") is None

    def _render(self, event_date, gran, monkeypatch):
        db = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = [("shipped_with", "You are shipped with Y")]
        db.cursor.return_value.__enter__.return_value = cur
        user = str(uuid.uuid4())
        fact = {"_subject_id": user, "object": "V2.3", "rel_type": "shipped_with",
                "fact_class": "B", "source": "db",
                "event_date": event_date, "event_date_granularity": gran}
        prose = M.convert_to_prose(
            [dict(fact)], db, anchor=user, user_id=user,
            preferred_alias_map={user: "you"})
        assert len(prose) == 1
        return prose[0]

    def test_measured_sentence_qT1_serves_the_gold_tokens(self):
        """THE MEASURED SENTENCE (A20/A22-L qT1): gold 'June 14' must token-match the
        served line — the scorer HIT where the ISO-only render missed."""
        line = self._render("2026-06-14", "day", None)
        assert "(on 2026-06-14, June 14th, 2026)" in line
        hit = sc.score_recall("June 14", line)
        assert hit["deterministic_hit"] is True, hit

    def test_wren_kindergarten_class_serves_september(self):
        """The roadmap's ITEM-1 litmus: gold 'september' matches the rendered surface of
        a month-granularity fact ('(on 2026-09)' alone never carried it)."""
        line = self._render("2026-09-01", "month", None)
        assert "(on 2026-09, September 2026)" in line
        hit = sc.score_recall("september", line)
        assert hit["deterministic_hit"] is True, hit

    def test_year_stays_iso_only(self):
        line = self._render("1998-01-01", "year", None)
        assert "(on 1998)" in line
        assert "January" not in line  # a year-only statement names no month

    def test_flag_off_is_byte_parity(self, monkeypatch):
        monkeypatch.setattr(M, "RECALL_DATE_NATURAL_SURFACE", False)
        line = self._render("2026-06-14", "day", None)
        assert "(on 2026-06-14)" in line
        assert "June" not in line
        line_m = self._render("2026-09-01", "month", None)
        assert "(on 2026-09)" in line_m
        assert "September" not in line_m


# ═════════════════════════════════ ITEM 2 ══════════════════════════════════════════

_T2 = None  # recency keys are constructed inside the tests


def _qf3_rows():
    """THE MEASURED qF3 CANDIDATE SET (the A20/A22 seat's episodic rows for
    'what's the safe way to force-push the release branch?', sel words
    [force, release, branch])."""
    import datetime as dt
    return [
        {"text": "the lease check has already saved the release branch once",
         "matches": 2, "recency_key": dt.datetime(2026, 9, 1, 8, 0, 0),
         "specificity": 2.0, "intent": ""},
        {"text": "House rule after last month's incident: force-push only with "
                 "--force-with-lease, never a bare --force — the lease check has "
                 "already saved the release branch once.",
         "matches": 3, "recency_key": dt.datetime(2026, 10, 1, 9, 0, 1),
         "specificity": 1.5, "intent": "STATEMENT"},
        {"text": "after rebuilding an image with the same tag, docker compose up -d "
                 "does nothing to running containers — you need the --force-recreate flag",
         "matches": 1, "recency_key": dt.datetime(2026, 10, 1, 9, 0, 0),
         "specificity": 0.5, "intent": ""},
    ]


class TestItem2RRFFusion:
    def test_flag_defaults_on(self):
        assert S.RECALL_RRF_FUSION is True

    def test_measured_qf3_race_ranks_the_register_row_first(self):
        """THE MEASURED RACE: the register seed turn (matches all 3 sel words, the row
        the gold --force-with-lease lives in) ranks FIRST under fusion."""
        out = S._rrf_fuse_rows(_qf3_rows())
        assert out[0]["text"].startswith("House rule after last month's incident")
        assert "--force-with-lease" in out[0]["text"]

    def test_pure_fusion_flip_where_single_signal_loses(self):
        """The fusion property itself: a row that LOSES the overlap signal alone wins
        under fusion (it leads recency + specificity)."""
        import datetime as dt
        rows = [
            {"text": "aaa other row leads overlap", "matches": 3,
             "recency_key": dt.datetime(2026, 9, 1), "specificity": 0.3, "intent": ""},
            {"text": "bbb register row rare", "matches": 1,
             "recency_key": dt.datetime(2026, 11, 1), "specificity": 1.0, "intent": ""},
            {"text": "ccc filler", "matches": 1,
             "recency_key": dt.datetime(2026, 8, 1), "specificity": 0.3, "intent": ""},
        ]
        out = S._rrf_fuse_rows(rows)
        assert out[0]["text"] == "bbb register row rare"

    def test_recency_rank_is_newest_first(self):
        import datetime as dt
        rows = [
            {"text": "older", "matches": 2, "recency_key": dt.datetime(2026, 1, 1),
             "specificity": 1.0, "intent": ""},
            {"text": "newer", "matches": 2, "recency_key": dt.datetime(2026, 11, 1),
             "specificity": 1.0, "intent": ""},
        ]
        out = S._rrf_fuse_rows(rows)
        assert out[0]["text"] == "newer"

    def test_deterministic_run_to_run(self):
        assert ([r["text"] for r in S._rrf_fuse_rows(_qf3_rows())]
                == [r["text"] for r in S._rrf_fuse_rows(_qf3_rows())])

    def test_k_is_the_standard_60(self):
        assert S._RRF_K == 60

    def test_off_path_keeps_the_legacy_ranking_and_shape_blind_echo(self):
        """OFF = byte-parity, pinned at the source: the legacy branch keeps the exact
        (-count, text) ranking and the shape-blind echo arm (a row matching every sel
        word is skipped REGARDLESS of intent); only the ON branch carries the
        intent-aware echo and the fusion."""
        src = open(S.__file__, encoding="utf-8").read()
        assert "ranked = sorted(cand.items(), key=lambda kv: (-kv[1], kv[0]))" in src
        assert 'if matches >= len(sel_l) and r.get("intent") == "QUERY":' in src
        # the legacy echo arm has NO intent condition (the measured wound): between the
        # legacy fetch and the ranking, the echo check is a bare threshold
        legacy = src.split(
            "SELECT left(raw_text, 240) FROM episodic_log WHERE raw_text ILIKE %s")[1]
        legacy = legacy.split("if not overlapped:")[0]
        assert "if matches >= len(sel_words):" in legacy
        assert "intent" not in legacy

    def test_admission_keeps_the_noise_floor(self):
        """A one-word coincidence on a COMMON word (supported by many candidate rows)
        still never serves under ON — only the ≥2-overlap gate or the sole-carrier
        admission admits a row."""
        src = open(S.__file__, encoding="utf-8").read()
        # SERVE-GAP ROUND 4 (updated WITH evidence): the admission gained the
        # flag-gated QA-short arm (_qa_short, EPISODIC_BAND_QA_SHAPE only) — the
        # OFF noise floor is the SAME ≥2-overlap / sole-carrier pair, now spelled
        # with the two legacy conditions first.
        assert "if matches < 2 and not _rare and not _qa_short:" in src
        assert "_qa_short = (EPISODIC_BAND_QA_SHAPE" in src
        assert "_rare = any(support[w] == 1 for w in sel_l if w in low)" in src

    def test_zero_new_queries(self):
        """The ON path runs the SAME single per-word fetch (one ILIKE per sel word,
        LIMIT 5, recency-ordered) — the SELECT only exposes the columns the fusion
        reads (intent, created_at); the OFF branch keeps the legacy SELECT verbatim.

        ANCHOR RENAMED BY DESIGN (the compose2 band-leg fix, 2026-11-15): the
        branch's empty-gate line became `if not overlapped and _echo_row is
        None:` (the binding-echo extra slot — the guard adds ZERO queries, pure
        in-memory divert), so the split anchor moved with it; the mechanism
        assertion below is byte-identical."""
        src = open(S.__file__, encoding="utf-8").read()
        on = src.split("if RECALL_RRF_FUSION:")[1].split(
            "if not overlapped and _echo_row is None:")[0]
        assert on.count("cur.execute(") == 1  # ONE episodic fetch — zero new queries
        assert ("SELECT left(raw_text, 240), COALESCE(intent, ''), created_at" in src)
        assert "ORDER BY created_at DESC LIMIT 5" in src
        assert "SELECT left(raw_text, 240) FROM episodic_log WHERE raw_text ILIKE %s" in src


@pytest.mark.usefixtures("requires_postgres")
class TestItem2BandDoorLitmus:
    """THE REAL BAND READ on a throwaway schema: the measured qF3 race through the REAL
    _episodic_verbatim_band_once — ON serves the register turn (gold present), OFF
    serves the lease fragment (the measured miss). Skips honestly with no DB."""

    def test_band_serves_the_register_row_on_and_the_fragment_off(self, postgres_dsn,
                                                                  monkeypatch):
        import psycopg2
        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'''
                CREATE TABLE "{schema}".episodic_log (
                    id bigserial PRIMARY KEY,
                    raw_text text NOT NULL,
                    intent text,
                    created_at timestamptz DEFAULT now())
            ''')
            cur.execute(f'''
                INSERT INTO "{schema}".episodic_log (raw_text, intent, created_at) VALUES
                ('House rule after last month''s incident: force-push only with '
                 '--force-with-lease, never a bare --force — the lease check has already '
                 'saved the release branch once.', 'STATEMENT', '2026-10-01 09:00:01+00'),
                ('the lease check has already saved the release branch once',
                 '', '2026-09-01 08:00:00+00'),
                ('after rebuilding an image with the same tag, docker compose up -d does '
                 'nothing to running containers — you need the --force-recreate flag',
                 '', '2026-10-01 09:00:00+00')
            ''')
        conn.close()
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        try:
            band_on = S._episodic_verbatim_band_once(
                seat, "what's the safe way to force-push the release branch?")
            assert band_on.startswith(S._EPISODIC_BAND_LABEL)
            assert "--force-with-lease" in band_on, band_on
            assert "House rule after last month's incident" in band_on
            # the gold HIT, measured
            hit = sc.score_recall("--force-with-lease", band_on)
            assert hit["deterministic_hit"] is True, hit
            monkeypatch.setattr(S, "RECALL_RRF_FUSION", False)
            band_off = S._episodic_verbatim_band_once(
                seat, "what's the safe way to force-push the release branch?")
            # OFF = the measured wound: the register turn is echo-skipped (it matches
            # all three sel words), the fragment serves
            assert "--force-with-lease" not in band_off
            assert "lease check has already saved" in band_off
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()


# ═════════════════════════════════ ITEM 3 ══════════════════════════════════════════

class TestItem3SpecShelf:
    def test_flag_defaults_on(self):
        assert M._RECALL_SPEC_SHELF is True

    def test_measured_sentences_bind_the_declared_shelf(self):
        """THE MEASURED SENTENCES: the litmus question and the smoke question both bind
        the vmid shelf; the serial/part-number class binds its own shelves; an ordinary
        question binds nothing."""
        assert M._query_spec_shelf_rels("what's the home assistant vm id?") == ("vmid",)
        assert M._query_spec_shelf_rels(
            "what is the vmid of the Home Assistant VM?") == ("vmid",)
        assert M._query_spec_shelf_rels(
            "what is the laptop's serial number?") == ("serial",)
        assert M._query_spec_shelf_rels("which part number does the filter take?") \
            == ("part_number",)
        assert M._query_spec_shelf_rels("what model is the enlarger?") == ("model",)
        assert M._query_spec_shelf_rels("where do we live?") == ()
        assert M._query_spec_shelf_rels("how long do we keep application logs?") == ()

    def test_whole_word_match_never_substring(self):
        # 'vmid' inside a longer token must not bind; 'spec' inside 'inspection' neither
        assert M._query_spec_shelf_rels("what is the xvmidfoo value?") == ()
        assert M._query_spec_shelf_rels("the inspection report is filed") == ()

    def test_migration_312_follows_the_310_311_pattern(self):
        """The migration file carries the exact 310/311 contract: five declared shelves,
        tail_types {SCALAR}, category 'system', supersede, string, is_leaf_only,
        confidence 0.8, ON CONFLICT DO NOTHING, and the per-tenant loop."""
        sql = open(os.path.join(REPO, "migrations",
                                "312_seed_identifier_spec_shelves.sql"),
                   encoding="utf-8").read()
        for shelf in ("vmid", "serial", "model", "spec", "part_number"):
            assert f"('{shelf}', " in sql, shelf
        assert "ARRAY['SCALAR']::TEXT[]" in sql
        assert "'supersede', 'builtin', 'system'" in sql
        assert "'string'" in sql
        assert "ON CONFLICT (rel_type) DO NOTHING" in sql
        assert "faultline\\_%" in sql  # the per-tenant loop (310/311's exact pattern)
        assert "INSERT INTO public.rel_types" in sql
        assert "is_leaf_only" in sql or "true," in sql  # the 275 contract columns ride

    def test_declared_shelf_class_is_the_detector_tier(self):
        """The shelf class is the CLOSED class from the detector's identifier tier —
        never a question-side word list, never grown."""
        rels = tuple(r for r, _s in M._IDENTIFIER_SPEC_SHELVES)
        assert rels == ("vmid", "serial", "model", "spec", "part_number")

    def test_admission_is_metadata_coupled_and_scoped_to_the_anchor(self):
        """Source pins: the admission runs ONLY in fetch_facts_from_anchor, gated on the
        flag + a shelf-binding query + a LIVE attribute row on the ANCHOR (one bounded
        read), and lays the shelf into path.scalar_rels (which the #13 tail seam
        exempts)."""
        src = open(M.__file__, encoding="utf-8").read()
        assert "if bool(anchor_uuid) and _RECALL_SPEC_SHELF:" in src
        assert "_spec_rels = _query_spec_shelf_rels(query_text or \"\")" in src
        assert "SELECT DISTINCT attribute FROM entity_attributes" in src
        assert "superseded_at IS NULL" in src
        assert "AND attribute = ANY(%s)" in src
        assert "path.scalar_rels.append(_sa)" in src
        assert "determine_path.spec_shelf_admitted" in src

    def test_tail_seam_exempts_the_admitted_shelf(self):
        """The serving half rides the EXISTING BUG-RECALL-14 seam: a scalar_rels row
        (source=attributes) is exempt from the lexical topic gate — no new exemption
        machinery, admission by construction."""
        src = open(M.__file__, encoding="utf-8").read()
        assert ("if (str(_f.get(\"rel_type\") or \"\").strip().lower() in _mi_rels"
                "\n                        and _f.get(\"source\") == \"attributes\"):"
                in src)


# ═════════════════════════════════ ITEM 4 ══════════════════════════════════════════

class TestItem4PluralNormalForm:
    def test_flag_defaults_on(self):
        assert M.REL_TYPE_PLURAL_NORMAL_FORM is True

    def test_the_measured_wound_normalizes(self):
        """THE MEASURED SENTENCE: pet1's junk sibling minted rel 'cats'; the mint now
        registers the singular class form."""
        assert M._plural_normal_form_rel("cats") == "cat"
        assert M._plural_normal_form_rel("dogs") == "dog"

    def test_verb_form_rels_are_never_touched_or_converge(self):
        """A verb surface with NO noun sense ('owns') is never touched; a verb
        inflection whose base IS also a noun ('runs' → 'run') converges — the same
        direction the read-side canonical fold already applies, and strictly better
        vocabulary than minting the inflected form."""
        assert M._plural_normal_form_rel("owns") is None
        assert M._plural_normal_form_rel("runs") == "run"  # convergent, documented

    def test_non_plural_and_compound_candidates_untouched(self):
        assert M._plural_normal_form_rel("spouse") is None
        assert M._plural_normal_form_rel("has_settings") is None
        assert M._plural_normal_form_rel("cats_pajamas") is None
        assert M._plural_normal_form_rel("cat") is None
        assert M._plural_normal_form_rel("") is None

    def test_all_three_mint_seams_carry_the_arm(self):
        """The both-seams-covered discipline (plus the ingest edge seam so the EDGE
        writes the normal form too): every call site is flag-gated."""
        src = open(M.__file__, encoding="utf-8").read()
        assert src.count("if REL_TYPE_PLURAL_NORMAL_FORM:") == 2  # the two mint fns
        assert ("if REL_TYPE_PLURAL_NORMAL_FORM and not _rel_meta(rel_type_lower):"
                in src)  # the ingest edge seam
        assert src.count("_plural_normal_form_rel(") >= 3  # all three seams call it
        assert "edge.rel_type = _pnf" in src

    def test_known_rels_are_never_rewritten(self):
        """The edge seam's gate: a KNOWN rel (a tenant's own authoritative plural row)
        keeps its identity — normalization fires only on the mint path."""
        src = open(M.__file__, encoding="utf-8").read()
        assert ("if REL_TYPE_PLURAL_NORMAL_FORM and not _rel_meta(rel_type_lower):"
                in src)


# ═════════════════════════════════ ITEM 5 ══════════════════════════════════════════

class TestItem5QueryVariantNet:
    def test_flag_defaults_on(self):
        assert M._RECALL_ANCHOR_QUERY_VARIANTS is True

    def test_dotted_token_expands_spaced(self):
        """THE MEASURED SHAPE (qT1's variance class): 'v2.3' probes the spaced alias."""
        assert M._query_variant_forms("v2.3") == ["v2 3"]
        assert M._query_variant_forms("v2.3.") == []  # trailing punct is stripped upstream

    def test_hyphen_token_expands_spaced(self):
        assert M._query_variant_forms("force-push") == ["force push"]
        assert M._query_variant_forms("0w-20") == ["0w 20"]

    def test_plural_expands_singular_wordnet_guarded(self):
        assert M._query_variant_forms("cats") == ["cat"]
        assert M._query_variant_forms("flags") == ["flag"]

    def test_no_false_singulars(self):
        assert M._query_variant_forms("bus") == []
        assert M._query_variant_forms("cross") == []
        assert M._query_variant_forms("ship") == []

    def test_flag_off_is_byte_parity(self, monkeypatch):
        monkeypatch.setattr(M, "_RECALL_ANCHOR_QUERY_VARIANTS", False)
        assert M._query_variant_forms("cats") == []
        assert M._query_variant_forms("v2.3") == []
        assert M._variant_expanded_words(["ship", "v2.3"]) == [["ship"], ["v2.3"]]

    def test_expansion_map_raw_first(self):
        exp = M._variant_expanded_words(["ship", "v2.3", "cats"])
        assert exp[0] == ["ship"]
        assert exp[1][0] == "v2.3" and "v2" in exp[1]  # split parts (≥2 chars; the
        # spaced PHRASE form "v2 3" rides _query_variant_forms, the parts feed the
        # ILIKE n-gram patterns where a 1-char part would over-match)
        assert exp[2] == ["cats", "cat"]

    def test_all_three_probes_carry_the_net(self):
        """The net lives at the ANCHOR-PROBE boundary (bare-tool-noun + the compound-
        alias containment/subsequence probes) — flag-gated, the main n-gram loop
        untouched."""
        src = open(M.__file__, encoding="utf-8").read()
        assert "_probe_forms = [_tok] + _query_variant_forms(_tok)" in src
        assert "_variant_tokens: list = []" in src
        assert "_orig_tokens + _variant_tokens" in src
        assert "_exp_pat = \"%\" + \"%\".join(_like_escape(_t) for _t in _exp) + \"%\"" in src


# ═══════════════ THE REAL-DOOR LITMUS (throwaway *_test seat, DSN-gated) ═══════════

_DSN = os.environ.get("POSTGRES_DSN", "")
LAB1 = ("The homelab runs Proxmox on the r730; the VM for Home Assistant is vmid 112 "
        "and backs up nightly at 2am to the NAS.")
PET1 = ("We have two cats: Miso is a tabby, and Juniper is a black rescue we adopted "
        "in June.")


class _D:
    def get(self, k):
        return None

    def setex(self, k, t, v):
        return True

    def ping(self):
        return True


class _Idem:
    redis_url = "dict://test"
    ttl = 3600
    client = _D()

    def generate_key(self, text, user_id, endpoint, **kw):
        import hashlib
        return "idem_" + hashlib.sha256(
            f"{text}|{user_id}|{endpoint}".encode()).hexdigest()[:32]

    def get_or_lock(self, key):
        return True

    def get_cached_response(self, key):
        return None

    def is_duplicate(self, key):
        return False

    def cache_response(self, key, response, ttl_seconds=3600):
        return True

    def release_lock(self, key):
        return True


@pytest.fixture(scope="module")
def parity_seat():
    if not _DSN or not _DSN.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("store-side litmus needs a throwaway *_test POSTGRES_DSN (skipped)")
    import psycopg2
    from src.provisioning.schema_manager import (create_user_schema,
                                                 derive_user_slug_from_uuid)
    import src.api.main as M
    # migration 312's public section must be in place BEFORE provisioning so the fresh
    # tenant inherits the declared shelves via the public copy (the 310/311 discipline)
    _here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(_here, "..", "migrations",
                            "312_seed_identifier_spec_shelves.sql"), encoding="utf-8").read()
    pub = sql.split("-- ── 2.")[0]
    c = psycopg2.connect(_DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(pub)
    c.close()
    user_id = str(uuid.uuid4())
    schema, status = create_user_schema(user_id, derive_user_slug_from_uuid(user_id),
                                        psycopg2.connect(_DSN))
    assert status == "ready", status
    conn = psycopg2.connect(_DSN)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, f"{user_id[:8]}@example.invalid", user_id[:8], user_id[:8]))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                    "ready_at) VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING",
                    (user_id, schema))
    conn.commit()
    conn.close()
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    M._idempotency_mgr = _Idem()
    from starlette.testclient import TestClient
    with TestClient(M.app) as client:
        yield {"client": client, "user_id": user_id, "schema": schema, "M": M}
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    conn.close()


def _harvest_and_ingest(env, text):
    """The REAL two-door capture the smoke driver drives: /harvest-spans → /ingest with
    the harvested edges (the deployed rewrite-lane shape minus the brain)."""
    h = env["client"].post("/harvest-spans",
                           json={"text": text, "user_id": env["user_id"]})
    assert h.status_code == 200, h.text[:300]
    edges = h.json().get("edges") or []
    r = env["client"].post("/ingest", json={"text": text, "user_id": env["user_id"],
                                            "edges": edges, "source": "mcp"})
    assert r.status_code == 200, r.text[:300]
    return r.json(), edges


@pytest.mark.usefixtures("requires_postgres")
class TestItem3SpecShelfDoorLitmus:
    """THE ITEM-3 LITMUS through the REAL doors: the identifier edge goes through the
    REAL /ingest (the WGM gate with migration 312's DECLARED shelf — never a bypass),
    lands (vm, vmid, '112') in entity_attributes, and the natural questions — the
    smoke's 'vmid' phrasing AND the spaced 'vm id' phrasing — walk to '112'. (The
    CAPTURE half — the harvest producing (vm, vmid, '112') — is already pinned at the
    detector level by test_family_smoke_extraction_pins.py F2(a); the spine harvest
    fails CLOSED on a brainless seat, so this litmus drives the write door directly.)"""

    def test_vmid_row_lands_and_the_questions_walk_to_it(self, parity_seat):
        import psycopg2
        from src.api.main import determine_path, fetch_facts_from_anchor
        from src.api.models import EdgeInput
        seat, schema = parity_seat["user_id"], parity_seat["schema"]
        r = parity_seat["client"].post("/ingest", json={
            "text": LAB1, "user_id": seat,
            "edges": [EdgeInput(subject="vm", rel_type="vmid", object="112",
                                object_datatype="string",
                                fact_provenance="user_stated").model_dump()],
            "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        rj = r.json()
        assert rj.get("status") in ("valid", "partial"), rj
        # the edge is NEVER dropped at a vocabulary gate — the declared shelf admits it
        drops = rj.get("dropped") or []
        assert not [d for d in drops if d.get("object") == "112"], drops

        conn = psycopg2.connect(_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}", public')
                # the declared shelf is in the fresh tenant (migration 312's public copy)
                cur.execute("SELECT rel_type, tail_types, scalar_datatype FROM rel_types "
                            "WHERE rel_type IN ('vmid','serial','model','spec',"
                            "'part_number')")
                seeded = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
                for shelf in ("vmid", "serial", "model", "spec", "part_number"):
                    assert shelf in seeded, f"{shelf} missing from the fresh tenant"
                    assert "SCALAR" in (seeded[shelf][0] or []), seeded[shelf]
                # the row: (vm, vmid, '112') live in entity_attributes
                cur.execute(
                    "SELECT ea.value_text, (ea.superseded_at IS NULL) AS live "
                    "FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'vm' AND ea.attribute = 'vmid'")
                rows = cur.fetchall()
                assert any(r[0] == "112" for r in rows if r[1]), rows
                with conn.cursor() as cur2:
                    cur2.execute(f'SET search_path TO "{schema}", public')
                    cur2.execute("SELECT entity_id FROM entity_aliases WHERE alias = 'vm' "
                                 "LIMIT 1")
                    _r = cur2.fetchone()
                assert _r, "the vm entity unresolved"
                vm_id = _r[0]

                # THE WALK — both measured question shapes reach '112'
                conn.close()
                conn = psycopg2.connect(_DSN)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}", public')
                for q in ("what is the vmid of the Home Assistant VM?",
                          "what's the home assistant vm id?"):
                    path = determine_path(q, conn, user_id=seat)
                    res_w = fetch_facts_from_anchor(vm_id, seat, path, query_text=q)
                    blob = str(res_w).lower()
                    assert "112" in blob, f"walk missed '112' for {q!r}: {blob[:300]}"

                # THE FULL /query TAIL (the BUG-RECALL-14 lesson: the lexical topic gate
                # sits BETWEEN the fetch and the answer and drops unexempted rows — the
                # served facts channel must carry the row THROUGH it, via the #13
                # exemption the scalar_rels admission feeds)
                for q in ("what is the vmid of the Home Assistant VM?",
                          "what's the home assistant vm id?"):
                    qr = parity_seat["client"].post(
                        "/query", json={"text": q, "user_id": seat})
                    assert qr.status_code == 200, qr.text[:300]
                    served = [(f.get("subject"), f.get("rel_type"), str(f.get("object")))
                              for f in (qr.json().get("facts") or [])]
                    assert any("112" in o for _s, _r, o in served), (q, served)
        finally:
            conn.close()


@pytest.mark.usefixtures("requires_postgres")
class TestItem4PluralNormalFormDoorLitmus:
    """THE ITEM-4 LITMUS through the REAL doors: the junk sibling (user, cats, two)
    never registers the plural 'cats' as vocabulary — the singular class form mints —
    so the class anchor stays available for "what are our cats' names?"."""

    def test_junk_sibling_never_registers_the_plural(self, parity_seat):
        import psycopg2
        from src.api.models import EdgeInput
        res, _e = _harvest_and_ingest(parity_seat, PET1)
        seat, schema = parity_seat["user_id"], parity_seat["schema"]
        # the EXPLICIT junk sibling, exactly the F1b vehicle (the smoke's extraction shape)
        r = parity_seat["client"].post("/ingest", json={
            "text": PET1, "user_id": seat,
            "edges": [EdgeInput(subject="user", rel_type="cats", object="two",
                                object_datatype="string",
                                fact_provenance="user_stated").model_dump()],
            "source": "mcp"})
        assert r.status_code == 200, r.text[:300]

        conn = psycopg2.connect(_DSN)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}", public')
                cur.execute("SELECT rel_type FROM rel_types WHERE rel_type IN "
                            "('cats','cat')")
                minted = {row[0] for row in cur.fetchall()}
            assert "cats" not in minted, (
                "the plural surface must never become vocabulary (roadmap item 4)")
            # THE CLASS ANCHOR STAYS AVAILABLE: the cats query does not scope onto a
            # junk rel — keyword 'cats' resolves no rel_type/alias row
            from src.api.main import determine_path
            path = determine_path("what are our cats' names?", conn, user_id=seat)
            scoped = ({str(r).strip().lower()
                       for r in (getattr(path, "relationship_rels", None) or [])}
                      | {str(r).strip().lower()
                         for r in (getattr(path, "scalar_rels", None) or [])})
            assert "cats" not in scoped, scoped
        finally:
            conn.close()
