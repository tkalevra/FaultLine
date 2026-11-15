"""FETCH MODELS cost legend: classify_model_class must color models HONESTLY.

WHY THIS FILE EXISTS. A new user hooking a brain into FaultLine has no idea what a
"reasoning model" is in their plan, so the consoles' FETCH MODELS flow now color-codes the
returned ids (green/orange/red/grey) with a right-justified legend. The colors are only
trustworthy if they are DERIVED from the same per-host reasoning-controls table
(src/api/llm_client.resolve_reasoning_controls) that actually drives the payload — a legend
computed from anything else would promise a suppression the request path never delivers.

The classification contract pinned here:
  GREEN  - reasoning can be fully turned off, or the model never reasons at all
           (can_disable=True), or an off-switch field is recorded (reasoning_effort="none").
  ORANGE - reasoning cannot be turned OFF but the recorded control REDUCES it to an
           effort floor (reasoning_effort="low"): gemini-3-pro, groq/xai gpt-oss,
           z.ai coding-surface reasoning_default. Moderate cost, not free.
  RED    - a reasoning model that can be neither disabled nor reduced: glm-5.3/
           glm-5.3-flash (minimal-effort is NOT trusted as a reduction), qwq,
           deepseek-r1, glm-4.7, glm-4.5v, sonar-reasoning, claude-fable/mythos.
  GREY   - the honest third state: unrecognized host + a model that does not LOOK like a
           reasoning family. Calls work; the reasoning cost is UNVERIFIED. Unknown is not
           RED — that would warn every self-hosted vLLM/LM Studio user for no measured
           reason. A detected reasoning family on an unknown host still goes RED
           (reasoning is known; only the host is not).

classify_model_class is PURE and must NEVER raise per model — the list renders one id at a
time and a classification fault must degrade that id to grey, not break the fetch.
"""
import pytest

from src.api.llm_client import classify_model_class, resolve_reasoning_controls


@pytest.mark.parametrize("base_url,model,expected", [
    # GREEN — the z.ai CODING surface honors thinking:{"type":"disabled"} on glm-4.5-air
    # (live-keyed bench, n=5, reasoning_tokens 0/0/0/0/0 — recorded in the table).
    ("https://api.z.ai/api/coding/paas/v4", "glm-4.5-air", "green"),
    # RED — GLM-5.3 is forced-thinking on BOTH z.ai surfaces; reasoning_effort "minimal"
    # is not trusted as a reduction (z.ai maps low/medium to HIGH on this family).
    ("https://api.z.ai/api/coding/paas/v4", "glm-5.3", "red"),
    ("https://api.z.ai/api/paas/v4", "glm-5.3", "red"),
    # ORANGE — gemini-3 effort floor is reducible ("low", no off value on the 3 series).
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3-pro", "orange"),
    # GREEN — gpt-5.x records reasoning_effort "none": a real off switch (can_disable=True).
    ("https://api.openai.com/v1", "gpt-5.1-chat-latest", "green"),
    # RED — deepseek-r1 is thinking-only.
    ("https://api.deepseek.com", "deepseek-r1", "red"),
    # RED — qwq (Qwen thinking-only) on DashScope.
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwq-32b", "red"),
    # ORANGE — groq gpt-oss: only low/med/high, "none" 400s (live-verified) → reducible.
    ("https://api.groq.com/openai/v1", "gpt-oss-120b", "orange"),
    # ORANGE — xAI: reasoning cannot be disabled, effort floor "low" is the recorded control.
    ("https://api.x.ai/v1", "grok-4.5", "orange"),
    # GREY — unknown host + a model that matches no reasoning marker: honest third state.
    ("http://198.51.100.20:8080/v1", "totally-unknown-model", "grey"),
    # RED — unknown HOST but a DETECTED reasoning family: reasoning is known, control is not.
    ("http://198.51.100.20:8080/v1", "deepseek-r1", "red"),
    # GREEN — recognized host, non-reasoning model → nothing to disable.
    ("https://api.openai.com/v1", "gpt-4o-mini", "green"),
    # Degenerate inputs must not raise: classify to grey like any other unknown.
    (None, None, "grey"),
    ("", "", "grey"),
    ("not a url at all", "model-x", "grey"),
])
def test_classify_model_class(base_url, model, expected):
    assert classify_model_class(base_url, model) == expected


def test_classify_matches_the_controls_that_would_actually_be_sent():
    """The legend is only honest if it AGREES with the payload path: whenever the class
    claims green, resolve_reasoning_controls must be able to disable (or the model never
    reasons); orange must carry a low effort floor; red must have neither."""
    for base_url, model in [
        ("https://api.z.ai/api/coding/paas/v4", "glm-4.5-air"),
        ("https://api.openai.com/v1", "gpt-5.1-chat-latest"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3-pro"),
        ("https://api.z.ai/api/coding/paas/v4", "glm-5.3"),
        ("https://api.deepseek.com", "deepseek-r1"),
    ]:
        c = resolve_reasoning_controls(base_url, model)
        k = classify_model_class(base_url, model)
        if k == "green":
            assert c["can_disable"], f"{model}: green but can_disable=False"
        elif k == "orange":
            assert not c["can_disable"], f"{model}: orange but can_disable=True"
            assert c["fields"].get("reasoning_effort") == "low", \
                f"{model}: orange without a low effort floor: {c['fields']}"
        else:
            assert k == "red"
            assert not c["can_disable"], f"{model}: red but can_disable=True"
            assert c["fields"].get("reasoning_effort") != "low", \
                f"{model}: red but a low effort floor is recorded"


def test_classify_never_raises():
    """A hostile/garbage input per id must degrade to a class, never break the list render."""
    for base_url, model in [
        ("https://api.z.ai/api/coding/paas/v4", 12345),
        ("https://api.z.ai/api/coding/paas/v4", None),
        (object(), "glm-4.5-air"),
        ("https://api.z.ai/api/coding/paas/v4", "Çélinë-Çhat-99"),
    ]:
        assert classify_model_class(base_url, model) in ("green", "orange", "red", "grey")
