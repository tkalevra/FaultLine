# FOSS-001 — quickstart LLM probe sends Python-urllib User-Agent; UA-filtering gateways (Cerebras) 403 and the wizard blames the API key

- **Branch matrix:** es 761cf914 ✅ reproduced · it 9f2724ac ✅ same code · main c0fe9322 ✅ same code (quickstart.py identical across the three; one-line `_DEFAULT_LANG` diff only)
- **Severity:** High (kills the wizard's headline feature on a documented backend class; misleading error)
- **Discovered:** 2026-10-17, fresh-install gauntlet, es first

## Symptom

A human installs per the README (`git clone https://github.com/tkalevra/FaultLine.git && ./setup.sh`), picks any language, chooses **"OpenAI-compatible"**, enters a hosted OpenAI-compatible endpoint (e.g. `https://gateway.example.com/v1`) and a **valid** key. The wizard answers:

    Connecting → https://api.cerebras.ai ...
      ✗ could not connect — HTTP 403 — auth failed (check your API key)

The key is fine. The only escape is "Continue anyway — I'll type the model id" which **silently defeats the connect→list→pick flow** the README advertises ("tests the connection and lists your available models to choose from … no blind typing").

## Root cause

The wizard's probe helper (`quickstart.py::_http_get` / `probe_models`) uses `urllib.request` with the **default User-Agent `Python-urllib/3.x`**. Cerebras' edge (Cloudflare-class bot filtering) rejects that exact UA with 403 while the same key+endpoint returns 200 to any normal UA. Measured from the same host, same key, same instant:

    curl UA            → 200
    python-urllib UA   → 403 Forbidden
    UA=curl/8.5.0 via urllib → 200

Note the RUNTIME client (httpx, used after install) sends a normal UA and works — only the WIZARD's stdlib probe trips the filter, so the product works but the on-ramp tells you your key is broken.

## Reproduce

1. Host with outbound internet; `./setup.sh` from a fresh clone of any branch.
2. Language → any. Backend → 4 (OpenAI-compatible). URL `https://gateway.example.com/v1`. Any valid Cerebras key.
3. Observe 403 "auth failed". Independently `curl -H "Authorization: Bearer <key>" https://api.cerebras.ai/v1/models` → 200.

## Fix direction (gauntlet work order)

1. In `quickstart.py`, send a real User-Agent on EVERY outbound call (e.g. `FaultLine-Quickstart/1.0 (+https://github.com/tkalevra/FaultLine)`) — one helper, no per-call literals.
2. Reword the 403 branch of the failure hint: mention that some gateways (Cerebras/Cloudflare) filter default Python user-agents, and after the UA fix, keep "auth failed" reserved for genuine 401/403-from-provider-body cases (surface the provider `response_body` when present).
3. Pin: a stdlib-only unit test asserting the helper sets the UA header (no network), plus a probe_models smoke test against a local stub server that 403s `Python-urllib` UAs and 200s the FaultLine UA.
4. **You are NOT your own critic.** When your work is green, STOP and request a clean critic agent; the critic runs semgrep + codeql on the changed file and re-derives the UA claim against the Cerebras endpoint docs online before passing it.

## Filed evidence

- dind install log (es): `✗ could not connect — HTTP 403` at the Cerebras probe; runtime `/health llm:ok` against the same key minutes later via httpx.
- UA matrix above measured 2026-10-17 from one consistent egress IP.
