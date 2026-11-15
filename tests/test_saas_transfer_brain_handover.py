"""Transfer brain handover — the disclosure a single sees before an org takes control.

Owner's ask: "It should be made clear on 'migration' from single to tenant that that
configuration/control is being taken away, and should be displayed to the user in a copy/paste
format for posterity (but not our responsibility to save it) just display and hijack control."

The SECURITY property these tests exist for: a stored API key is DESTROYED on transfer and is
NEVER rendered on the way out. "Show them what they're losing" must not become "hand them a
credential in a copyable block". If the first test here ever goes red, someone widened the
disclosure to include the key — that is a credential-disclosure regression, not a fixture drift.
"""
import json
from unittest.mock import patch

import pytest

from saas import transfers, seat_brain


_SECRET = "sk-SUPERSECRET-abc123-DO-NOT-LEAK"


def _seat_cfg(**over):
    cfg = {
        "configured": True,
        "backend_type": "openai",
        "base_url": "https://api.example.com",
        "model": "gpt-4o",
        # Both shapes a real config can carry — plaintext (if a caller ever hands one over) and
        # the Fernet ciphertext actually stored. NEITHER may reach the output.
        "api_key": _SECRET,
        "api_key_ciphertext": "gAAAAAB" + _SECRET,
        "api_key_set": True,
    }
    cfg.update(over)
    return cfg


def _disclose(scope=None, cfg=None, org="Acme Corp"):
    scope = scope or seat_brain.SCOPE_SEAT
    with patch.object(seat_brain, "brain_scope", return_value=scope), \
         patch("saas.llm_config.get_seat_config_redacted", return_value=cfg):
        return transfers.endpoint_handover_disclosure(
            {"user_id": "u-1", "account_id": "a-1"}, org_name=org)


@pytest.mark.parametrize("field", ["api_key", "api_key_ciphertext"])
def test_the_stored_key_is_never_disclosed(field):
    """THE security test. Neither the plaintext key nor its ciphertext may appear ANYWHERE in
    the payload — not in the structured endpoint dict, not in the copy/paste block."""
    out = _disclose(cfg=_seat_cfg())
    blob = json.dumps(out)
    assert _SECRET not in blob, f"{field}: the stored key LEAKED into the transfer disclosure"
    assert "gAAAAAB" not in blob, "the Fernet ciphertext leaked into the transfer disclosure"
    assert out["api_key_shown"] is False
    assert "api_key" not in (out["endpoint"] or {})


def test_the_disclosure_says_control_is_being_taken_away():
    """The owner asked for the LOSS to be explicit, not a footnote. Assert on meaning, not
    wording: the block must name the org, say the config goes away, and say the key is destroyed."""
    out = _disclose(cfg=_seat_cfg())
    copy = out["copy_text"].lower()
    assert out["control_taken"] is True
    assert "acme corp" in copy                      # names WHO takes over
    assert "deleted" in copy or "destroyed" in copy  # says the config does not survive
    assert "not shown" in copy                       # and is honest that the key is withheld


def test_copy_block_carries_the_non_secret_config_verbatim():
    """'Copy/paste format for posterity' — the parts they can actually re-enter elsewhere."""
    copy = _disclose(cfg=_seat_cfg())["copy_text"]
    for value in ("openai", "https://api.example.com", "gpt-4o"):
        assert value in copy, f"{value} missing — the copy block is not usable for re-entry"


def test_we_state_plainly_that_we_do_not_keep_it():
    """Owner: "but not our responsibility to save it". The text must not imply we retain a copy."""
    copy = _disclose(cfg=_seat_cfg())["copy_text"].lower()
    assert "we do not store it for you" in copy


def test_an_org_governed_seat_discloses_nothing():
    """A seat already on the ORG's brain has nothing of its own to hand over — and the org's
    endpoint is not this person's to be shown. Silence, not somebody else's config."""
    out = _disclose(scope=seat_brain.SCOPE_ACCOUNT, cfg=_seat_cfg())
    assert out["configured"] is False
    assert out["endpoint"] is None
    assert "api.example.com" not in out["copy_text"]


def test_no_endpoint_configured_still_explains_the_governance_change():
    """Nothing to lose config-wise, but control STILL changes hands — say so."""
    out = _disclose(cfg={"configured": False})
    assert out["control_taken"] is True
    assert out["configured"] is False
    assert "acme corp" in out["copy_text"].lower()


def test_a_control_plane_failure_degrades_loudly_not_silently():
    """A read failure must be VISIBLE. Silently rendering "nothing configured" would tell someone
    they are losing nothing while their endpoint is quietly deleted."""
    with patch.object(seat_brain, "brain_scope", side_effect=RuntimeError("control plane down")):
        out = transfers.endpoint_handover_disclosure(
            {"user_id": "u-1", "account_id": "a-1"}, org_name="Acme Corp")
    assert out["read_failed"] is True
    assert out["control_taken"] is True      # the governance change is still true and still stated


def test_saas_off_discloses_nothing_at_all():
    """No SaaS, no accounts, no transfer — the whole feature must be absent."""
    with patch("saas.transfers.saas_mode_enabled", return_value=False):
        assert transfers.endpoint_handover_disclosure({"user_id": "u", "account_id": "a"}) is None
