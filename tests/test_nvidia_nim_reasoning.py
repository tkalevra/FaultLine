"""NVIDIA NIM must be able to turn reasoning OFF.

Nemotron 3 reasons by DEFAULT, and NVIDIA's off switch is a chat-template kwarg rather than a
top-level field: their reference says "To disable reasoning, add enable_thinking=False", and the
model cards show it on the wire as {"chat_template_kwargs": {"enable_thinking": false}}.

Before this entry existed, integrate.api.nvidia.com was an UNKNOWN host: no suppression field was
sent at all, so a tenant who bound NIM paid for chain-of-thought on every extraction call and was
shown "FaultLine cannot suppress reasoning on this endpoint" instead of a working switch.
"""
import pytest

from src.api.llm_client import resolve_reasoning_controls
from saas.reasoning_ticket import reasoning_forced

NIM = "https://integrate.api.nvidia.com/v1/"
DISABLE = {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("model", [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/nemotron-3-nano-30b-a3b",
])
def test_nemotron_reasoning_can_be_turned_off(model):
    c = resolve_reasoning_controls(NIM, model)
    assert c["known"] is True, f"{model}: NIM is not a recognised host"
    assert c["can_disable"] is True, f"{model}: reasoning reported as un-disablable"
    assert c["fields"] == DISABLE, f"{model}: wrong off switch: {c['fields']}"


@pytest.mark.parametrize("model", [
    "meta/llama-3.1-70b-instruct",
    "mistralai/mistral-7b-instruct-v0.3",
])
def test_a_non_reasoning_nim_model_is_sent_nothing(model):
    """THE 400 TRAP. NIM fronts many non-Nemotron models whose templates do not declare
    `enable_thinking`; sending an unknown kwarg is what takes a whole account down, exactly as
    recorded for OpenAI's reasoning_effort on gpt-4o."""
    c = resolve_reasoning_controls(NIM, model)
    assert c["fields"] == {}, f"{model} would be sent {c['fields']} and may 400"


def test_the_false_cannot_suppress_banner_is_gone():
    """An unknown host produced 'FaultLine cannot suppress reasoning on this endpoint'. With the
    switch recorded there is nothing to warn about."""
    assert reasoning_forced(NIM, "nvidia/nemotron-3-super-120b-a12b") is None


def test_a_trailing_slash_base_url_resolves_the_same():
    """The console stores exactly what the provider documents, trailing slash and all."""
    a = resolve_reasoning_controls("https://integrate.api.nvidia.com/v1/", "nvidia/nemotron-3-super-120b-a12b")
    b = resolve_reasoning_controls("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3-super-120b-a12b")
    assert a["fields"] == b["fields"] == DISABLE
    assert a["known"] is True and b["known"] is True


def test_the_entry_carries_its_source_and_date():
    """The table's doctrine: a published fact that goes stale needs a URL and a date a human can
    re-check. An entry without them cannot be audited when the provider changes."""
    from src.api.llm_client import _REASONING_CONTROLS
    e = _REASONING_CONTROLS["integrate.api.nvidia.com"]
    assert e["source"].startswith("https://"), e.get("source")
    assert e["recorded"], "no recorded date"
