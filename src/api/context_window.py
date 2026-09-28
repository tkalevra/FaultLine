"""CONTEXT-WINDOW GUARD — never knowingly send a request the brain cannot read.

THE BUG THIS EXISTS FOR (confirmed from a live error, 2026-08-01)
----------------------------------------------------------------
The owner's llama.cpp brain answered::

    send_error: task id = 13471, error: request (9575 tokens) exceeds the
    available context size (8192 tokens)

We sent 9,575 prompt tokens at a slot whose window was 8,192. The request was
**REJECTED — never inferred**. Nothing in this codebase checked, or even knew, the
window (``grep -rn "n_ctx" src/`` returned nothing before this module).

WHY IT WAS INVISIBLE — and why it is a capture bug, not a latency bug
---------------------------------------------------------------------
llama.cpp returns that rejection as an ordinary HTTP **400**
(``ERROR_TYPE_EXCEED_CONTEXT_SIZE`` → ``code = 400``, ``tools/server/server-common.cpp``).
Our stack sees a 400 exactly like any other transport failure:

    raise_for_status() → HTTPStatusError → attempt_failed → retried → raised
      → reframe catches → "reframe.llm_failed" → used_llm=False
      → SILENT fallback to segment_clauses → materially weaker capture

The benchmark scores that as a capture MISS, indistinguishable from a genuine engine
failure. Worse, the retry loop re-sends a request that can only ever fail again, and
after ``max_retries`` the CIRCUIT BREAKER records a failure — so three oversized turns
open the breaker and then degrade every *well-sized* turn behind them for 60s. One
mis-sized prompt poisons its neighbours. That is the shape of "12.5% answer flicker /
18% graph divergence on identical ingest".

WHAT THE PRIMARY SOURCE SAYS (llama.cpp @ master, quoted, not paraphrased)
-------------------------------------------------------------------------
1. **The per-slot window is the total divided by the slot count.** ``src/llama-context.cpp``::

       if (cparams.kv_unified) {
           cparams.n_ctx_seq = cparams.n_ctx;
       } else {
           cparams.n_ctx_seq = cparams.n_ctx / cparams.n_seq_max;
           cparams.n_ctx_seq = GGML_PAD(cparams.n_ctx_seq, 256);

   and ``tools/server/server-context.cpp:1294``: ``int n_ctx_slot = llama_n_ctx_seq(ctx_tgt);``
   → ``-c 32768 -np 4`` (kv_unified off) gives **8192 per slot**, exactly what the owner saw.
   **The number the operator typed after ``-c`` is a TRAP** — it is not the usable window.

2. **``/props`` already reports the PER-SLOT value, so we do NOT divide it ourselves.**
   ``tools/server/server-context.cpp:4568`` (``get_props``)::

       json default_generation_settings_for_props = json {
           { "params", tparams.to_json(true) },
           { "n_ctx",  meta->slot_n_ctx },
       };
       json props = {
           { "default_generation_settings", default_generation_settings_for_props },
           { "total_slots",                 params.n_parallel },

   ``meta->slot_n_ctx`` is the divided, padded, training-capped per-slot value. This is
   the one field worth trusting. (``LLM_CONTEXT_SLOT_DIVISION`` exists for a server that
   advertises a TOTAL instead; it is opt-in, never guessed.)

3. **The rejection is on PROMPT TOKENS ALONE**, ``tools/server/server-context.cpp:3188``::

       if (slot.task->n_tokens() >= slot.n_ctx) {
           send_error(slot, string_format("request (%d tokens) exceeds the available "
                      "context size (%d tokens), try increasing it", ...),
                      ERROR_TYPE_EXCEED_CONTEXT_SIZE);

   Note ``>=``, and note that ``n_predict`` is **not** in that comparison. So there are
   TWO distinct failure modes and this module tracks both:
     * **HARD** — ``prompt >= window``: the server refuses. Zero output.
     * **SOFT** — ``prompt + max_tokens > window``: the server accepts, then has less room
       to generate than we asked for. A truncated JSON array is unparseable, which lands
       in the SAME silent-degrade branch. Reserving the completion budget is therefore not
       pedantry — ignoring it rebuilds the identical bug one call later.

4. **The rejection body carries the server's own numbers**, ``tools/server/server-task.cpp:1529``::

       json server_task_result_error::to_json() {
           json res = format_error_response(err_msg, err_type);
           if (err_type == ERROR_TYPE_EXCEED_CONTEXT_SIZE) {
               res["n_prompt_tokens"] = n_prompt_tokens;
               res["n_ctx"]           = n_ctx;

   That is ground truth straight from the engine, so a rejection TEACHES us the real
   window (``learn_from_rejection``) and the next call is sized correctly. We never have
   to guess twice.

HONESTY ABOUT THE ESTIMATE (read this before trusting a number in a log line)
-----------------------------------------------------------------------------
Pre-flight we do NOT have the model's tokenizer, so the count is an ESTIMATE:

  * **Default estimator = characters ÷ ``LLM_CONTEXT_CHARS_PER_TOKEN`` (3.5).** Measured
    against the REAL Qwen tokenizer over 4,000 LongMemEval turns: mean 4.66 chars/token,
    p10 4.12, p05 3.94, **p01 3.48**, min 1.36. So 3.5 sits at roughly the 1st percentile
    of real English content — it over-counts typical prose by ~25% and only under-counts
    the densest ~1%.
  * **Plus a margin**, ``LLM_CONTEXT_SAFETY_MARGIN`` (default 1.08), and a per-message
    chat-template allowance (role tokens + delimiters, which the payload pays for and a
    naive ``len(text)/4`` forgets).
  * **Plus RATCHET-DOWN calibration.** Every successful response reports
    ``usage.prompt_tokens`` — free ground truth. We keep the DENSEST ratio ever observed
    on this endpoint and use ``min(configured, observed_densest)``. Calibration can only
    ever make the estimate MORE conservative, never less.
  * **Residual, stated plainly:** content far denser than 3.5 chars/token (CJK, base64,
    minified code) can still under-estimate. That case is caught one call later by
    ``learn_from_rejection``, loudly, and never silently.

FAIL-SAFE POSTURE
-----------------
  * Window UNKNOWN (no env, probe failed, no rejection seen yet) → the guard **observes
    and never blocks**. Unknown is not an excuse to refuse work.
  * Window KNOWN and the request does not fit → we do NOT send it. Callers that can split
    (the atomizer) split; everyone else gets a LOUD, COUNTED failure instead of a silent
    degrade. Fail toward not losing capture, never toward pretending.
  * ``LLM_CONTEXT_GUARD=false`` → every entry point here is a no-op and the stack is
    byte-for-byte what it was before this module existed. Pinned by
    ``tests/test_context_overrun_guard.py::test_flag_off_is_byte_for_byte_legacy``.
"""

