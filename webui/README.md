# FaultLine — FOSS control plane webui

The user-facing **control plane** for a self-hosted (FOSS / AGPL-v3) FaultLine instance.
Vanilla JS + HTML + CSS, no framework, no build step, no `node_modules`. This is the FOSS
product's face: dashboard, seats + tokens, the LLM **Brain**, OpenWebUI wiring, help, and an
honest FOSS-vs-SaaS comparison.

> **License:** GNU AGPL v3. See [`../LICENSE`](../LICENSE). Every file in this tree carries an
> AGPL-3.0-only header notice. The word/term for the LLM panel on this line is **"Brain"**
> (never the SaaS-only synonym).

---

## Serve it

This UI is static and is served by the **FaultLine backend itself** at **`/`** on
port `8000` — the same origin as the control API, so the relative `/api/dashboard/*`
calls work with no CORS config. The backend serves `webui/` as a catch-all static
mount *after* all explicit routes (`/health`, `/api/dashboard/*`, `/openapi.json`),
so the API is never shadowed. No extra service/container is needed.

The static assets themselves are not auth-gated (the login page must render); only
the `/api/dashboard/*` handlers require the operator bearer. Every dashboard call
sends `Authorization: Bearer <FAULTLINE_ADMIN_TOKEN>`; 401 on missing/mismatch
(constant-time compare).

**Operator token (`FAULTLINE_ADMIN_TOKEN`):**
- **Unset** → the backend auto-mints a random one on first boot and prints it to the
  container logs (search for `FAULTLINE_ADMIN_TOKEN`). Paste that into the sign-in.
- **Set** (in `.env` / the compose `environment:` block) → that exact value is the login.

The seat cap (`FOSS_MAX_SEATS = 5`) is enforced **server-side** — it is a source
constant in `src/api/dashboard.py`, **not** an env var. The webui's 5-seat disable is
UX only; the `POST /api/dashboard/seats` handler counts active seats from the DB under
a transaction advisory lock and returns **409** at the cap (race-free). Bumping the cap
requires editing the source and redeploying.

To preview standalone during development, any static file server works:

```bash
# from the repo root
python -m http.server 8090 --directory webui
# then open http://localhost:8090/  (the API calls will be "pending" until a backend is wired)
```

---

## File layout

| File          | Role                                                        |
|---------------|-------------------------------------------------------------|
| `index.html`  | Shell: login gate, status bar, top tab nav, 6 views, modals |
| `style.css`   | Design system (CSS-var themed, terminal aesthetic)          |
| `app.js`      | Auth gate, API wrapper, router, all 6 panels, tour          |
| `strings.js`  | English string map + `t(key)` resolver                      |
| `favicon.svg` | Inline-able mark                                            |

Classic `<script>` tags (no ES module loader), so it works under a strict CSP with no
`type="module"` requirement.

---

## API contract

All calls are operator-bearer-authenticated (`Authorization: Bearer <token>`), JSON in/out,
served same-origin. Endpoints not yet implemented on the backend are rendered as a
**"backend wiring pending"** state — the UI degrades gracefully and never breaks.

| Method | Path                                         | Body / Result                                                                 |
|--------|----------------------------------------------|-------------------------------------------------------------------------------|
| GET    | `/api/dashboard/health`                      | `{database, qdrant, llm, re_embedder, llm_config}` — polled every ~10s        |
| GET    | `/api/dashboard/config`                      | `{version, mcp_port, backend_port, …}` (non-secret instance summary)          |
| GET    | `/api/dashboard/seats`                       | `{seats:[{user_id,label,created_at,active}], limit:5}`                        |
| POST   | `/api/dashboard/seats`                       | `{label?}` → `{user_id, token, created_at}` (201; **409 at the 5-seat cap**)  |
| DELETE | `/api/dashboard/seats/{user_id}`             | `{revoked:true}`                                                              |
| GET    | `/api/dashboard/llm`                         | `{backend_type, base_url, model, api_key_set}`                                |
| PUT    | `/api/dashboard/llm`                         | `{backend_type, base_url, model?, api_key?}` → `{ok:true, restart_required:true}` |
| POST   | `/api/dashboard/llm/test`                    | `{ok:true, latency_ms}` or `{ok:false, error}`                                |
| GET    | `/api/dashboard/openwebui`                   | `{mcp_url, api_key_set, filter_script}`                                       |
| POST   | `/api/dashboard/openwebui/rotate-key`        | `{api_key}` (new key, shown once)                                             |

