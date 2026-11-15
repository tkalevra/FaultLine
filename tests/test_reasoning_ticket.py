"""Reasoning-forced support ticket: detection + banner + email (SaaS layer).

Backs DEV/DESIGN-reasoning-forced-banner-ticket.md. DB-free: the mailer is mocked so these run in
CI without a live control plane.
"""
import pytest

from saas.reasoning_ticket import (
    reasoning_forced, build_reasoning_banner, send_reasoning_ticket_email, SUPPORT_EMAIL,
)


def test_reasoning_forced_detects_cannot_disable():
    got = reasoning_forced("https://api.x.ai", "grok-4.5")
    assert got is not None
    assert got["host"] == "api.x.ai"
    assert got["model"] == "grok-4.5"
    assert reasoning_forced("https://api.z.ai", "glm-5.3") is not None


def test_reasoning_forced_none_when_disableable_or_unset():
    assert reasoning_forced("https://api.deepseek.com", "deepseek-v4-flash") is None
    assert reasoning_forced("https://api.openai.com", "gpt-4o") is None
    assert reasoning_forced(None, None) is None
    assert reasoning_forced("", "grok-4.5") is None


def test_reasoning_forced_strips_credentials_from_provider():
    # A BYO base_url may legitimately embed a credential; the email must carry netloc ONLY.
    got = reasoning_forced("https://user:pass@api.x.ai/v1?api-key=sekret", "grok-4.5")
    assert got is not None
    assert got["provider"] == "api.x.ai"
    assert "user:pass" not in got["provider"]
    assert "sekret" not in str(got)


def test_build_reasoning_banner_shape():
    b = build_reasoning_banner("https://api.x.ai", "grok-4.5", "x", admin=True)
    assert b["kind"] == "reasoning_forced"
    assert b["severity"] == "warn"
    assert b["cta_target"] == "reasoning_ticket"
    assert "#reasoning" in b["help"]
    assert "grok-4.5" in b["body"]


def test_send_reasoning_ticket_email_subject_and_cc(monkeypatch):
    captured = {}

    def fake_send(to, subject, text_body, cc=None, reason=None, **kw):
        captured.update(to=to, subject=subject, body=text_body, cc=cc, reason=reason)
        return {"ok": True}

    import saas.smtp_sender as _smtp
    monkeypatch.setattr(_smtp, "send_email", fake_send)
    res = send_reasoning_ticket_email(
        SUPPORT_EMAIL, cc=["tenant@example.com"], provider="https://api.x.ai",
        model="grok-4.5", reason="cannot disable", logs_text="line1\nline2", source="tenant console")
    assert res["ok"] is True
    assert captured["subject"] == "LLM Brain - Reasoning problem"
    assert captured["to"] == SUPPORT_EMAIL
    assert captured["cc"] == ["tenant@example.com"]
    assert captured["reason"] == "reasoning_ticket"
    assert "https://api.x.ai" in captured["body"]
    assert "grok-4.5" in captured["body"]
    assert "line1" in captured["body"] and "line2" in captured["body"]
    assert chr(96) * 3 in captured["body"]  # codeblock fence