from __future__ import annotations

import json as _json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import structlog  # type: ignore
    log = structlog.get_logger()
except Exception:  # pragma: no cover - logging fallback
    import logging
    log = logging.getLogger("context_window")


# ──────────────────────────────────────────────────────────────────────────────
# Flags / knobs — every one env-driven, no per-model hardcoding anywhere.
# ──────────────────────────────────────────────────────────────────────────────

def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def _num(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("context_window.invalid_env", env_var=name, value=raw[:40],
                    using_default=default)
        return default


def guard_enabled() -> bool:
    """Master switch — ``LLM_CONTEXT_GUARD`` (default true). OFF ⇒ every seam is a no-op."""
    return _flag("LLM_CONTEXT_GUARD", "true")


def _chars_per_token() -> float:
    v = _num("LLM_CONTEXT_CHARS_PER_TOKEN", 3.5) or 3.5
    return max(1.0, v)


def _safety_margin() -> float:
    v = _num("LLM_CONTEXT_SAFETY_MARGIN", 1.08) or 1.08
    return max(1.0, v)


def _per_message_overhead() -> int:
    """Chat-template tokens per message (role marker + delimiters) — the payload pays these."""
    return int(_num("LLM_CONTEXT_MESSAGE_OVERHEAD_TOKENS", 4) or 4)


def _envelope_overhead() -> int:
    """Fixed per-request template tokens (BOS, assistant-turn prefix, …)."""
    return int(_num("LLM_CONTEXT_ENVELOPE_OVERHEAD_TOKENS", 8) or 8)


def _min_useful_completion() -> int:
    """A completion budget below this is not worth sending — truncated JSON is unparseable,
    which lands in the same silent-degrade branch the whole guard exists to kill."""
    return int(_num("LLM_CONTEXT_MIN_COMPLETION_TOKENS", 64) or 64)


def _operation_completion_floor(operation: str) -> int:
    """The minimum clamped completion budget still worth sending for THIS operation.

    THE DEFECT THIS CAP FIXES (a floor larger than the caller's entire budget refuses
    work that would have succeeded). The global floor is 64; the tiny JSON-verdict
    operations declare budgets of 16 (INTENT_ADJUDICATION), 32 (INTENT_PRECLASSIFY)
    and 48 (OCCURRENCE_CLASSIFY). Reasoning headroom (dprompt-156) is added to the
    budget BEFORE the window check, so near the edge a 16-token call arrives here
    asking for 1040; with only 63 tokens of room the soft-overrun branch fires, 63
    fails the 64 floor, and the call is REFUSED with a CRIT — even though the
    operation needed 16 and had several times that. Unpadded, it fit. The floor is a
    stand-in for "what this operation needs", so it must NEVER exceed the operation's
    own declared budget: a budget the operation's author chose IS useful for it by
    definition, whatever its size.

    The declared budget is resolved from the single source of truth
    (`llm_calls.LLMMaxTokens`, env-overridable per operation) so a future small
    operation is covered the day its budget entry lands — no list of small operation
    names here to drift. The tenant-brain override only ever clamps budgets UP (never
    below the resolved budget), so the table value is a sound lower bound of the
    call's real unpadded need. Fail-safe: if the import or lookup fails, the global
    floor stands (exactly today's behaviour).
    """
    floor = _min_useful_completion()
    try:
        # Lazy import: llm_calls imports THIS module lazily (in _ctx_mod), so a
        # function-level import here cannot create a module-load cycle.
        from src.api.llm_calls import LLMMaxTokens
        budget = LLMMaxTokens.get(operation or "DEFAULT")
        if isinstance(budget, int) and budget > 0:
            floor = min(floor, budget)
    except Exception:  # noqa: BLE001 — the floor must never break the guard
        pass
    return max(1, floor)


def _probe_enabled() -> bool:
    return _flag("LLM_CONTEXT_PROBE", "true")


def _probe_timeout() -> float:
    return _num("LLM_CONTEXT_PROBE_TIMEOUT", 2.0) or 2.0


def _probe_ttl() -> float:
    return _num("LLM_CONTEXT_PROBE_TTL", 300.0) or 300.0


def _configured_window() -> Optional[int]:
    """``LLM_CONTEXT_WINDOW`` — the operator's explicit, authoritative override."""
    v = _num("LLM_CONTEXT_WINDOW", None)
    return int(v) if v and v > 0 else None


def _default_window() -> Optional[int]:
    """``LLM_CONTEXT_WINDOW_DEFAULT`` — the explicit, configurable fallback.

    DELIBERATELY UNSET by default. A guessed window is worse than no window: too small
    refuses work that would have succeeded, too large re-creates the bug. Unset ⇒ UNKNOWN
    ⇒ observe-only until a probe or a rejection tells us the truth.
    """
    v = _num("LLM_CONTEXT_WINDOW_DEFAULT", None)
    return int(v) if v and v > 0 else None


def _slot_division() -> Optional[int]:
    """``LLM_CONTEXT_SLOT_DIVISION`` — opt-in divisor for a server that advertises the TOTAL
    context rather than the per-slot window.

    ⚠️ NOT applied to llama.cpp ``/props``, which already reports ``meta->slot_n_ctx`` (the
    per-slot value — see the module docstring, source quoted). Dividing that AGAIN would
    quarter a correct window and refuse healthy calls. Set this only for a backend proven
    to advertise the undivided total.
    """
    raw = (os.getenv("LLM_CONTEXT_SLOT_DIVISION", "auto") or "auto").strip().lower()
    if raw in ("auto", "", "0", "1", "none", "off"):
        return None
    try:
        n = int(raw)
        return n if n > 1 else None
    except ValueError:
        log.warning("context_window.invalid_slot_division", value=raw[:20])
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Resolved-window state (per endpoint+model), and the ratchet-down calibration.
# ──────────────────────────────────────────────────────────────────────────────

SOURCE_ENV = "env"                 # operator said so — authoritative
SOURCE_LEARNED = "server_rejection"  # the engine told us its own n_ctx — authoritative
SOURCE_PROBE = "probe"             # endpoint self-report (/props, /v1/models)
SOURCE_DEFAULT = "configured_default"
SOURCE_UNKNOWN = "unknown"


@dataclass
class _WindowState:
    window: Optional[int] = None
    source: str = SOURCE_UNKNOWN
    total_slots: Optional[int] = None
    probed_at: float = 0.0
    probe_failed: bool = False
    # densest chars/token ever OBSERVED on this endpoint (ratchets down only)
    observed_ratio: Optional[float] = None


_LOCK = threading.Lock()
_STATE: dict[str, _WindowState] = {}


def _state_key() -> str:
    """Endpoint+model identity. A BYO tenant brain has its own window; so does each model."""
    try:
        from src.api.llm_client import get_backend_endpoint
        ep = get_backend_endpoint() or ""
    except Exception:  # noqa: BLE001 — never break a call over a config read
        ep = ""
    model = os.getenv("WGM_LLM_MODEL", "") or ""
    # Open core: the endpoint+model pair from the environment IS the brain identity.
    return f"{ep}|{model}"


def _get_state(key: Optional[str] = None) -> _WindowState:
    k = key or _state_key()
    with _LOCK:
        st = _STATE.get(k)
        if st is None:
            st = _WindowState()
            _STATE[k] = st
        return st


def reset_state() -> None:
    """Test hook — drop every cached window/calibration."""
    with _LOCK:
        _STATE.clear()


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint discovery — ask the endpoint what it can actually take.
# ──────────────────────────────────────────────────────────────────────────────

# Field names different OpenAI-compatible servers use to advertise a window. Metadata,
# not per-model hardcoding: a BYO brain may be any of these and we read whichever it offers.
_MODEL_WINDOW_FIELDS = (
    "max_model_len",          # vLLM
    "context_length",         # llama.cpp /v1/models, several gateways
    "max_context_length",     # LM Studio
    "loaded_context_length",  # LM Studio (the value actually loaded — preferred if present)
    "n_ctx",
    "n_ctx_train",
    "max_input_tokens",
    "context_window",
)
# Preference order when a payload advertises several: the LOADED/effective value wins over
# the model's theoretical training window (n_ctx_train is a ceiling, not a promise).
_MODEL_WINDOW_PRIORITY = (
    "loaded_context_length", "max_model_len", "n_ctx", "context_length",
    "max_context_length", "context_window", "max_input_tokens", "n_ctx_train",
)


def _base_url() -> Optional[str]:
    """The endpoint ROOT (chat path stripped). Read-only use of the endpoint resolver."""
    try:
        from src.api.llm_client import get_backend_endpoint
        ep = get_backend_endpoint()
    except Exception:  # noqa: BLE001
        return None
    if not ep:
        return None
    for suffix in ("/v1/chat/completions", "/openai/v1/chat/completions", "/v1/messages",
                   "/api/chat/completions", "/chat/completions", "/api/chat"):
        if ep.endswith(suffix):
            return ep[: -len(suffix)] or None
    return ep.rsplit("/", 1)[0] or None


def _window_from_props(body: Any) -> tuple[Optional[int], Optional[int]]:
    """(window, total_slots) from a llama.cpp ``/props`` body.

    ``default_generation_settings.n_ctx`` is ``meta->slot_n_ctx`` — ALREADY per-slot
    (source quoted in the module docstring), so it is used as-is.
    """
    if not isinstance(body, dict):
        return None, None
    slots = body.get("total_slots")
    slots = int(slots) if isinstance(slots, (int, float)) and slots > 0 else None
    dgs = body.get("default_generation_settings")
    if isinstance(dgs, dict):
        for k in ("n_ctx", "n_ctx_seq"):
            v = dgs.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v), slots
        params = dgs.get("params")
        if isinstance(params, dict):
            v = params.get("n_ctx")
            if isinstance(v, (int, float)) and v > 0:
                return int(v), slots
    v = body.get("n_ctx")
    if isinstance(v, (int, float)) and v > 0:
        return int(v), slots
    return None, slots