**Additive** (not called by this version of the webui, but backed for curl / future UI):

| Method | Path                                         | Result                                                                        |
|--------|----------------------------------------------|-------------------------------------------------------------------------------|
| GET    | `/api/dashboard/actions`                     | `{actions:[{ts, action, target_user_id?, detail?}]}` last 25 operator actions |

**Seat tokens are real credentials, not cosmetic.** When a client presents a minted seat
token as `Authorization: Bearer <seat-token>` to the MCP server (`:8002`), the MCP server
hashes it and resolves it to the seat's `user_id` from `dashboard_seats` — that UUID becomes
the authenticated principal, and `bind_tenant`'s per-token identity path activates
automatically (the token IS the identity; the `X-OpenWebUI-User-Id` header becomes a
cross-check, not the source of truth). So minting a seat and pasting the token into OpenWebUI
genuinely scopes that connection's memory to that seat.

**Backend types** (`GET/PUT /api/dashboard/llm`) — the exact list from `.env.example` /
`docker-compose.yml`:

```
openwebui | ollama | lm_studio | openai | anthropic | groq | localai | raw
```

One-time secrets (seat token, rotated MCP key) are returned once and shown in a reveal modal
with a copy button and a "store it now" warning.

---

## i18n convention (English on this branch)

Italian and Spanish live on separate branches (`foss-it`, `foss-es`) — they are **not** created
here. To make that translation pass a cheap string-file swap:

- Every user-visible English string lives in **`strings.js`** as `window.FL_STRINGS`, keyed by a
  stable dotted id (`"dash.health"`, `"seats.mint"`, …).
- HTML references strings via `data-i18n="key"`; `app.js` applies them through `applyI18n()`.
  For elements that mix translatable text with non-translatable child markup (e.g. a section
  label plus a `pill-note`), `applyI18n` updates only the leading text node.
- JS reads strings through `t("key")`.
- A translation branch swaps **only** `strings.js` — keys stay stable, HTML/JS are untouched.

---

## Leak-gate invariants (NON-NEGOTIABLE)

This is the FOSS line. The tree must stay leak-gate clean:

1. **No `saas/` directory** is created or referenced. No file path under `webui/` contains the
   substring `saas`.
2. **No importing of the private hosted package** — there is no `import` / `from` reference to
   it, no `SAAS`/FOSS build-mode feature flag, and no `flag…gent` symbol anywhere.
3. **No SaaS-only LLM-panel synonym.** The LLM panel is the **Brain**. The reserved 6-letter
   neuroscience term used by the hosted line must never appear in this tree (any case).
4. **No code lineage from the hosted tree.** This UI was written fresh as vanilla JS/HTML/CSS.
5. AGPL-v3 notice on every new file.

### Self-check

Run from the **repo root**. Each line must print nothing. (The pattern is written via shell
concatenation so that this README itself does not contain the banned tokens literally — the
command reproduces the exact check at runtime and stays self-consistent over the whole tree.)

```bash
CX=cor""tex; grep -rli "$CX" webui/
A=saas;  grep -rE "import $A|from $A|SAAS""_MODE|flag""gent" webui/
find webui -path '*saas*'
```

All three are empty on a clean tree.

---

## AGPL header notice

Each new file begins with a short header, e.g.:

```
SPDX-License-Identifier: AGPL-3.0-only
License: GNU AGPL v3 — see ./LICENSE in the repo root.
```

---

## Tabs at a glance

1. **Dashboard** — stack health (database/qdrant/llm/re-embedder/llm-config, ~10s poll), instance
   config snapshot, seat usage meter.
2. **Seats & Tokens** — list, mint (token shown once), revoke. Hard cap of 5; at the cap, minting
   is disabled with an honest pointer to the hosted offering. Each seat exposes its MCP URL and
   the `X-OpenWebUI-User-Id` value for wiring.
3. **LLM Brain** — backend type (8 options), base URL, model, write-only API key; save (with a
   "restart the backend" notice) and a connection probe.
4. **OpenWebUI** — MCP tool URL, the supported OpenAPI tool-server wiring steps, the legacy filter
   script, and MCP-key rotation (new key shown once).
5. **Help** — first-run guided tour + persistent help (how memory works, `/expand`, corrections &
   retractions, connecting a model) in the plain, privacy-first README voice.
6. **FOSS vs SaaS** — static, honest side-by-side matrix and a link to https://faultline.ca.
