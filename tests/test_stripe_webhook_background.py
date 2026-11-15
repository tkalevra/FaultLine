"""Driven behavioural pins — the Stripe webhook's background processing is routed through
``src.mcp.server._background`` and its task reference is HELD (gauntlet
stripe-webhook-wall-inheritance, round 1).

THE WOUND: ``saas/stripe_routes.py::stripe_webhook`` scheduled money-event processing as a
BARE ``asyncio.create_task(_async_process(...))`` — two defects, the same class that killed
the drain worker (first-touch round 15) and the bench runner (round 17):
  (a) WALL INHERITANCE — the task copies the webhook REQUEST's context, so it inherited that
      request's turn wall (``src.mcp.server._turn_deadline``, ``MCP_TURN_WALL_S``). A webhook
      arriving near the end of a long request's window had its processing silently capped
      once the wall expired.
  (b) NO HELD REFERENCE — a bare create_task result can be garbage-collected mid-flight
      (the codebase's own warning in saas/agent_proxy.py).

THE FIX under pin: route through ``_background`` (clears the deadline INSIDE the task, keeps
the caller's wall, preserves the coroutine name, returns the task) and hold the reference in
``saas.stripe_routes._INFLIGHT_WEBHOOKS`` until the done-callback discards it — the
``agent_proxy._INFLIGHT_WRITES`` pattern.

These pins drive the REAL route coroutine with ``verify_and_record`` monkeypatched to a
verified event and ``process_event`` to a recorder, and pin the MECHANISM (the in-task
contextvars deadline is None), not just the outcome — the ``_async_process`` →
``asyncio.to_thread`` hop does not itself consult the wall today, so an outcome-only pin
would stay green through a revert.
"""

import asyncio

import pytest
from starlette.requests import Request

from saas import stripe_webhooks as _webhooks
from saas import stripe_routes as R
import src.mcp.server as S


# ── harness ──────────────────────────────────────────────────────────────────────