def _window_from_models(body: Any) -> Optional[int]:
    """Best window advertised anywhere in a ``/v1/models`` body (any nesting depth)."""
    found: dict[str, int] = {}

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _MODEL_WINDOW_FIELDS and isinstance(v, (int, float)) and v > 0:
                    found.setdefault(k, int(v))
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node[:32]:
                walk(item, depth + 1)

    walk(body)
    for k in _MODEL_WINDOW_PRIORITY:
        if k in found:
            return found[k]
    return None


def _apply_slot_division(window: Optional[int]) -> Optional[int]:
    div = _slot_division()
    if window and div:
        return max(1, window // div)
    return window


def _probe_paths(base: str) -> list[tuple[str, str]]:
    """Window-probe URLs for ``base``. The models probe goes through THE ONE JOIN.

    ``/v1/models`` was appended by hand here, which doubles the version on a base that already
    carries one (``…/api/paas/v4`` → ``…/v4/v1/models``) — the same defect that 404'd chat.
    ``/props`` is llama.cpp's own non-versioned introspection path and has no join entry, so it
    stays a direct append; it is not an OpenAI-family operation path."""
    from src.api.llm_client import resolve_endpoint
    return [("props", f"{base}/props"),
            ("models", resolve_endpoint(base, "/v1/models"))]


def _probe_sync(st: _WindowState) -> None:
    """One blocking discovery round, hard-bounded and at most once per TTL. Never raises."""
    base = _base_url()
    if not base:
        st.probe_failed = True
        st.probed_at = time.time()
        return
    try:
        import httpx
        headers = {}
        try:
            from src.api.llm_client import get_llm_headers
            headers = get_llm_headers() or {}
        except Exception:  # noqa: BLE001
            headers = {}
        # The operator's env brain keeps the plain client (open core: one operator,
        # one configured endpoint — nothing tenant-supplied to vet).
        with httpx.Client(timeout=_probe_timeout()) as client:
            for kind, url in _probe_paths(base):
                try:
                    r = client.get(url, headers=headers)
                    if r.status_code != 200:
                        continue
                    body = r.json()
                except Exception:  # noqa: BLE001 — a missing endpoint is normal, not an error
                    continue
                if kind == "props":
                    win, slots = _window_from_props(body)
                    if slots:
                        st.total_slots = slots
                else:
                    win = _window_from_models(body)
                if win:
                    st.window = _apply_slot_division(win)
                    st.source = SOURCE_PROBE
                    st.probe_failed = False
                    st.probed_at = time.time()
                    log.info("context_window.probed", url=url.rsplit("/", 1)[-1],
                             window=st.window, advertised=win, total_slots=st.total_slots,
                             note="llama.cpp /props advertises the PER-SLOT n_ctx; not divided again")
                    return
    except Exception as e:  # noqa: BLE001 — discovery must never break a call
        log.debug("context_window.probe_error", error=str(e)[:120])
    st.probe_failed = True
    st.probed_at = time.time()


def _shared_window() -> Optional[int]:
    """A learned window published by another process, or None.

    Open core: the coordination module (``redis_coord``) carries the outbound-rate and
    daily-budget namespaces only — there is no shared-window namespace — so the learned
    window is PER PROCESS. That is never worse than a missing cache: each process learns the
    same number from its own first overrun. Kept as a seam so a deployment can add a shared
    store without touching the resolver."""
    return None


def _publish_shared_window(window: int) -> None:
    """Share an AUTHORITATIVE learned window. Open core: no shared store (see above)."""
    return None


def resolve_window(allow_probe: bool = True) -> tuple[Optional[int], str]:
    """The usable per-request window for the ACTIVE brain, and where the number came from.

    Precedence — authoritative first, guess never:
      1. ``LLM_CONTEXT_WINDOW``            operator override
      2. LEARNED from a server rejection   the engine's own ``n_ctx``
      3. endpoint probe                    ``/props`` then ``/v1/models``
      4. ``LLM_CONTEXT_WINDOW_DEFAULT``    explicit configured fallback
      5. UNKNOWN                           → observe-only, never block
    """
    env = _configured_window()
    if env:
        return env, SOURCE_ENV
    st = _get_state()
    if st.window and st.source == SOURCE_LEARNED:
        return st.window, SOURCE_LEARNED
    # A window ANOTHER PROCESS already learned the hard way (from the server's own 400).
    # Without this, every worker must overrun once to learn the same number — N workers, N
    # wasted oversized requests, N capture misses, re-learned from scratch after each deploy.
    # Same authority as a local SOURCE_LEARNED: it originated from the endpoint itself.
    shared = _shared_window()
    if shared:
        with _LOCK:
            st.window = shared
            st.source = SOURCE_LEARNED
        return shared, SOURCE_LEARNED
    if st.window and st.source == SOURCE_PROBE:
        return st.window, SOURCE_PROBE
    if allow_probe and _probe_enabled():
        stale = (time.time() - st.probed_at) > _probe_ttl()
        if stale or (st.probed_at == 0.0 and not st.probe_failed):
            _probe_sync(st)
            if st.window:
                return st.window, st.source
    dflt = _default_window()
    if dflt:
        return dflt, SOURCE_DEFAULT
    return None, SOURCE_UNKNOWN


# ──────────────────────────────────────────────────────────────────────────────
# Estimation — honest, conservative, and calibrated by real usage.
# ──────────────────────────────────────────────────────────────────────────────

def _effective_ratio() -> float:
    """chars/token used for the estimate: the configured floor, ratcheted down by the
    DENSEST ratio ever actually observed on this endpoint. Only ever more conservative."""
    ratio = _chars_per_token()
    obs = _get_state().observed_ratio
    if obs and obs > 0:
        ratio = min(ratio, obs)
    return max(1.0, ratio)


def estimate_tokens(messages: list[dict]) -> int:
    """Conservative ESTIMATE of the prompt tokens ``messages`` will cost.

    Heuristic, deliberately — we do not have the served model's tokenizer at call time.
    chars ÷ ratio (3.5 by default ≈ the 1st percentile of measured real English) + the
    per-message chat-template overhead + a fixed envelope, all × the safety margin.
    """
    ratio = _effective_ratio()
    chars = 0
    n_msgs = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        n_msgs += 1
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            # multimodal/segmented content — count every text part we can see
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chars += len(part["text"])
                elif isinstance(part, str):
                    chars += len(part)
        elif content is not None:
            try:
                chars += len(_json.dumps(content))
            except Exception:  # noqa: BLE001
                chars += len(str(content))
        chars += len(str(m.get("role") or ""))
    raw = chars / ratio + n_msgs * _per_message_overhead() + _envelope_overhead()
    return int(math.ceil(raw * _safety_margin()))


def observe_usage(messages: list[dict], result: Any) -> None:
    """Free calibration: an OpenAI-compatible response reports ``usage.prompt_tokens``.

    Ratchet the observed chars/token DOWN toward the densest content really seen, so the
    estimator can only get more conservative. Never raises, never widens the estimate.
    """
    if not guard_enabled():
        return
    try:
        usage = (result or {}).get("usage") if isinstance(result, dict) else None
        pt = (usage or {}).get("prompt_tokens")
        if not isinstance(pt, (int, float)) or pt <= 0:
            return
        chars = 0
        for m in messages or []:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, str):
                chars += len(c)
        if chars < 200:      # too short to calibrate on — template overhead dominates
            return
        ratio = chars / float(pt)
        if ratio <= 0 or ratio > 20:
            return
        st = _get_state()
        with _LOCK:
            if st.observed_ratio is None or ratio < st.observed_ratio:
                prev = st.observed_ratio
                st.observed_ratio = max(1.0, ratio)
                log.info("context_window.calibrated", chars_per_token=round(st.observed_ratio, 3),
                         previous=(round(prev, 3) if prev else None), prompt_tokens=int(pt),
                         note="ratchet-down only; estimate can only become more conservative")
    except Exception:  # noqa: BLE001 — calibration must never break a call
        pass


