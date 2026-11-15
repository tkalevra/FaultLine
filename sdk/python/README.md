# FaultLine Python SDK

A thin, dependency-free client for the FaultLine **v1** HTTP API. FaultLine is a
per-tenant, write-validated knowledge-graph memory: you hand it raw messages, it does
all extraction/validation/structuring server-side, and you recall grounded, traceable
answers. This client is a thin wrapper over the REST calls — no memory logic lives here.

## Install

```bash
pip install -e sdk/python        # from the FaultLine repo
```

Zero runtime dependencies (standard-library `urllib` only).

## Quickstart (10 lines)

```python
from faultline import Client

fl = Client(
    api_key="flk_your_account_key",          # or a flu_ seat token; omit on an open self-host
    base_url="https://memory.example.com",
    user_id="1a2b3c4d-63cd-4f31-92ce-f2c9edb760ab",  # the seat UUID (tenant)
)

fl.remember("My sister Diane is a nurse in Ottawa.")
print(fl.recall("what does my sister do?").memory)   # -> "Your sister Diane is a nurse."
fl.correct("actually Diane lives in Toronto now")
fl.forget("me", rel_type="has_email")                # bounded: one named fact
```

## API

| Method | Does |
|---|---|
| `remember(text)` | Store facts from a message (verbatim; engine extracts). |
| `ingest_document(text, source_ref=…, title=…)` | Ingest long-form/document text. |
| `recall(query)` | Recall grounded memory as prose (`.memory`). |
| `correct(text)` | Correct or retract a fact in natural language. |
| `forget(subject, rel_type=…, old_value=…)` | Tombstone ONE named fact (never bulk). |
| `learn(text)` | Learn `X is a subclass of Y` lines, or `/expand <topic>`. |
| `health()` | Liveness + version (no auth). |

Every result object exposes `.data` — the exact payload an MCP client would receive.

## Errors

Typed exceptions map to the v1 error envelope:

```python
from faultline import QuotaExceededError, ReadOnlyError, AuthError

try:
    fl.remember("…")
except ReadOnlyError:
    ...   # seat is in agent "serve" mode — writes frozen, recall still works
except QuotaExceededError as e:
    ...   # e.detail carries {metric, limit, used}
except AuthError:
    ...   # bad/missing bearer
```

All inherit from `FaultLineError` (`.status`, `.code`, `.detail`).

## Tenant scoping

The tenant is always **server-derived** from your key/seat. `user_id` selects *which of
your account's seats* you act as; you can never address another tenant's memory.
