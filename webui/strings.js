/*
  FaultLine FOSS control plane — webui string map
  SPDX-License-Identifier: AGPL-3.0-only
  License: GNU AGPL v3 — see ./LICENSE in the repo root.

  i18n convention (English on this branch):
  - Every user-visible English string lives here, keyed by a stable dotted id.
  - HTML references strings via data-i18n="key"; app.js applies them on load
    and after every render via applyI18n().
  - JS reads strings via t("key"). Dynamic/long bodies can also be looked up.
  - A translation pass (foss-it / foss-es) swaps ONLY this file. Keep keys stable.
*/
(function () {
  'use strict';

  var STRINGS = {
    "sb.scope.signedout": "not signed in",
    "sb.scope.signedin":  "operator",
    "sb.instance": "instance",
    "sb.mode.offline": "offline",
    "sb.mode.online":  "online",
    "sb.mode.degraded":"degraded",

    "head.title": "FaultLine Control Plane",
    "head.subtitle": "self-hosted · AGPL v3 · single-instance",
    "head.help": "? Help",
    "head.logout": "sign out",

    "login.title": "Operator sign-in",
    "login.intro": "Paste your admin token once. It is the operator bearer for this instance's control API, stored only in this browser and sent as Authorization: Bearer on every call.",
    "login.token": "Admin token",
    "login.connect": "▸ connect",
    "login.where": "Where do I get the token?",

    "tab.dashboard": "Dashboard",
    "tab.seats":  "Seats & Tokens",
    "tab.brain":  "LLM Brain",
    "tab.openwebui": "OpenWebUI",
    "tab.help":   "Help",
    "tab.compare":"FOSS vs SaaS",

    "dash.health": "Stack health",
    "dash.summary":"Instance summary",
    "dash.seats":  "Seats",
    "dash.brain":  "Brain",
    "dash.llmkey": "LLM API key",

    "seats.title": "Seats",
    "seats.help": "A seat is one end-user memory store, scoped to its own database schema. Minting a seat returns a connection token once — store it immediately. Use it as the Bearer token in a single-user client (Claude Desktop, opencode); OpenWebUI is wired separately with the MCP key. Each seat is identified by its user_id.",
    "seats.mint":  "＋ mint seat",
    "seats.list":  "Seat roster",
    "seats.col.label": "Label",
    "seats.col.created":"Created",
    "seats.col.status": "Status",
    "seats.col.actions":"Actions",
    "seats.empty": "no seats yet — mint one above",
    "seats.cap.reached": "The FOSS edition supports up to 5 seats. Larger seat counts are available on the hosted plans \u2014 see https://faultline.ca/pricing",
    "seats.revoke.confirm": "Revoke this seat? Its memory store is preserved, but its connection token stops working immediately.",

    "brain.title": "LLM Brain",
    "brain.what": "What is the Brain?",
    "brain.type": "Backend type",
    "brain.baseurl": "Base URL",
    "brain.model": "Model",
    "brain.apikey": "API key",
    "brain.save": "▸ save Brain config",
    "brain.test": "⚡ test connection",
    "brain.saved": "saved — restart the backend container for changes to take effect.",
    "brain.key.set": "set",
    "brain.key.unset": "not set",

    "owui.title": "OpenWebUI integration",
    "owui.mcp": "MCP tool URL",
    "owui.keyset": "MCP key set",
    "owui.rotate": "↻ rotate MCP key",
    "owui.steps.title": "Connect OpenWebUI (recommended path)",
    "owui.steps.intro": "The supported path is the OpenAPI tool server — it reliably forwards the per-user identity header FaultLine scopes on. Verified against OpenWebUI v0.10.x; paths apply from v0.6.31+.",
    "owui.filter": "Filter script (legacy inlet/outlet)",
    "owui.filter.placeholder": "-- backend wiring pending -- the live filter script is served by GET /api/dashboard/openwebui",
    "owui.copy": "⎘ copy",
    "owui.weak": "Weak model won't call the tools? Switch Chat Controls → Advanced Params → Function Calling → Legacy. The legacy inlet/outlet Filter lives in openwebui/faultline_function.py (Workspace → Functions) for automatic injection.",
    "owui.rotate.warn": "Rotating mints a new MCP key and invalidates the old one immediately. Any OpenWebUI tool config using the old key must be updated.",

    "help.tour": "Guided tour",
    "help.tour.intro": "A 5-stop walkthrough of this console. Runs once on first visit; replay anytime.",
    "help.tour.start": "▸ start the tour",
    "help.memory": "How memory works",
    "help.expand": "/expand — domain intelligence",
    "help.correct": "Corrections & retractions",
    "help.connect": "Connect a model",

    "cmp.title": "FOSS vs SaaS",
    "cmp.intro": "The same engine, two ways to run it. This is factual, not salesy — pick what fits.",
    "cmp.foss": "Self-hosted (FOSS)",
    "cmp.saas": "FaultLine SaaS",
    "cmp.row.license": "License",
    "cmp.row.seats": "Seats",
    "cmp.row.hosting": "Hosting",
    "cmp.row.brain": "LLM Brain",
    "cmp.row.data": "Data location",
    "cmp.row.updates": "Updates",
    "cmp.row.audit": "Audit log / event history",
    "cmp.row.multi": "Multi-tenant isolation",
    "cmp.row.serve": "Serve / Train (read-only agent) mode",
    "cmp.row.support": "Support",
    "cmp.row.auth": "Control-plane auth",
    "cmp.row.price": "Price",
    "cmp.cta": "Explore FaultLine SaaS → https://faultline.ca",

    "modal.cancel": "cancel",
    "modal.confirm": "confirm",
    "reveal.copy": "⎘ copy to clipboard",
    "reveal.close": "I've stored it — close",
    "reveal.token.title": "Seat token — shown once",
    "reveal.token.warn": "Store this now \u2014 it will not be shown again. This is a SEAT token: use it as the Bearer token in a single-user client such as Claude Desktop or opencode. OpenWebUI does not use seat tokens \u2014 wire it once with this instance\u2019s MCP key instead.",
    "reveal.mcpkey.title": "New MCP key — shown once",
    "reveal.mcpkey.warn": "Store this now. It will not be shown again. The old key stops working immediately.",

    "lic.badge": "AGPL v3",
    "lic.sub": "free software — with obligations",
    "lic.body": "FaultLine FOSS is licensed under the GNU Affero General Public License v3. You may run, study, modify and share it freely. Two conditions come with that: if you modify it and let others use it over a network, you must offer them the modified source; and anything you redistribute stays under the same licence.",
    "lic.scope": "This is the self-hosted edition. Your memory, your database, your model endpoint — nothing is sent to us. By signing in you are operating your own instance under these terms.",
    "lic.full": "read the full licence",
    "lic.gnu": "AGPL v3 at gnu.org",

    "tour.welcome": "Welcome",
    "tour.firstrun": "New here? Take a 30-second tour of the control plane.",
    "tour.dismiss": "later",
    "tour.go": "▸ start tour",
    "tour.back": "‹ back",
    "tour.skip": "skip",
    "tour.next": "next ›",
    "tour.done": "done",

    "tour.s1.title": "Status bar",
    "tour.s1.body": "Instance health, version, and the live status dot. Health polls every 10s.",
    "tour.s2.title": "Six tabs",
    "tour.s2.body": "Dashboard, Seats & Tokens, LLM Brain, OpenWebUI, Help, and the FOSS vs SaaS comparison.",
    "tour.s3.title": "Seats",
    "tour.s3.body": "Mint up to 5 seats — one per person. Each token is shown ONCE, so store it immediately. A seat token is for a single-user client (Claude Desktop, opencode). OpenWebUI is different: wire it once on the OpenWebUI tab with the instance MCP key, and each signed-in user is scoped automatically.",
    "tour.s4.title": "LLM Brain",
    "tour.s4.body": "Point FaultLine at a model you already run (Ollama, LM Studio, OpenWebUI, or a hosted API). Restart the backend after saving.",
    "tour.s5.title": "OpenWebUI",
    "tour.s5.body": "The supported wiring path and your MCP tool URL. Rotate the MCP key anytime.",

    "footer": "FAULTLINE · WRITE-VALIDATED MEMORY · FOSS · AGPL V3",

    "err.network": "network error — endpoint unreachable",
    "err.401": "401 — token rejected. Sign in again.",
    "err.pending": "backend wiring pending",
    "err.pending.detail": "This control endpoint is not live on the backend yet. The UI renders gracefully; the operator wiring it is separate from this console."
  };

  function t(key) {
    var v = STRINGS[key];
    return v == null ? key : v;
  }

  window.FL_STRINGS = STRINGS;
  window.t = t;
})();