# ──────────────────────────────────────────────────────────────────────────────
# The plan — what a call should do BEFORE it goes out.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ContextPlan:
    """Verdict for one prospective call. ``fits`` True ⇒ send it unchanged."""
    fits: bool = True
    window: Optional[int] = None
    source: str = SOURCE_UNKNOWN
    prompt_tokens_est: int = 0
    max_tokens: int = 0
    effective_max_tokens: int = 0
    hard_overrun: bool = False      # prompt alone >= window → the server WILL refuse
    soft_overrun: bool = False      # prompt fits, completion budget does not
    clamped: bool = False
    prompt_budget: Optional[int] = None   # tokens available to the PROMPT after reserving
    detail: dict = field(default_factory=dict)

    def as_log(self) -> dict:
        return {
            "window": self.window, "window_source": self.source,
            "prompt_tokens_est": self.prompt_tokens_est,
            "max_tokens": self.max_tokens,
            "effective_max_tokens": self.effective_max_tokens,
            "hard_overrun": self.hard_overrun, "soft_overrun": self.soft_overrun,
        }


def plan_call(messages: list[dict], max_tokens: int, operation: str = "DEFAULT") -> ContextPlan:
    """Decide whether this call can be sent as-is. Never raises.

    Window UNKNOWN → ``fits=True`` always (observe-only). Guard off → ``fits=True``.
    """
    if not guard_enabled():
        return ContextPlan(fits=True, max_tokens=max_tokens, effective_max_tokens=max_tokens)
    try:
        window, source = resolve_window()
        est = estimate_tokens(messages)
        plan = ContextPlan(window=window, source=source, prompt_tokens_est=est,
                           max_tokens=int(max_tokens or 0),
                           effective_max_tokens=int(max_tokens or 0))
        if not window:
            plan.fits = True
            return plan
        # HARD: llama.cpp refuses at `n_tokens() >= n_ctx` (source quoted above) — note >=.
        if est >= window:
            plan.fits = False
            plan.hard_overrun = True
            plan.prompt_budget = window - _min_useful_completion()
            return plan
        room = window - est
        if room < plan.max_tokens:
            # SOFT: the prompt fits but generation would be cut short. A truncated JSON
            # response is unparseable → the SAME silent degrade. Clamp if what remains is
            # still useful; otherwise treat it as a hard miss and let the caller split.
            # "Useful" is OPERATION-RELATIVE (see _operation_completion_floor): the floor
            # is capped by the operation's own declared budget, so a tiny operation near
            # the window edge — whose budget reasoning headroom has inflated inside this
            # very check — is clamped to the room it has (which exceeds everything it
            # unpadded ever asked for) instead of being refused by a floor larger than
            # its entire budget.
            plan.soft_overrun = True
            _floor = _operation_completion_floor(operation)
            plan.detail["completion_floor"] = _floor
            if room >= _floor:
                plan.effective_max_tokens = int(room)
                plan.clamped = True
                plan.fits = True
            else:
                plan.fits = False
                plan.prompt_budget = window - plan.max_tokens
            return plan
        plan.fits = True
        return plan
    except Exception as e:  # noqa: BLE001 — the guard must never break a call
        log.warning("context_window.plan_failed", operation=operation, error=str(e)[:160])
        return ContextPlan(fits=True, max_tokens=max_tokens, effective_max_tokens=max_tokens)


