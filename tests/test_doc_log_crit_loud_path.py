"""ID5 — the document drain's loud path must itself work.

`src/re_embedder/embedder.py::_backend_state` is the three-state probe (DOWN / STALLED / READY)
the document drain uses to DEFER instead of burning chunks while the backend's event loop is
blocked. Its STALLED branch — "TCP connect succeeded, HTTP read timed out: alive but not serving"
— reported the condition through `_doc_log_crit(event, **fields)`, but passed the message
POSITIONALLY. `_doc_log_crit` takes exactly one positional argument, so the probe raised
`TypeError` on the very stall it was reporting (gauntlet ingest-replay-loop-deadlock,
2026-09-15): the branch existed, the CRIT line never did, and the caller saw a crash instead
of a classification.

Two pins:
1. Drive the STALLED branch with a real `httpx.ReadTimeout` from a faked `httpx.get` and
   assert a CRITICAL record carrying the event name and `log_level=CRIT` is emitted, the
   probe returns BACKEND_STALLED, and no TypeError escapes.
2. An AST census over embedder.py: every `_doc_log_crit(` call passes exactly ONE positional
   argument (the event). A new positional-message caller goes red here before it can break
   the loud path again.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging

import httpx
import pytest

embedder = importlib.import_module("src.re_embedder.embedder")


def test_stall_branch_emits_crit_line_and_classifies_stalled(monkeypatch, caplog):
    def fake_get(url, timeout=None, **kw):
        # connect succeeded, read timed out — the exact signature of a blocked event loop
        raise httpx.ReadTimeout("read timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(embedder.httpx, "get", fake_get)
    caplog.set_level(logging.CRITICAL, logger=embedder.log.name)
    with caplog.at_level(logging.CRITICAL, logger=embedder.log.name):
        state = embedder._backend_state("http://backend-under-test:8000")
    assert state == embedder.BACKEND_STALLED
    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL and "doc_drain.backend_stalled" in r.getMessage()]
    assert crit, [r.getMessage() for r in caplog.records]
    msg = crit[0].getMessage()
    assert msg.startswith("CRITICAL doc_drain.backend_stalled")
    assert "log_level=CRIT" in msg
    assert "probe_timeout_s=" in msg and "event loop likely blocked" in msg


def test_pool_timeout_is_classified_down_by_the_unreachable_gate_first(monkeypatch, caplog):
    """`_is_backend_unreachable` treats pool exhaustion as "never delivered" → DOWN, and it runs
    BEFORE the stalled arm — so the `httpx.PoolTimeout` half of the stalled branch's isinstance
    is unreachable (pre-existing, recorded in round_1/REPORT.md; not changed here). Pinned as
    the documented classification so a silent re-ordering is noticed."""
    def fake_get(url, timeout=None, **kw):
        raise httpx.PoolTimeout("pool", request=httpx.Request("GET", url))

    monkeypatch.setattr(embedder.httpx, "get", fake_get)
    with caplog.at_level(logging.CRITICAL, logger=embedder.log.name):
        assert embedder._backend_state("http://backend-under-test:8000") == embedder.BACKEND_DOWN
    assert not any("doc_drain.backend_stalled" in r.getMessage() for r in caplog.records)


def test_connect_refused_is_down_not_stalled_and_not_loud(monkeypatch, caplog):
    def fake_get(url, timeout=None, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(embedder.httpx, "get", fake_get)
    with caplog.at_level(logging.CRITICAL, logger=embedder.log.name):
        assert embedder._backend_state("http://backend-under-test:8000") == embedder.BACKEND_DOWN
    assert not any("doc_drain.backend_stalled" in r.getMessage() for r in caplog.records)


def test_doc_log_crit_signature_and_every_caller_passes_event_only():
    """`_doc_log_crit(event, **fields)` — one positional, keyword fields. Pin the signature AND
    every call site so a positional message can never again turn the loud path into a TypeError."""
    sig = inspect.signature(embedder._doc_log_crit)
    params = list(sig.parameters.values())
    assert [p.kind for p in params] == [inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_KEYWORD], sig
    tree = ast.parse(inspect.getsource(embedder))
    offenders = []
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_doc_log_crit":
            total += 1
            if len(node.args) != 1 or any(isinstance(a, ast.Starred) for a in node.args):
                offenders.append(node.lineno)
    assert total >= 10, total  # the census actually saw the callers
    assert offenders == [], f"_doc_log_crit called with a positional message at lines {offenders}"
    # and the fixed call really is a keyword call (the one the stall branch makes)
    with pytest.raises(TypeError):
        embedder._doc_log_crit("x", "positional message is a TypeError by design")
