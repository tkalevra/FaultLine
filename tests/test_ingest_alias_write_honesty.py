"""IDENTITY-HONESTY BUG 2 - alias-write honesty in the /ingest response + MCP zero-capture
predicate.

Measured on the owner's live demo (2026-08-21): the name turn's alias registration was NOT
counted in the /ingest response, so a write that LANDED (entity_registry.alias_registered)
answered committed/staged/scalar_committed == 0 - and the MCP lane's ingest_landed_nothing
guard then logged "NOTHING LANDED - the gate rejected every one" for a write that landed.
Both directions of the lie are pinned here:

  * no SUCCESS SHAPE for writes that didn't happen (a refused alias write counts 0),
  * no FAILURE SHAPE for writes that did (a registered alias un-blocks the predicate).

Also pins the response-shape contract: the alias counters are ADDITIVE
(status/committed/staged/scalar_committed readers untouched) and the idempotency cache now
stores name-only turns (aliases_registered > 0) instead of skipping them.
"""
import inspect

from src.mcp import server as mcp_server


def test_ingest_lane_counts_alias_writes():
    body = {"status": "valid", "committed": 0, "staged": 0, "scalar_committed": 0,
            "aliases_registered": 1, "aliases_preferred": 1, "alias_preference_flips": 1}
    assert mcp_server.ingest_landed_nothing(body) is False, (
        "a turn that registered an alias LANDED a write - reporting it as "
        "NOTHING LANDED is the alias-write honesty lie (bug 2)")


def test_zero_capture_still_detected_without_alias_fields():
    body = {"status": "valid", "committed": 0, "staged": 0, "scalar_committed": 0}
    assert mcp_server.ingest_landed_nothing(body) is True


def test_alias_only_zeros_do_not_flip_when_landed_via_pref():
    body = {"status": "valid", "committed": 0, "staged": 0, "scalar_committed": 0,
            "aliases_registered": 0, "aliases_preferred": 1}
    assert mcp_server.ingest_landed_nothing(body) is False


def test_ingest_response_carries_alias_counters():
    from src.api import main as api_main
    src = inspect.getsource(api_main)
    assert '"aliases_registered": aliases_registered' in src, (
        "the /ingest response must count alias registrations (bug 2)")
    assert '"aliases_preferred": aliases_preferred' in src
    assert '"alias_preference_flips": alias_preference_flips' in src


def test_idempotency_gate_counts_alias_writes():
    from src.api import main as api_main
    src = inspect.getsource(api_main)
    assert "or aliases_registered > 0" in src, (
        "the idempotency cache gate must include alias writes")


def test_register_alias_returns_a_write_result():
    from src.entity_registry import registry as reg_mod
    src = inspect.getsource(reg_mod)
    assert '"written": True, "refused": False' in src
    assert '"written": False, "refused": True' in src


def test_refused_alias_write_is_not_counted_as_landed():
    body = {"status": "valid", "committed": 0, "staged": 0, "scalar_committed": 0,
            "aliases_registered": 0, "aliases_preferred": 0, "alias_preference_flips": 0}
    assert mcp_server.ingest_landed_nothing(body) is True, (
        "no success shape for writes that did not happen (bug 2)")


def test_nothing_landed_message_names_the_gate():
    from src.mcp import server as mcp_server
    src = inspect.getsource(mcp_server)
    assert "aliases_registered" in src, (
        "the ingest lane's count fields must include aliases_registered or the "
        "NOTHING LANDED message mis-fires on alias-only turns")