def prompt_budget_chars(reserve_completion_tokens: int) -> Optional[int]:
    """How many CHARACTERS of prompt a splitting caller may send per window.

    None ⇒ window unknown ⇒ the caller must not attempt to split (nothing to split TO).
    """
    if not guard_enabled():
        return None
    window, _ = resolve_window()
    if not window:
        return None
    budget_tokens = window - max(0, int(reserve_completion_tokens)) - _envelope_overhead()
    if budget_tokens <= 0:
        return None
    return max(1, int(budget_tokens * _effective_ratio() / _safety_margin()))


# ──────────────────────────────────────────────────────────────────────────────
# Learning from the server's own rejection — the authoritative correction.
# ──────────────────────────────────────────────────────────────────────────────

# llama.cpp: ERROR_TYPE_EXCEED_CONTEXT_SIZE → type "exceed_context_size_error", HTTP 400,
# message "request (%d tokens) exceeds the available context size (%d tokens), try
# increasing it" (or "input (%d tokens) is larger than the max context size (%d tokens)").
# Other OpenAI-compatible servers phrase it differently; match on shape, not on one vendor.
_OVERRUN_TYPE_TOKENS = (
    "exceed_context_size_error",
    "context_length_exceeded",
)
_OVERRUN_MSG_RE = re.compile(
    r"(exceeds?\s+the\s+available\s+context|larger\s+than\s+the\s+max\s+context"
    r"|maximum\s+context\s+length|context\s+length\s+exceeded|exceeds?\s+context\s+window"
    r"|too\s+many\s+tokens|reduce\s+the\s+length\s+of\s+the\s+messages)",
    re.IGNORECASE,
)
_OVERRUN_NUMBERS_RE = re.compile(r"\((\d+)\s+tokens?\).*?\((\d+)\s+tokens?\)", re.DOTALL)


