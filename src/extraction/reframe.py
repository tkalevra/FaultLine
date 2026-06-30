"""LLM reframe / atomize — de-ramble a multi-topic turn into clean one-fact-per-line atoms.

This is the "make it less like the ramblings of a 7-year-old that just learned English"
pre-segmentation normalizer of ``DEV/fix-reports/RC-reframe-atomize.md``. It runs at the very
TOP of the segmentation entry points (``/extract/rewrite`` before ``_split_sentences``, and the
``/harvest-spans`` recall path on already-isolated fact-bearing spans), BEFORE the trigger-span
segmenter, the LLM chunked extract pass, and the deterministic verb-lift. It hands those
downstream extractors clean, single-fact, de-noised statements instead of a dilute run-on.

HARD CONSTRAINTS (CLAUDE.md + the RC report):
- "USER IS TRUTH" — enforced MECHANICALLY by the deterministic token-subset guardrail
  (``_guardrail_ok``), NOT by the prompt. The LLM may RESTRUCTURE the user's own words only;
  it may never add, drop, or alter a fact / number / date / name / value. Any atom that
  introduces a content token absent from the user's actual words, or alters a numeric literal,
  is REJECTED and the verbatim source span substituted. Total failure → raw turn (today's path).
- The LLM here does NOT structure, name relations, or emit triples — it only SPLITS and
  DE-NOISES English prose. Structured extraction stays with GLiNER2 + the downstream LLM extract
  pass. GLiNER2 is NEVER touched here (Pitfall 11 is N/A — pure English splitting, no labels).
- PURE except for the centralized LLM stack: NO GLiNER2, NO DB read for the prompt
  (metadata-free, so cheap and Pitfall-11-immune), NO hand-rolled httpx, NO hardcoded
  timeout/max_tokens. Uses ``call_llm_with_retry_async`` op ``REFRAME`` only.
- Fail-safe (the trigger-span idiom): ANY failure {flag off, LLM error/timeout/circuit-open,
  malformed JSON, empty atom list, every atom rejected, fabricated source} ⇒ the raw turn flows
  exactly as it does today. The reframe can only ever HELP or NO-OP, never corrupt. Fail LOUD
  (WARNING with counts) so a drifting model surfaces in logs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

try:
    import structlog  # type: ignore
    log = structlog.get_logger()
except Exception:  # pragma: no cover - logging fallback
    import logging
    log = logging.getLogger("reframe")


# ──────────────────────────────────────────────────────────────────────────────
# Flags (read at call site too; mirrored here for unit-testability / clarity).
# ──────────────────────────────────────────────────────────────────────────────

def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() not in ("0", "false", "no")


def reframe_enabled() -> bool:
    """Master kill switch — REFRAME_ENABLED (default true)."""
    return _flag("REFRAME_ENABLED", "true")


def reframe_on_harvest_enabled() -> bool:
    """Recall-path sub-flag — REFRAME_ON_HARVEST (default true). Gates the /harvest-spans path."""
    return _flag("REFRAME_ON_HARVEST", "true")


# ──────────────────────────────────────────────────────────────────────────────
# NO COMPLEXITY GATE. The LLM atomizer runs on EVERY non-empty turn (still gated by
# the master REFRAME_ENABLED flag; empty/whitespace → early return, no LLM). The
# refined prompt itself decides split-vs-leave-vs-empty: an already-clean single fact
# comes back as exactly one atom, a pure question/greeting comes back as {"atoms": []},
# a dense turn is split. The old surface-count gate (and its coordinating-conjunction
# word-list) was a hardcode that skipped only the cheapest ~8% of calls while DROPPING
# buried dated facts the prompt now captures — removed in full (no-hardcode + capture).
# ──────────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Atom:
    """One clean atomic statement plus the verbatim source span it derives from.

    INVARIANT (enforced by the guardrail): ``source_span`` is a byte-substring of the original
    message, and ``text`` introduces no content token / numeric literal absent from the user's
    actual words. A guardrail-rejected atom keeps ``text == source_span`` (the user's exact words).

    ``rejected`` is a health flag (NOT load-bearing for downstream): True iff the LLM's clean
    rewrite was DISCARDED in favor of a verbatim fallback (subject-drop / content / numeric).
    It exists only so ``reframe_to_atomic`` can count genuine rejections instead of the old
    ``text == source_span`` heuristic, which false-counted OK atoms whose model ``source`` happened
    to equal its ``statement`` (the common single-fact case).
    """
    text: str
    source_span: str
    rejected: bool = False


@dataclass
class ReframeResult:
    atoms: list[Atom] = field(default_factory=list)
    used_llm: bool = False
    rejected_count: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Deterministic token-subset GUARDRAIL (the core — pure Python, no LLM, no DB).
# ──────────────────────────────────────────────────────────────────────────────

# Closed list of function words that may differ between an atom and its source without it
# counting as "introduced content" (determiner/copula/pronoun swaps: "that fence" → "the fence").
_FUNCTION_WORDS: frozenset[str] = frozenset({
    # determiners / articles
    "a", "an", "the", "this", "that", "these", "those", "some", "any", "no", "every", "each",
    # copulas / be
    "is", "are", "was", "were", "be", "been", "being", "am", "'s", "s",
    # auxiliaries / modals
    "do", "does", "did", "have", "has", "had", "will", "would", "shall", "should", "can",
    "could", "may", "might", "must",
    # prepositions / particles (function-level; load-bearing ones still ride content if they
    # carry a digit, which prepositions never do)
    "to", "of", "in", "on", "at", "for", "and", "or", "but", "as", "by", "with", "from",
    "into", "onto", "than", "then", "so", "if", "because", "while",
    # pronouns / possessives
    "i", "me", "my", "mine", "myself", "you", "your", "yours", "he", "him", "his", "she",
    "her", "hers", "it", "its", "they", "them", "their", "theirs", "we", "us", "our", "ours",
    "who", "whom", "whose", "which", "what", "where", "when", "there", "here",
    # common copula-adjacent filler that is not a fact token
    "just", "also", "too", "very", "really",
})

# Token regex that keeps dotted/dated/IP/email literals INTACT as one token:
#   10.0.0.21, foo@bar.com, 3/22, 2023-03-22  →  single tokens.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-:/@][A-Za-z0-9]+)*")


def _content_tokens(s: str) -> set[str]:
    """Lowercased content tokens of ``s`` (function words removed)."""
    if not s:
        return set()
    toks = _TOKEN_RE.findall(s.lower())
    return {t for t in toks if t not in _FUNCTION_WORDS}


def _literals(s: str) -> set[str]:
    """Tokens containing a digit (numbers, dates, IPs, "3/22", "2023-03-22", "40")."""
    return {t for t in _content_tokens(s) if any(c.isdigit() for c in t)}


# ── Tier 3: SUBJECT-PRESERVATION (grammatical, spaCy-only — no word lists) ───────────
# A subject-DROPPED atom ("attending the workshop ... last Saturday" from "I have been
# attending the workshop ... last Saturday") is a strict TOKEN SUBSET of the source — it
# introduces no new token and alters no literal, so Tiers 1+2 pass it. But with the
# grammatical subject "I" gone, the downstream deterministic spine has no first-person
# ``nsubj`` to ground ``(user, participated_in, workshop)`` on, and the whole fact silently
# falls to Class C (lost). Tier 3 rejects an atom that drops the source clause's subject so
# the verbatim source span (which the spine parses correctly) is substituted instead.
#
# Subject-agnostic: "My son Theodore broke his leg" → atom "broke his leg" loses the subject
# "son" and is rejected the same way. Grammar only (spaCy nsubj/nsubjpass + first-person
# morphology), NO hardcoded pronoun/noun list. Fail-safe: spaCy missing / parse error →
# treat as PASS (never crash, never a new false reject — strictly no worse than today).

def _subjects(doc) -> list:
    """The grammatical subject tokens (nsubj/nsubjpass) of ``doc``, in token order."""
    try:
        return [t for t in doc if t.dep_ in ("nsubj", "nsubjpass")]
    except Exception:  # noqa: BLE001 — dep probe must never crash extraction
        return []


def _has_first_person_subject(doc) -> bool:
    """True iff ``doc`` has a 1st-person personal-pronoun subject ("I"/"we") — grammar, no list."""
    try:
        from src.extraction.linguistics import _is_first_person_personal_pronoun
        return any(_is_first_person_personal_pronoun(t) for t in _subjects(doc))
    except Exception:  # noqa: BLE001 — fail-safe
        return False


def _subject_preserved(atom_text: str, source_span: str) -> bool:
    """Tier 3 gate. True ⇒ the atom retained the source clause's grammatical subject.

    Grammatical, spaCy-only (no word lists). Returns True (PASS — fail-safe) when the spaCy
    layer is unavailable or either parse fails: the layer can only ever ADD a rejection it is
    certain about, never invent one or crash. Two cumulative checks against the SOURCE:

      1. If the SOURCE has a grammatical subject (nsubj/nsubjpass) and the ATOM has NONE → FAIL.
         This is the bug case: "I have been attending the workshop ..." → "attending the
         workshop ..." (subject "I" dropped → bare participial fragment, no nsubj), and
         subject-agnostically "My son Theodore broke his leg" → "broke his leg" (subject "son"
         dropped). A subject-stripped atom cannot ground downstream, so reject it.
      2. SINGLE-clause first-person guard: if the SOURCE has EXACTLY ONE subject and it is a
         1st-person pronoun ("I"/"we"), the atom must STILL carry a first-person subject. This
         catches a same-clause reattribution/passivization ("I drove the car" → "the car was
         driven") that keeps SOME nsubj so (1) misses it. Restricted to single-subject sources
         so a legitimate per-clause split of a compound source ("... son broke his leg and I
         drove ...") whose atom covers the non-first-person clause is NOT falsely rejected.
    """
    try:
        from src.extraction.linguistics import _get_nlp
        nlp = _get_nlp()
        if nlp is None:                       # spaCy layer off / not baked → fail-safe PASS
            return True
        src_doc = nlp(source_span)
        atom_doc = nlp(atom_text)
    except Exception:  # noqa: BLE001 — any spaCy failure → fail-safe PASS (never lose a good atom)
        return True

    src_subjects = _subjects(src_doc)

    # (1) Source had a subject but the atom dropped it entirely.
    if src_subjects and not _subjects(atom_doc):
        return False

    # (2) Single first-person-subject source whose atom lost the first-person subject.
    if (
        len(src_subjects) == 1
        and _has_first_person_subject(src_doc)
        and not _has_first_person_subject(atom_doc)
    ):
        return False

    return True


def _containing_sentence(full_message: str, source_span: str) -> str:
    """The TRUE original sentence within ``full_message`` that CONTAINS ``source_span``.

    Two jobs, both needing the user's actual subject-intact sentence (never the LLM's mangled
    ``source`` fragment): (a) the Tier-3 subject-preservation REFERENCE — so a subject drop is
    caught even when the LLM stripped the subject from its ``source`` field too; (b) the verbatim
    FALLBACK text on a subject-drop rejection — so downstream parses the user's real sentence.
    Deterministic, spaCy-only (reuses ``linguistics.segment_clauses`` = ``doc.sents``); NO LLM,
    no word lists.

    Resolution order (fail-safe — never lose the fact):
      1. Single-sentence message → the whole ``full_message`` (the common case; subject intact).
      2. Multi-sentence → the EXACTLY-ONE sentence whose text contains ``source_span`` (so a
         multi-fact turn does not collapse to one giant atom).
      3. None / ambiguous (no containing sentence, or more than one) / spaCy unavailable →
         ``full_message`` (whole turn — subject intact, never a drop).
    """
    msg = (full_message or "").strip()
    span = (source_span or "").strip()
    if not msg:
        return span  # degenerate; nothing better to anchor to
    if not span:
        return msg
    try:
        from src.extraction.linguistics import segment_clauses
        sentences = segment_clauses(msg)
    except Exception:  # noqa: BLE001 — any failure → whole-message fallback (never lose the fact)
        sentences = []

    # spaCy unavailable / parse miss → single sentence assumed → whole message (subject intact).
    if not sentences or len(sentences) <= 1:
        return msg

    span_lower = span.lower()
    containing = [s for s in sentences if span_lower in s.lower()]
    if len(containing) == 1:
        return containing[0]
    # None or ambiguous (>1) → fail-safe to the whole turn (subject intact, never a drop).
    return msg


# Guardrail outcomes. ``OK`` = pass through the LLM rewrite. The rejection reasons are
# DISTINGUISHED because their verbatim fallback TARGET differs (RC §4.3 + the subject-drop fix):
#   - SUBJECT_DROP  → the LLM's REWRITE (``atom_text``) dropped the clause subject, so it cannot
#                     ground downstream; fall back to the TRUE ORIGINAL sentence (subject intact).
#   - CONTENT/NUMERIC → the ``source`` span is still the user's exact words for this fact; the LLM
#                     only mangled its rewrite, so the verbatim ``source`` span remains the anchor.
_GR_OK = "ok"
_GR_SUBJECT_DROP = "subject_drop"
_GR_CONTENT = "content"
_GR_NUMERIC = "numeric"


def _guardrail_check(atom_text: str, source_span: str, full_message: str) -> str:
    """Deterministic "USER IS TRUTH" gate. Returns a guardrail-outcome token (see ``_GR_*``).

    ``_GR_OK`` ⇒ the atom may be passed downstream as-is. Any other value is a rejection whose
    name identifies WHICH tier failed (so the caller can pick the correct verbatim fallback).

    Three-tier check (RC §4.3 + the subject-preservation fix). **PRECEDENCE: Tier 3 runs
    FIRST.** Subject-drop is the most destructive rejection — a subject-stripped fragment is a
    pure token-subset that Tiers 1+2 wave straight through (it invents no token and alters no
    literal), so if content/numeric were evaluated first they would short-circuit and the
    subject-drop branch would be UNREACHABLE for the common case where the LLM also tweaks a
    content token (e.g. "attending" → "attended"). The subject-drop fallback
    (``_containing_sentence`` → the true subject-intact sentence) is strictly SAFER than the
    content/numeric ``source``-span fallback (which keeps the subject-less fragment), so it must
    take precedence. Tiers 1+2 still cover the cases where the subject IS preserved.

    - **Tier 3 (subject preservation, grammatical) — CHECKED FIRST:** an atom that DROPS the
      source clause's grammatical subject (incl. a first-person "I"/"we") is rejected — it cannot
      ground downstream. spaCy grammar only, no word lists; fail-safe to PASS when spaCy is
      unavailable (so Tiers 1+2 then decide, exactly as before).
    - **Tier 1 (content tokens, message-scoped):** every non-numeric content token in the atom
      must appear SOMEWHERE in the original message (not necessarily in the source span). This
      allows pronoun resolution ("it" → "the fence" when "fence" appeared in an earlier clause)
      while still refusing any noun the user never typed.
    - **Tier 2 (numeric literals, span-scoped & strict):** every digit-bearing token in the atom
      must appear VERBATIM in the SOURCE SPAN — not merely somewhere in the message. This is the
      byte-intact guarantee for values LLMs love to "tidy" (rounding, reformatting 3/22 →
      March 22, dropping a subnet octet, relocating a number to the wrong fact).

    Any violation → a rejection token → caller substitutes the appropriate verbatim text.
    """
    # Tier 3 (FIRST — most destructive, safest fallback): the source clause's grammatical subject
    # must survive in the ATOM TEXT — the thing that actually flows downstream to the spine. Checked
    # against the TRUE ORIGINAL sentence (the one in ``full_message`` containing the LLM's
    # ``source``), NOT just the LLM's ``source`` span — because the model can drop the subject from
    # BOTH the statement AND its ``source`` field, and the true sentence always carries the user's
    # subject so the drop is caught regardless of how the model mangled it.
    #
    # IMPORTANT (enumeration fix): we test ONLY ``atom_text`` here, NOT the ``source`` span. A
    # correctly-split ENUMERATION item carries the shared subject in the ATOM TEXT but cites a bare
    # subjectless NP as its ``source`` — e.g. "We have a cat …, a dog named Rex, … a snake named
    # Sophia" splits to atom_text="We have a dog named Rex." (subject "We" intact) with
    # source="a dog named Rex" (no nsubj). The atom is GOOD; the source is just the verbatim
    # provenance fragment. The OLD code also tested the ``source`` span and rejected these correct
    # atoms, replacing them with the entire run-on sentence — the live "Rex/Sophia merge" bug.
    # The ``source``-subjectlessness only matters when the source is USED as the downstream text
    # (the CONTENT/NUMERIC verbatim fallback), so that case is handled in ``_apply_guardrail`` by
    # anchoring a subjectless source to its true containing sentence — without falsely rejecting the
    # OK atom here. Fail-safe PASS when spaCy is unavailable, leaving Tiers 1+2 to decide as before.
    subject_ref = _containing_sentence(full_message, source_span)
    if not _subject_preserved(atom_text, subject_ref):
        return _GR_SUBJECT_DROP

    atom_ct = _content_tokens(atom_text)
    source_ct = _content_tokens(source_span)
    msg_ct = _content_tokens(full_message)

    # Tier 1: a content token in NEITHER the source span NOR the whole message was INVENTED.
    introduced = atom_ct - source_ct
    introduced_outside_message = introduced - msg_ct
    if introduced_outside_message:
        return _GR_CONTENT

    # Tier 2: any numeric/dated/IP literal in the atom must be byte-verbatim in the source span.
    if not _literals(atom_text) <= _literals(source_span):
        return _GR_NUMERIC

    return _GR_OK


def _guardrail_ok(atom_text: str, source_span: str, full_message: str) -> bool:
    """Back-compat boolean wrapper over ``_guardrail_check`` (True ⇒ pass)."""
    return _guardrail_check(atom_text, source_span, full_message) == _GR_OK


# ──────────────────────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────────────────────

# NOTE on output shape: the centralized async stack does ``json.loads(content)`` and, on a
# parse-failure (think tags / stray prose), regex-extracts the FIRST ``{...}`` object. A bare
# top-level JSON ARRAY would not survive that regex fallback. So we ask the model for an OBJECT
# wrapping the list — ``{"atoms": [...]}`` — which parses on the happy path AND the regex
# fallback. (Deviation from RC §3's top-level-array shape; required for the shared parser.)
_SYSTEM_PROMPT = 'You are helping build a MEMORY GRAPH. The same real thing must come out in the SAME clean canonical form EVERY time, so it can be matched and de-duplicated downstream. Your job is to reformat one chat message into clean, single-fact statements.\n\nWHO each fact is ABOUT (critical — the graph is subject-agnostic):\nKeep each statement\'s TRUE SUBJECT exactly as the user wrote it. If the user wrote "I" / "my car" / "my son Theodore" / "the server", that EXACT subject stays. NEVER reattribute a fact to someone else, NEVER rewrite the subject to "the user", and NEVER force first person. A fact about the user\'s son stays about the son; a fact about a server stays about the server. Keep the user\'s OWN action verb ("drove", "received", "met", "attended") — do not swap it for a different verb.\n\nHARD RULES — obey every one:\n1. NEVER add a fact, name, number, date, place, or value that is not in the message. Copy every number/date/name/address EXACTLY, character for character.\n2. NEVER drop a real fact, and NEVER invent one. Every concrete statement in the message — including a fact tucked into a "by the way" aside — becomes its own line.\n3. ONE fact per line. Split compound sentences; keep each statement short. Never split one fact across two lines and never merge two facts into one line.\n4. STRIP the chatter and any request or question to the assistant, but KEEP every concrete stated fact in the SAME message. Remove ONLY the conversational parts — greetings, "by the way", "I just", "also", "anyway", and the request/question clause itself ("can you help me", "do you have any tips", "do you have any recommendations", "do you have any ideas"). A message can ASK a question AND state a fact in passing — keep the fact, drop the question. The request/question wording must never appear in a statement, but any concrete fact tucked beside or behind it (a "by the way" aside, an "I recall that…" background note, a stated event/date/name) STILL becomes its own statement. Return {"atoms": []} ONLY when the message states no concrete fact at all.\n5. DROP pure opinion/plan/feeling that names no concrete thing ("excited to capture great shots", "it was an amazing event"). Keep every clause that states something concrete about a subject.\n6. Resolve a pronoun ONLY to a noun literally present earlier in the SAME message; otherwise leave the pronoun as-is. When the pronoun could refer to EITHER a person\'s proper NAME or a common-noun role word for that SAME person (the message already gave both, e.g. "my mother\'s name is Diane" then "she ...", or "my sister is Sarah" then "she ..."), resolve the pronoun to the proper NAME, never the role word. This affects ONLY the pronoun\'s own clause; the earlier naming statement still becomes its own separate line unchanged.\n7. A RELATIVE CLAUSE IS A SEPARATE FACT. "X who is 10", "the server that runs Linux", "Paris, which is in France" each state a SECOND fact about that thing — split it onto its OWN line with the thing ITSELF as the subject ("Mia is 10.", "The server runs Linux.", "Paris is in France."). NEVER let a relative pronoun (who, that, which) be the subject of a line, and never leave "who is 10" attached to a list.\n8. A LIST OF DISTINCT THINGS SPLITS PER THING. When one clause introduces several distinct things — a comma/"and" list, or a count followed by the items ("three kids: Mia, Theo, and Leo", "two cars: a Tesla and a truck", "three servers: a web server named Apollo, a database named Vault, and a cache named Echo") — keep the bare NAMES listed together on the ONE membership line (carrying the shared subject and verb, e.g. "We have three kids: Mia, Theo, and Leo."), and put each thing\'s OWN attribute (its age, type, owner, location) on its own separate line. When a list item is itself "a <type> named <Name>", give it its own membership line in that exact form ("We run a database named Vault."). Never collapse two different things onto one line, and never invent a singular noun for an item the message only named inside the list.\n\nNAMING THINGS — ONE STABLE CANONICAL FORM (this is what lets the graph de-duplicate):\nA. Refer to a named thing by its EXACT proper/quoted TITLE — same words, same order, with its quotes — EVERY time, no matter how the surrounding sentence was phrased.\nB. For a named EVENT, the event phrase MUST be written in this EXACT shape, every single time:\n       the \'<Exact Title>\' event\n   - ALWAYS keep the quoted title AND the literal word "event" right after it. Never omit "event".\n   - NEVER put any other word between the title and "event", and NEVER fold extra words into the phrase: drop describers like "auto racking"/"annual", and drop any venue/place adjunct ("at the local racing track", "in nearby city") from the event phrase.\n   - If a date is given for the event, append it as  on <date>  AFTER the word "event" (a venue/place is dropped, only the date may follow). \n   This identical shape must come out for EVERY mention of the same event, whatever verb the user used and wherever the title sat in the original sentence.\n   Examples (identical event phrase every time; the user\'s own verb is kept):\n     "I participated in the \'Turbocharged Tuesdays\' auto racking event ... on June 14th"  ->  "I participated in the \'Turbocharged Tuesdays\' event on June 14th."\n     "I drove my car at the \'Turbocharged Tuesdays\' event ... on June 14th"  ->  "I drove my car at the \'Turbocharged Tuesdays\' event on June 14th."\n     "I met a mechanic ... at the \'Turbocharged Tuesdays\' event"  ->  "I met a mechanic at the \'Turbocharged Tuesdays\' event."\n     "just got back from the \'Rack Fest\' in nearby city on June 18th"  ->  "I got back from the \'Rack Fest\' event on June 18th."  (venue "in nearby city" dropped, "event" added)\n     "I attended the \'Rack Fest\' event last weekend, on June 18th"  ->  "I attended the \'Rack Fest\' event on June 18th."\nC. When the message gives a specific name next to a general word for the SAME thing ("the laptop, Dell XPS 13"), use the SPECIFIC name ("Dell XPS 13").\nD. EACH date belongs to EXACTLY ONE statement — the fact it actually describes. Never copy one date onto two statements, never strip a date off the thing it modifies.\n\nNO-FACT TURNS: if the message states no concrete fact (a pure question, greeting, or filler), return {"atoms": []}. Do not invent a statement just to have output.\n\nOUTPUT: strict JSON, nothing else — an OBJECT with one key "atoms":\n{"atoms": [ {"statement": "<clean single-fact sentence>", "source": "<the exact substring of the original message this came from>"} , ... ] }\nIf there is no statable fact, return {"atoms": []}.\n\nEXAMPLES:\nMessage: "My favorite color is red."\nOutput: {"atoms": [{"statement": "My favorite color is red.", "source": "My favorite color is red"}]}\nMessage: "By the way, my son Theodore broke his leg last Tuesday."\nOutput: {"atoms": [{"statement": "My son Theodore broke his leg last Tuesday.", "source": "my son Theodore broke his leg last Tuesday"}]}\nMessage: "The server in the rack went down on March 3rd."\nOutput: {"atoms": [{"statement": "The server in the rack went down on March 3rd.", "source": "The server in the rack went down on March 3rd"}]}\nMessage: "We have three kids: Mia who is 10, Theo who is 12, and Leo who is 19."\nOutput: {"atoms": [{"statement": "We have three kids: Mia, Theo, and Leo.", "source": "We have three kids: Mia who is 10, Theo who is 12, and Leo who is 19"}, {"statement": "Mia is 10.", "source": "Mia who is 10"}, {"statement": "Theo is 12.", "source": "Theo who is 12"}, {"statement": "Leo is 19.", "source": "Leo who is 19"}]}\nMessage: "We run two servers: a web server named Apollo and a cache named Echo."\nOutput: {"atoms": [{"statement": "We run a web server named Apollo.", "source": "a web server named Apollo"}, {"statement": "We run a cache named Echo.", "source": "a cache named Echo"}]}\nMessage: "What\'s the best way to clean and maintain my hiking boots?"\nOutput: {"atoms": []}\n'


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

async def reframe_to_atomic(
    text: str,
    user_id: str = "anonymous",
    messages: list[dict] | None = None,
) -> ReframeResult:
    """De-ramble ``text`` into clean atomic statements with verbatim source spans.

    Returns a ``ReframeResult``. The caller's contract:
      - ``result.atoms`` non-empty → feed ``a.text`` for each atom to segmentation/extraction.
      - ``result.atoms`` empty → fall back to the raw ``text`` (today's behavior).

    Fail-safe: the entire call is wrapped — flag off / LLM failure / malformed JSON / empty list
    all return ``ReframeResult(atoms=[], used_llm=...)`` so the caller falls back to raw ``text``.
    Guardrail-rejected atoms keep their verbatim source span (never dropped, never corrupted).
    """
    raw = (text or "").strip()
    if not raw:
        return ReframeResult(atoms=[], used_llm=False)
    if not reframe_enabled():
        return ReframeResult(atoms=[], used_llm=False)

    # NO complexity gate: run the LLM atomizer on every non-empty turn. The refined prompt
    # returns exactly one atom for an already-clean fact, {"atoms": []} for a pure question /
    # greeting / filler, and a clean split for a dense turn — so the model (not a surface-count
    # heuristic) decides. Empty/whitespace text already returned above; this is the only gate.
    try:
        from src.api.llm_calls import call_llm_with_retry_async
        from src.api.llm_calls import LLMTimeouts, LLMModels

        _messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": raw},
        ]
        result = await call_llm_with_retry_async(
            messages=_messages,
            model=LLMModels.get("REFRAME"),
            user_id=user_id or "anonymous",
            timeout=LLMTimeouts.get("REFRAME"),
            max_retries=1,                # latency-sensitive; fail open fast (INTENT_ADJUDICATION idiom)
            operation="REFRAME",          # resolves timeout (6.0s) + max_tokens (256)
        )
    except Exception as e:
        log.warning("reframe.llm_failed", user_id=(user_id or "?")[:8], error=str(e)[:160])
        return ReframeResult(atoms=[], used_llm=False)

    # Centralized stack returns {} on no-JSON/parse-failure, {"error": ...} on circuit-open.
    if not isinstance(result, dict) or result.get("error"):
        log.warning("reframe.no_usable_output",
                    user_id=(user_id or "?")[:8],
                    raw_len=len(raw),
                    result_keys=list(result.keys()) if isinstance(result, dict) else None)
        return ReframeResult(atoms=[], used_llm=True)

    raw_atoms = result.get("atoms")
    if not isinstance(raw_atoms, list) or not raw_atoms:
        # Empty/malformed atom list → fall back to raw turn (today's behavior). Not an error per
        # se (a pure-question turn legitimately yields []), but logged so a no-op model surfaces.
        log.info("reframe.empty",
                 user_id=(user_id or "?")[:8], raw_len=len(raw), atom_count=0)
        return ReframeResult(atoms=[], used_llm=True)

    atoms = _apply_guardrail(raw_atoms, raw)
    rejected = sum(1 for a in atoms if a.rejected)
    # "rejected" counts atoms whose LLM rewrite was DISCARDED in favor of the verbatim span (the
    # ``Atom.rejected`` flag set by the guardrail). It is a fail-loud health signal (model drift /
    # invention), not a hard failure. NOTE: the old ``text == source_span`` heuristic false-counted
    # OK atoms whose model ``source`` happened to equal its ``statement`` (the single-fact case).
    log.info("reframe.done",
             user_id=(user_id or "?")[:8],
             raw_len=len(raw),
             atom_count=len(atoms),
             rejected_count=rejected)
    if rejected:
        log.warning("reframe.guardrail_rejections",
                    user_id=(user_id or "?")[:8],
                    rejected_count=rejected,
                    atom_count=len(atoms))
    return ReframeResult(atoms=atoms, used_llm=True, rejected_count=rejected)


def _apply_guardrail(raw_atoms: list, full_message: str) -> list[Atom]:
    """Validate each raw LLM atom against the deterministic guardrail; return clean Atoms.

    For each ``{"statement", "source"}``:
      - Drop atoms with no usable statement.
      - REJECT (skip entirely) atoms whose ``source`` is NOT a byte-substring of the original
        message — the LLM fabricated its own provenance (RC §4.4). No verbatim text exists to
        fall back to, so the atom is discarded rather than invented.
      - For a valid source: run ``_guardrail_check``. Pass → keep the LLM's clean statement.
        Reject → substitute a VERBATIM fallback so downstream runs on the user's exact words:
          • **SUBJECT-DROP (Tier 3):** the LLM's ``source`` itself dropped the subject, so it is
            NOT a safe anchor (the spine can't ground "attending the workshop …" with no "I").
            Fall back to the TRUE ORIGINAL sentence containing the span (subject intact) — for a
            single-sentence turn that is the whole ``full_message``. THIS is the bug fix: before,
            we anchored to the mangled subject-less ``source`` and lost the capture.
          • **CONTENT / NUMERIC (Tiers 1–2):** the ``source`` span is still the user's exact words
            for this fact (only the rewrite was mangled), so the verbatim ``source`` span remains
            the correct anchor — unchanged behavior.
    """
    msg_lower = full_message.lower()
    out: list[Atom] = []
    for item in raw_atoms:
        if not isinstance(item, dict):
            continue
        statement = (item.get("statement") or "").strip()
        source = (item.get("source") or "").strip()
        if not statement and not source:
            continue
        # Fabricated provenance: a source that is not a substring of the user's message is a
        # guardrail failure (the LLM invented where it came from). No verbatim anchor → drop.
        if not source or source.lower() not in msg_lower:
            log.warning("reframe.fabricated_source",
                        source_preview=source[:80], statement_preview=statement[:80])
            continue
        outcome = _guardrail_check(statement, source, full_message) if statement else _GR_CONTENT
        if outcome == _GR_OK:
            out.append(Atom(text=statement, source_span=source))
        elif outcome == _GR_SUBJECT_DROP:
            # The LLM dropped the subject FROM ITS REWRITE — the atom text cannot ground downstream.
            # Anchor to the TRUE original sentence (subject intact) instead.
            true_sentence = _containing_sentence(full_message, source)
            log.warning("reframe.subject_drop_fallback",
                        dropped_source_preview=source[:80],
                        true_sentence_preview=true_sentence[:120])
            out.append(Atom(text=true_sentence, source_span=true_sentence, rejected=True))
        else:
            # CONTENT / NUMERIC rejection: fall back to the user's verbatim words for this fact.
            # The ``source`` span is normally the right anchor, BUT if it is itself a subjectless
            # fragment (a correctly-split enumeration item whose shared subject sits at the head of
            # the list, e.g. "a dog named Rex"), the spine cannot ground it — so in that one case
            # anchor to the TRUE containing sentence (subject intact). This preserves the old
            # "subjectless source → true sentence" safety for genuine content/numeric rejections,
            # WITHOUT it pre-empting and falsely rejecting OK enumeration atoms (that pre-emption was
            # the live merge bug). Grammatical + subject-agnostic; fail-safe (spaCy off → source).
            fallback = source
            if not _subject_preserved(source, _containing_sentence(full_message, source)):
                fallback = _containing_sentence(full_message, source)
            out.append(Atom(text=fallback, source_span=fallback, rejected=True))
    return out