def _request(payload: bytes = b"{}", sig: str = "t=1,v1=stubbed") -> Request:
    """A minimal starlette Request: the route reads ONLY ``await request.body()`` and the
    ``Stripe-Signature`` header — verification itself is stubbed at verify_and_record."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/payments/stripe-webhook",
        "raw_path": b"/payments/stripe-webhook",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"stripe-signature", sig.encode())],
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 443),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


def _stub_verified(monkeypatch, event_id="evt_test_1",
                   event_type="checkout.session.completed"):
    meta = {"id": event_id, "type": event_type}
    monkeypatch.setattr(_webhooks, "verify_and_record",
                        lambda payload, sig: (True, dict(meta)))
    return meta


def _sleepy_to_thread(seen):
    """Replace ``asyncio.to_thread`` for the stripe_routes call path: run the recorder
    in-task (NOT in an executor thread — executor threads get a context COPY via
    ctx.run, but the point here is to read the task's OWN contextvars from inside it),
    after first capturing what the turn wall looks like FROM INSIDE the background task."""
    async def fake(fn, *args, **kwargs):
        seen["wall_remaining"] = S._turn_wall_remaining()
        seen["deadline"] = S._turn_deadline.get()
        await asyncio.sleep(0.2)
        return fn(*args, **kwargs)
    return fake


# ── PIN 1 — the processing task does NOT inherit the request's turn wall ─────────

def test_webhook_task_does_not_inherit_the_request_turn_wall(monkeypatch):
    """A 0.2 s wall is open in the (request) context when the route schedules the processing
    task. The route acks and returns; the wall then DIES. The recorder must still fire — and
    the mechanism must hold: read from INSIDE the running task, the turn deadline is CLEARED
    (None), because _background set it to None in the task's copied context. With the bare
    create_task restored, the task inherits the deadline and the in-task wall check sees a
    float/0.0 — never None — so this pin goes red on the revert."""
    _stub_verified(monkeypatch)
    fired = []
    monkeypatch.setattr(_webhooks, "process_event", lambda ev: fired.append(ev.get("id")))
    seen = {}
    monkeypatch.setattr(asyncio, "to_thread", _sleepy_to_thread(seen))

    async def drive():
        tok = S._turn_wall_open(0.2)          # the request's wall — SHORT
        try:
            resp = await R.stripe_webhook(_request())
        finally:
            S._turn_deadline.reset(tok)       # the request is over; its wall with it
        assert resp == {"received": True, "event_id": "evt_test_1",
                        "type": "checkout.session.completed"}, resp
        await asyncio.sleep(0.5)              # the wall (0.2 s) is now exhausted
        return fired, seen

    fired, seen = asyncio.run(drive())

    # OUTCOME: processing completed AFTER the wall died.
    assert fired == ["evt_test_1"], fired
    # MECHANISM: inside the task the deadline was cleared by _background — not inherited.
    assert seen.get("deadline") is None and seen.get("wall_remaining") is None, seen


# ── PIN 2 — the task reference is held until the task is done ────────────────────

def test_webhook_task_reference_is_held_until_done(monkeypatch):
    """After the route returns (ack-fast, processing still running) the registered task is in
    ``_INFLIGHT_WEBHOOKS``; once it completes and the loop turns, the done-callback has
    drained the set. With the bare create_task (no reference held) the set would be empty
    while the task is still in flight — and the task itself GC-able."""
    _stub_verified(monkeypatch)
    fired = []
    monkeypatch.setattr(_webhooks, "process_event", lambda ev: fired.append(ev.get("id")))
    seen = {}
    monkeypatch.setattr(asyncio, "to_thread", _sleepy_to_thread(seen))

    async def drive():
        R._INFLIGHT_WEBHOOKS.clear()          # defensive: drain any done leftover first
        await R.stripe_webhook(_request())
        # ack-fast: the route returned BEFORE the 0.2 s processing finished —
        # and the in-flight task is TRACKED.
        assert len(R._INFLIGHT_WEBHOOKS) == 1, R._INFLIGHT_WEBHOOKS
        task = next(iter(R._INFLIGHT_WEBHOOKS))
        assert not task.done()
        await task                            # the processing completes
        for _ in range(20):                   # done-callbacks run via call_soon
            await asyncio.sleep(0.01)
        return fired, set(R._INFLIGHT_WEBHOOKS)

    fired, remaining = asyncio.run(drive())
    assert fired == ["evt_test_1"], fired
    assert remaining == set(), remaining


# ── the no-running-loop fallback still works and tracks NOTHING ──────────────────

def test_no_loop_fallback_processes_inline_and_tracks_nothing(monkeypatch):
    """_background raising RuntimeError (no running loop) → the inline fallback processes the
    event — and a task that was never created must NOT be tracked in _INFLIGHT_WEBHOOKS."""
    _stub_verified(monkeypatch, event_id="evt_inline_1")
    fired = []
    monkeypatch.setattr(_webhooks, "process_event", lambda ev: fired.append(ev.get("id")))

    def _no_loop(coro):
        coro.close()   # never scheduled — close it so it cannot warn "never awaited"
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(S, "_background", _no_loop)
    R._INFLIGHT_WEBHOOKS.clear()

    async def drive():
        return await R.stripe_webhook(_request())

    resp = asyncio.run(drive())
    assert resp == {"received": True, "event_id": "evt_inline_1",
                    "type": "checkout.session.completed"}, resp
    assert fired == ["evt_inline_1"], fired            # processed INLINE, not dropped
    assert R._INFLIGHT_WEBHOOKS == set()               # nothing tracked for a task never created


# ── the census, driven from this lane too (PIN 3) ────────────────────────────────

def test_census_sees_the_stripe_site_as_routed():
    """PIN 3: the repo-wide task-creation census passes with the stripe allowlist entry
    REMOVED — the site is routed (this file exists because the census is the standing guard;
    invoking it here keeps the three pins together on this lane)."""
    import importlib
    import os as _os
    import sys as _sys
    _tests_dir = _os.path.dirname(_os.path.abspath(__file__))
    if _tests_dir not in _sys.path:      # tests/ is gitignored + not a package — not on sys.path
        _sys.path.insert(0, _tests_dir)
    census_mod = importlib.import_module("test_mcp_no_silent_route_downgrade")
    assert ("stripe_routes.py", "stripe_webhook", "_async_process") \
        not in census_mod._TASK_CREATION_ALLOWLIST
    bare = census_mod._bare_sites(
        census_mod.os.path.join(census_mod._REPO, "saas", "stripe_routes.py"))
    assert bare == [], bare