def classify_rejection(status_code: Optional[int], body_text: Optional[str]) -> Optional[dict]:
    """Is this failure the server refusing an over-window request? Returns its own numbers.

    ``{"n_prompt_tokens": int|None, "n_ctx": int|None, "message": str}`` or None.
    Deterministic string/JSON inspection — no LLM, no cosine.
    """
    if not body_text:
        return None
    text = body_text if isinstance(body_text, str) else str(body_text)
    n_prompt: Optional[int] = None
    n_ctx: Optional[int] = None
    message = ""
    matched = False
    try:
        body = _json.loads(text)
    except Exception:  # noqa: BLE001 — many gateways return prose
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            if str(err.get("type") or "").lower() in _OVERRUN_TYPE_TOKENS:
                matched = True
            if str(err.get("code") or "").lower() in _OVERRUN_TYPE_TOKENS:
                matched = True
        elif isinstance(err, str):
            message = err
        # llama.cpp attaches the authoritative numbers to the error result
        # (server-task.cpp:1531-1533: res["n_prompt_tokens"], res["n_ctx"]).
        # ⚠️ VERIFIED AGAINST A LIVE RESPONSE, not against the source alone: the running
        # build NESTS them inside "error" —
        #   {"error":{"code":400,"message":"request (9433 tokens) exceeds the available
        #    context size (8192 tokens)…","type":"exceed_context_size_error",
        #    "n_prompt_tokens":9433,"n_ctx":8192}}
        # — whereas `format_error_response` reads as though they sit at the top level.
        # Both are searched; relying on either alone would have left us on the message
        # regex, which infers prompt/window from (larger, smaller) and would misread a
        # payload whose numbers ever ordered differently.
        scopes = [body]
        if isinstance(body.get("error"), dict):
            scopes.append(body["error"])
        for scope in scopes:
            for key, target in (("n_prompt_tokens", "p"), ("n_ctx", "c")):
                v = scope.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    matched = True
                    if target == "p" and n_prompt is None:
                        n_prompt = int(v)
                    elif target == "c" and n_ctx is None:
                        n_ctx = int(v)
    if not message:
        message = text[:400]
    if not matched and not _OVERRUN_MSG_RE.search(message):
        return None
    if n_prompt is None or n_ctx is None:
        m = _OVERRUN_NUMBERS_RE.search(message)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            n_prompt = n_prompt or max(a, b)
            n_ctx = n_ctx or min(a, b)
    # A 400 whose body says nothing about tokens is a different 400 — do not claim it.
    if status_code is not None and status_code not in (400, 413, 422, 500) and n_ctx is None:
        return None
    return {"n_prompt_tokens": n_prompt, "n_ctx": n_ctx, "message": message[:400]}


def learn_from_rejection(info: dict) -> Optional[int]:
    """Adopt the server's OWN ``n_ctx`` as the authoritative window. Returns it, or None.

    This is why the guard never has to guess twice: the engine that refused the request
    told us exactly how big its window is.
    """
    if not guard_enabled() or not info:
        return None
    n_ctx = info.get("n_ctx")
    if not isinstance(n_ctx, int) or n_ctx <= 0:
        return None
    st = _get_state()
    with _LOCK:
        previous = st.window
        st.window = n_ctx
        st.source = SOURCE_LEARNED
    if previous != n_ctx:
        log.warning("context_window.learned_from_rejection", window=n_ctx, previous=previous,
                    n_prompt_tokens=info.get("n_prompt_tokens"),
                    note="authoritative: the endpoint reported its own n_ctx when it refused us")
    # Pay the overrun ONCE per fleet, not once per process. No-op unless the shared window
    # cache is switched on; never raises.
    _publish_shared_window(n_ctx)
    # A rejection also proves our estimate was too generous. Ratchet the ratio using the
    # server's real prompt-token count when we can attribute it.
    return n_ctx


def note_estimate_shortfall(estimated: int, actual_prompt_tokens: Optional[int],
                            messages: Optional[list[dict]] = None) -> None:
    """Record that a real prompt cost MORE than we estimated, and tighten the ratio."""
    if not guard_enabled() or not actual_prompt_tokens or actual_prompt_tokens <= 0:
        return
    try:
        if messages:
            chars = sum(len(m.get("content") or "") for m in messages
                        if isinstance(m, dict) and isinstance(m.get("content"), str))
            if chars > 0:
                ratio = chars / float(actual_prompt_tokens)
                st = _get_state()
                with _LOCK:
                    if st.observed_ratio is None or ratio < st.observed_ratio:
                        st.observed_ratio = max(1.0, ratio)
        if actual_prompt_tokens > estimated:
            log.warning("context_window.estimate_under",
                        estimated=estimated, actual=int(actual_prompt_tokens),
                        under_by=int(actual_prompt_tokens) - estimated,
                        note="heuristic estimator under-counted; ratio ratcheted down")
    except Exception:  # noqa: BLE001
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Countability — a degraded call must never again be invisible to a benchmark.
# ──────────────────────────────────────────────────────────────────────────────

_COUNTERS: dict[str, int] = {}


def count(event: str, operation: str = "*") -> None:
    """Bump a process-local counter. Greppable in logs AND readable by a bench harness."""
    with _LOCK:
        _COUNTERS[event] = _COUNTERS.get(event, 0) + 1
        _COUNTERS[f"{event}:{operation}"] = _COUNTERS.get(f"{event}:{operation}", 0) + 1


def counters() -> dict:
    """Snapshot of the overrun counters (for a bench harness / health endpoint)."""
    with _LOCK:
        return dict(_COUNTERS)


def reset_counters() -> None:
    with _LOCK:
        _COUNTERS.clear()


# Event names — ONE place, so a bench/grep never chases a renamed string.
EV_PREFLIGHT_BLOCKED = "context_overrun_preflight_blocked"
EV_SERVER_REJECTED = "context_overrun_server_rejected"
EV_COMPLETION_CLAMPED = "context_completion_clamped"
EV_CALLER_SPLIT = "context_overrun_caller_split"
