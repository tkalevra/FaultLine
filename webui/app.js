/*
  FaultLine FOSS control plane — webui application
  SPDX-License-Identifier: AGPL-3.0-only
  License: GNU AGPL v3 — see ./LICENSE in the repo root.

  Vanilla JS, no framework, no build step, no module loader. The control-plane
  face for a self-hosted FaultLine instance. Operator bearer-auth; same-origin API.
*/
'use strict';

/* ── LLM backend types (source of truth: .env.example / docker-compose.yml) ── */
var BACKEND_TYPES = [
  'openwebui', 'ollama', 'lm_studio', 'openai',
  'anthropic', 'groq', 'localai', 'raw'
];

var SEAT_LIMIT = 5;

/* ── runtime state ─────────────────────────────────────────────────────────── */
var S = {
  token: '',
  view: 'dashboard',
  mcpBase: '',            /* best-known MCP tool URL for wiring hints       */
  healthTimer: null,
  pending: {}             /* path → true when an endpoint is detected unwired */
};

/* ── tiny helpers ──────────────────────────────────────────────────────────── */
function $(id) { return document.getElementById(id); }
function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}
/* NO local `t` here. A global `function t` (or `var t`) IS window.t, so ANY shim in this
   file clobbers the one strings.js exports and ends up calling itself — RangeError on
   load, before a single listener is wired. Function declarations HOIST, so capturing
   `var _t = window.t` first does not help: the declaration wins. strings.js loads first
   and its t() already falls back to the key when a string is missing. Just use it. */

function applyI18n(root) {
  qsa('[data-i18n]', root || document).forEach(function (n) {
    var val = t(n.getAttribute('data-i18n'));
    if (n.children.length === 0) { n.textContent = val; return; }
    var first = n.firstChild;
    if (first && first.nodeType === 3) first.nodeValue = val + ' ';
    else n.insertBefore(document.createTextNode(val + ' '), n.firstChild);
  });
}

function setMsg(id, kind, text) {
  var elc = $(id);
  if (!elc) return;
  elc.className = 'msg ' + (kind || '');
  elc.textContent = text == null ? '' : String(text);
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(function () { return true; });
  }
  return new Promise(function (resolve) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      resolve(ok);
    } catch (e) { resolve(false); }
  });
}

/* ── theme + accent (persisted, matches FaultLine terminal aesthetic) ───────── */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  try { localStorage.setItem('fl_foss_theme', theme); } catch (e) {}
  $('theme-ico').textContent = theme === 'light' ? '☀' : '☾';
  $('theme-lbl').textContent = theme;
}
function initTheme() {
  var theme = 'dark';
  try { theme = localStorage.getItem('fl_foss_theme') || 'dark'; } catch (e) {}
  applyTheme(theme);
}
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'light' ? 'dark' : 'light');
}

var ACCENTS = ['', 'orange', 'red', 'blue', 'purple'];
function applyAccent(a) {
  if (a === '' || a == null) { document.documentElement.removeAttribute('data-accent'); try { localStorage.removeItem('fl_foss_accent'); } catch (e) {} }
  else { document.documentElement.setAttribute('data-accent', a); try { localStorage.setItem('fl_foss_accent', a); } catch (e) {} }
  qsa('#accent-picker .swatch').forEach(function (b) {
    b.classList.toggle('active', (b.dataset.accent || '') === (a || ''));
  });
}
function initAccent() {
  var a = '';
  try { a = localStorage.getItem('fl_foss_accent') || ''; } catch (e) {}
  applyAccent(a);
}

/* ── API wrapper ───────────────────────────────────────────────────────────── */
function ApiError(status, detail) { this.name = 'ApiError'; this.status = status; this.detail = detail || ('HTTP ' + status); this.message = this.detail; }
ApiError.prototype = Object.create(Error.prototype);

function api(method, path, body) {
  if (!S.token) return Promise.reject(new ApiError(401, 'no operator token'));
  var headers = {};
  if (body != null) headers['Content-Type'] = 'application/json';
  headers['Authorization'] = 'Bearer ' + S.token;
  return fetch(path, {
    method: method,
    headers: headers,
    body: body != null ? JSON.stringify(body) : undefined
  }).then(function (r) {
    if (r.status === 204) return null;
    if (r.status === 401) { forceLogout(); throw new ApiError(401, 'token rejected'); }
    return r.json().catch(function () { return null; }).then(function (data) {
      if (!r.ok) {
        var detail = (data && (data.detail || data.message || data.error)) || ('HTTP ' + r.status);
        var err = new ApiError(r.status, detail);
        err.data = data;
        throw err;
      }
      return data;
    });
  });
}

/* callApi: resilient wrapper that flags 404/network as "pending" (endpoint not wired) */
function callApi(method, path, body) {
  return api(method, path, body).then(function (data) {
    return { ok: true, data: data, pending: false };
  }, function (err) {
    if (err && (err.status === 404 || err.status === 0)) {
      S.pending[path] = true;
      return { ok: false, data: null, pending: true, err: err };
    }
    return { ok: false, data: null, pending: false, err: err };
  });
}

function friendly(err) {
  if (!err) return '';
  if (!(err instanceof ApiError)) return t('err.network');
  if (err.status === 401) return t('err.401');
  return err.status + ' — ' + (err.detail || 'request failed');
}

/* ── pending banner ────────────────────────────────────────────────────────── */
function showPending(viewKey, on) {
  var banner = $('pending-banner');
  if (!on) { if (banner.classList.contains('hidden')) return; banner.classList.add('hidden'); return; }
  banner.classList.remove('hidden');
  banner.innerHTML = '<strong>' + esc(t('err.pending')) + ':</strong> ' +
    '/api/dashboard/' + esc(viewKey) + ' — ' + esc(t('err.pending.detail'));
}

/* ── auth ──────────────────────────────────────────────────────────────────── */
function loadToken() {
  try { S.token = localStorage.getItem('fl_foss_token') || ''; } catch (e) { S.token = ''; }
}
function saveToken(tok) {
  S.token = tok || '';
  try { if (tok) localStorage.setItem('fl_foss_token', tok); else localStorage.removeItem('fl_foss_token'); } catch (e) {}
}
function forceLogout() {
  saveToken('');
  $('view-login').classList.remove('hidden');
  $('view-app').classList.add('hidden');
  $('btn-logout').classList.add('hidden');
  $('btn-help').classList.add('hidden');
  $('sb-scope').textContent = t('sb.scope.signedout');
  $('sb-mode').textContent = t('sb.mode.offline');
  dotState('warn');
  if (S.healthTimer) { clearInterval(S.healthTimer); S.healthTimer = null; }
}
function logout() {
  forceLogout();
  setMsg('login-msg', '', '');
  $('in-token').value = '';
}

/* Probe the token against the health endpoint; if any control endpoint answers
   (or even 401s), the token format is "live". Treat total absence of /health as
   pending and still enter the console (graceful). */
function connect() {
  var tok = ($('in-token').value || '').trim();
  if (!tok) { setMsg('login-msg', 'err', 'paste your admin token first.'); return; }
  saveToken(tok);
  $('login-msg').className = 'msg';
  $('login-msg').textContent = 'connecting…';
  fetch('/api/dashboard/health', { headers: { 'Authorization': 'Bearer ' + tok } })
    .then(function (r) {
      if (r.status === 401) { saveToken(''); setMsg('login-msg', 'err', t('err.401')); return; }
      enterApp();
    }, function () {
      /* network failure — still enter; console renders in pending state */
      enterApp();
    });
}
function enterApp() {
  $('view-login').classList.add('hidden');
  $('view-app').classList.remove('hidden');
  $('btn-logout').classList.remove('hidden');
  $('btn-help').classList.remove('hidden');
  $('sb-scope').textContent = t('sb.scope.signedin');
  applyI18n($('view-app'));
  startHealthPolling();
  showView('dashboard');
  maybeOfferTour();
}

/* ── status dot ────────────────────────────────────────────────────────────── */
function dotState(kind) {
  var dot = $('sb-dot');
  dot.className = 'status-dot' + (kind ? ' ' + kind : '');
}

/* ── router / tabs ─────────────────────────────────────────────────────────── */
function showView(name) {
  S.view = name;
  qsa('#tabs button').forEach(function (b) { b.classList.toggle('active', b.dataset.tab === name); });
  qsa('.view').forEach(function (v) { v.classList.toggle('hidden', v.dataset.view !== name); });
  showPending('', false);
  if (name === 'dashboard') { loadDashboard(); }
  else if (name === 'seats') { loadSeats(); }
  else if (name === 'brain') { loadLLM(); }
  else if (name === 'openwebui') { loadOpenWebUI(); }
  /* help + compare are static */
}

/* ── TAB 1: dashboard ──────────────────────────────────────────────────────── */
function startHealthPolling() {
  if (S.healthTimer) clearInterval(S.healthTimer);
  loadHealth();
  S.healthTimer = setInterval(loadHealth, 10000);
}

function healthPill(name, state) {
  var cls = 'status-pill ' + ({
    ok: 'ok', up: 'ok', healthy: 'ok', true: 'ok',
    down: 'down', false: 'down', unhealthy: 'down', error: 'down',
    warn: 'warn', degraded: 'warn'
  }[String(state).toLowerCase()] || 'unknown');
  var label = (state == null || state === '') ? '—' : String(state).toLowerCase();
  return '<div class="health-card"><div class="hc-label">' + esc(name) + '</div>' +
    '<div class="hc-state"><span class="' + cls + '">' + esc(label) + '</span></div></div>';
}

function loadHealth() {
  var active = (S.view === 'dashboard');
  return callApi('GET', '/api/dashboard/health').then(function (r) {
    var box = $('dash-health');
    if (!r.ok) {
      /* the status dot + mode reflect health always; the banner only on the dashboard */
      if (active) showPending('health', !!r.pending);
      if (!r.pending) setMsg('dash-health-msg', 'err', friendly(r.err)); else setMsg('dash-health-msg', '', '');
      box.innerHTML = '<div class="empty">' + esc(r.pending ? t('err.pending') : '—') + '</div>';
      dotState(r.pending ? 'warn' : 'down');
      $('sb-mode').textContent = r.pending ? t('sb.mode.degraded') : t('sb.mode.offline');
      return;
    }
    if (active) showPending('health', false);
    setMsg('dash-health-msg', '', '');
    var d = r.data || {};
    var anyDown = false, anyWarn = false;
    function mark(v) { var s = String(v).toLowerCase(); if (['down','false','unhealthy','error'].indexOf(s) >= 0) anyDown = true; else if (s === 'warn' || s === 'degraded') anyWarn = true; }
    ['database','qdrant','llm','re_embedder','llm_config'].forEach(function (k) { mark(d[k]); });
    box.innerHTML =
      healthPill('database', d.database) +
      healthPill('qdrant', d.qdrant) +
      healthPill('llm', d.llm) +
      healthPill('re-embedder', d.re_embedder) +
      healthPill('llm config', d.llm_config);
    dotState(anyDown ? 'down' : (anyWarn ? 'warn' : 'online'));
    $('sb-mode').textContent = anyDown ? 'degraded' : (anyWarn ? 'degraded' : t('sb.mode.online'));
  });
}

function loadDashboard() {
  callApi('GET', '/api/dashboard/config').then(function (r) {
    var p = $('dash-config-panel');
    if (r.pending) { p.innerHTML = '<div class="empty">' + esc(t('err.pending')) + ' — config</div>'; return; }
    if (!r.ok) { p.innerHTML = '<div class="empty">' + esc(friendly(r.err)) + '</div>'; return; }
    var d = r.data || {};
    if (d.version) $('sb-version').textContent = String(d.version);
    var rows = Object.keys(d).filter(function (k) {
      return ['secret','api_key','token','password'].indexOf(k.toLowerCase()) < 0;
    }).map(function (k) {
      return '<div class="kv"><span class="k">' + esc(k) + '</span><span class="v">' + esc(d[k]) + '</span></div>';
    });
    p.innerHTML = rows.join('') || '<div class="empty">no config snapshot</div>';
  });

  callApi('GET', '/api/dashboard/seats').then(function (r) {
    var used = '?', limit = SEAT_LIMIT;
    if (r.ok && r.data) {
      var seats = r.data.seats || [];
      used = seats.length; if (r.data.limit) limit = r.data.limit;
    }
    renderSeatUsage(used, limit);
  });

  callApi('GET', '/api/dashboard/llm').then(function (r) {
    var brain = '—', keyset = '—';
    if (r.ok && r.data) {
      brain = (r.data.backend_type || '?') + (r.data.model ? (' · ' + r.data.model) : '');
      keyset = r.data.api_key_set ? t('brain.key.set') : t('brain.key.unset');
    }
    $('dash-brain').textContent = brain;
    $('dash-llmkey').textContent = keyset;
  });
}

function renderSeatUsage(used, limit) {
  var n = (used === '?') ? 0 : Number(used) || 0;
  var lim = Number(limit) || SEAT_LIMIT;
  var pct = Math.min(100, Math.round((n / lim) * 100));
  $('dash-seats').textContent = (used === '?' ? '—' : (n + ' / ' + lim));
  var bar = $('dash-seats-bar');
  bar.querySelector('span').style.width = pct + '%';
  bar.classList.toggle('full', n >= lim);
  $('dash-seats-cap').textContent = n >= lim ? 'cap reached' : ((lim - n) + ' free');
}

/* ── TAB 2: seats ──────────────────────────────────────────────────────────── */
function loadSeats() {
  return callApi('GET', '/api/dashboard/seats').then(function (r) {
    var body = $('seats-body');
    var touchpoint = $('seat-cap-touchpoint');
    if (r.pending) {
      body.innerHTML = '';
      $('seats-empty').classList.remove('hidden');
      $('seat-cap-note').textContent = '? / ' + SEAT_LIMIT;
      $('btn-seat-mint').disabled = true;
      touchpoint.classList.add('hidden');
      return;
    }
    if (!r.ok) { setMsg('seats-msg', 'err', friendly(r.err)); return; }
    $('seats-empty').classList.add('hidden');
    var d = r.data || {};
    var seats = d.seats || [];
    var limit = d.limit || SEAT_LIMIT;
    var used = seats.length;
    $('seat-cap-note').textContent = used + ' / ' + limit;
    var atCap = used >= limit;
    $('btn-seat-mint').disabled = atCap;
    if (atCap) {
      touchpoint.classList.remove('hidden');
      touchpoint.textContent = t('seats.cap.reached');
    } else {
      touchpoint.classList.add('hidden');
    }
    if (!seats.length) {
      body.innerHTML = '';
      $('seats-empty').classList.remove('hidden');
      return;
    }
    $('seats-empty').classList.add('hidden');
    body.innerHTML = seats.map(function (s) {
      var active = (s.active !== false);
      return '<tr data-uid="' + esc(s.user_id) + '">' +
        '<td>' + esc(s.label || ('seat-' + String(s.user_id || '').slice(0, 8))) + '</td>' +
        '<td class="mono-id">' + esc(s.user_id) + '</td>' +
        '<td>' + esc(fmtDate(s.created_at)) + '</td>' +
        '<td><span class="status-pill ' + (active ? 'ok' : 'down') + '">' + (active ? 'active' : 'revoked') + '</span></td>' +
        '<td class="row-actions">' +
          '<button class="btn sm" data-act="wire" data-uid="' + esc(s.user_id) + '">wire</button>' +
          (active ? '<button class="btn sm danger" data-act="revoke" data-uid="' + esc(s.user_id) + '">revoke</button>' : '') +
        '</td></tr>';
    }).join('');
    qsa('#seats-body button[data-act]').forEach(function (btn) {
      btn.addEventListener('click', onSeatAction);
    });
  });
}

function onSeatAction(e) {
  var btn = e.currentTarget;
  var uid = btn.dataset.uid;
  if (btn.dataset.act === 'revoke') {
    confirmModal('Revoke seat', t('seats.revoke.confirm'), function () {
      callApi('DELETE', '/api/dashboard/seats/' + encodeURIComponent(uid)).then(function (r) {
        if (!r.ok) { setMsg('seats-msg', 'err', friendly(r.err)); return; }
        setMsg('seats-msg', 'ok', 'seat revoked.');
        loadSeats(); loadDashboard();
      });
    });
  } else if (btn.dataset.act === 'wire') {
    showSeatWire(uid);
  }
}

function showSeatWire(uid) {
  var base = S.mcpBase || guessMcpBase();
  var panel = $('seat-detail');
  panel.classList.remove('hidden');
  panel.innerHTML =
    '<div class="section-label" style="margin-top:0">Wiring for seat <span class="mono-id">' + esc(uid) + '</span></div>' +
    '<div class="kv"><span class="k">MCP tool URL</span><span class="v mono-id">' + esc(base) + '</span></div>' +
    '<div class="kv"><span class="k">Authorization</span><span class="v mono-id">Bearer &lt;seat token&gt;</span></div>' +
    '<div class="kv"><span class="k">X-OpenWebUI-User-Id</span><span class="v mono-id">' + esc(uid) + '</span></div>' +
    '<p class="hint">OpenWebUI: add an OpenAPI tool server at the URL above with Bearer auth = this seat\'s token. Set <code>ENABLE_FORWARD_USER_INFO_HEADERS=true</code> so the <code>X-OpenWebUI-User-Id</code> header scopes this seat\'s memory.</p>';
}

function mintSeat() {
  var label = ($('seat-label').value || '').trim();
  setMsg('seat-mint-msg', '', '');
  callApi('POST', '/api/dashboard/seats', label ? { label: label } : {}).then(function (r) {
    if (r.pending) { setMsg('seat-mint-msg', 'warn', t('err.pending')); return; }
    if (!r.ok) {
      if (r.err && r.err.status === 409) {
        $('btn-seat-mint').disabled = true;
        var tp = $('seat-cap-touchpoint'); tp.classList.remove('hidden'); tp.textContent = t('seats.cap.reached');
        setMsg('seat-mint-msg', 'err', 'seat cap reached.');
        return;
      }
      setMsg('seat-mint-msg', 'err', friendly(r.err)); return;
    }
    $('seat-label').value = '';
    var d = r.data || {};
    revealSecret('token', d.token, t('reveal.token.title'), t('reveal.token.warn'));
    setMsg('seat-mint-msg', 'ok', 'seat minted.');
    loadSeats(); loadDashboard();
  });
}

/* ── TAB 3: LLM Brain ──────────────────────────────────────────────────────── */
function buildBackendSelect() {
  var sel = $('llm-type');
  if (sel.options.length) return;
  BACKEND_TYPES.forEach(function (b) {
    var o = document.createElement('option');
    o.value = b; o.textContent = b; sel.appendChild(o);
  });
}

function loadLLM() {
  buildBackendSelect();
  return callApi('GET', '/api/dashboard/llm').then(function (r) {
    if (r.pending) { setMsg('llm-msg', 'warn', t('err.pending')); return; }
    if (!r.ok) { setMsg('llm-msg', 'err', friendly(r.err)); return; }
    var d = r.data || {};
    if (d.backend_type && BACKEND_TYPES.indexOf(d.backend_type) >= 0) $('llm-type').value = d.backend_type;
    else if (d.backend_type) { /* unknown type — still show it */
      var o = document.createElement('option'); o.value = d.backend_type; o.textContent = d.backend_type + ' (current)'; $('llm-type').appendChild(o);
      $('llm-type').value = d.backend_type;
    }
    $('llm-baseurl').value = d.base_url || '';
    $('llm-model').value = d.model || '';
    $('llm-apikey').value = '';
    $('llm-apikey').placeholder = d.api_key_set ? 'write-only — leave blank to keep' : 'optional — blank for local servers';
    $('llm-key-note').textContent = d.api_key_set ? t('brain.key.set') : t('brain.key.unset');
  });
}

function saveLLM() {
  var body = {
    backend_type: $('llm-type').value,
    base_url: $('llm-baseurl').value.trim()
  };
  var model = $('llm-model').value.trim();
  if (model) body.model = model;
  var key = $('llm-apikey').value;
  if (key) body.api_key = key;
  setMsg('llm-msg', '', '');
  callApi('PUT', '/api/dashboard/llm', body).then(function (r) {
    if (r.pending) { setMsg('llm-msg', 'warn', t('err.pending')); return; }
    if (!r.ok) { setMsg('llm-msg', 'err', friendly(r.err)); return; }
    $('llm-apikey').value = '';
    $('llm-key-note').textContent = body.api_key ? t('brain.key.set') : $('llm-key-note').textContent;
    setMsg('llm-msg', 'ok', t('brain.saved'));
  });
}

function testLLM() {
  setMsg('llm-msg', '', '');
  setMsg('llm-msg', '', 'probing…');
  callApi('POST', '/api/dashboard/llm/test', {}).then(function (r) {
    if (r.pending) { setMsg('llm-msg', 'warn', t('err.pending')); return; }
    if (!r.ok) { setMsg('llm-msg', 'err', friendly(r.err)); return; }
    var d = r.data || {};
    if (d.ok === false) setMsg('llm-msg', 'err', 'test failed — ' + (d.error || 'unknown'));
    else setMsg('llm-msg', 'ok', 'ok' + (d.latency_ms != null ? ' — ' + d.latency_ms + ' ms' : ''));
  });
}

/* ── TAB 4: OpenWebUI ──────────────────────────────────────────────────────── */
function guessMcpBase() {
  try { return window.location.origin.replace(/:\d+$/, '') + ':8002'; } catch (e) { return ''; }
}
function loadOpenWebUI() {
  return callApi('GET', '/api/dashboard/openwebui').then(function (r) {
    if (r.pending) {
      $('owui-url').textContent = guessMcpBase();
      $('owui-keyset').textContent = '?';
      $('owui-step-url').textContent = 'http://<host>:8002';
      return;
    }
    if (!r.ok) { setMsg('owui-msg', 'err', friendly(r.err)); return; }
    var d = r.data || {};
    var url = d.mcp_url || guessMcpBase();
    S.mcpBase = url;
    $('owui-url').textContent = url;
    $('owui-keyset').textContent = d.api_key_set ? t('brain.key.set') : t('brain.key.unset');
    if (d.filter_script) {
      var pre = $('owui-filter'); pre.textContent = d.filter_script;
    }
    var step = url.replace(/^https?:\/\//, '').replace(/\/mcp$/, '');
    $('owui-step-url').textContent = 'http://' + step;
  });
}

function rotateKey() {
  confirmModal('Rotate MCP key', t('owui.rotate.warn'), function () {
    callApi('POST', '/api/dashboard/openwebui/rotate-key', {}).then(function (r) {
      if (r.pending) { setMsg('owui-msg', 'warn', t('err.pending')); return; }
      if (!r.ok) { setMsg('owui-msg', 'err', friendly(r.err)); return; }
      var d = r.data || {};
      revealSecret('mcpkey', d.api_key, t('reveal.mcpkey.title'), t('reveal.mcpkey.warn'));
      setMsg('owui-msg', 'ok', 'key rotated.');
      loadOpenWebUI();
    });
  });
}

/* ── secret reveal modal ───────────────────────────────────────────────────── */
function revealSecret(kind, secret, title, warn) {
  var reveal = $('reveal');
  $('reveal-title').textContent = title;
  $('reveal-warn').textContent = warn;
  $('reveal-secret').textContent = secret || '—';
  $('reveal-msg').className = 'msg'; $('reveal-msg').textContent = '';
  reveal.classList.remove('hidden');
  $('reveal').dataset.secret = secret || '';
}
function closeReveal() { $('reveal').classList.add('hidden'); $('reveal').dataset.secret = ''; }

/* ── generic confirm modal ─────────────────────────────────────────────────── */
var _confirmCb = null;
function confirmModal(title, body, onConfirm) {
  $('modal-title').textContent = title;
  $('modal-body').textContent = body;
  $('modal-extra').innerHTML = '';
  _confirmCb = onConfirm;
  $('modal').classList.remove('hidden');
}
function closeModal() { $('modal').classList.add('hidden'); _confirmCb = null; }

/* ── date formatting ───────────────────────────────────────────────────────── */
function fmtDate(v) {
  if (!v) return '—';
  var d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

/* ── guided tour (localStorage-only, no server sync) ───────────────────────── */
var TOUR_STEPS = [
  { sel: '#statusbar',     title: 'Status bar',     body: 'Instance health, version, and the live status dot. Health polls every 10s.' },
  { sel: '#tabs',          title: 'Six tabs',       body: 'Dashboard, Seats & Tokens, LLM Brain, OpenWebUI, Help, and the FOSS vs SaaS comparison.' },
  { sel: '[data-tab="seats"]', title: 'Seats',      body: 'Mint up to 5 seats — one per person. Each token is shown ONCE, so store it immediately. A seat token is for a single-user client (Claude Desktop, opencode). OpenWebUI is different: wire it once on the OpenWebUI tab with the instance MCP key, and each signed-in user is scoped automatically.' },
  { sel: '[data-tab="brain"]', title: 'LLM Brain',  body: 'Point FaultLine at a model you already run (Ollama, LM Studio, OpenWebUI, or a hosted API). Restart the backend after saving.' },
  { sel: '[data-tab="openwebui"]', title: 'OpenWebUI', body: 'The supported wiring path and your MCP tool URL. Rotate the MCP key anytime.' }
];

function maybeOfferTour() {
  var seen = false;
  try { seen = !!localStorage.getItem('fl_foss_tour_seen'); } catch (e) {}
  if (!seen) {
    var p = $('firstrun'); if (p) p.classList.remove('hidden');
  }
}
function dismissTourPrompt() {
  try { localStorage.setItem('fl_foss_tour_seen', '1'); } catch (e) {}
  $('firstrun').classList.add('hidden');
}
function startTour() {
  $('firstrun').classList.add('hidden');
  try { localStorage.setItem('fl_foss_tour_seen', '1'); } catch (e) {}
  runTourStep(0);
}
function endTour() {
  var ov = document.querySelector('.tour-overlay'); if (ov) ov.remove();
  var card = document.querySelector('.tour-card'); if (card) card.remove();
  var sp = document.querySelector('.tour-spotlight'); if (sp) sp.remove();
}
function runTourStep(i) {
  endTour();
  if (i >= TOUR_STEPS.length) { showView('dashboard'); return; }
  var step = TOUR_STEPS[i];
  var target = document.querySelector(step.sel);
  var overlay = document.createElement('div'); overlay.className = 'tour-overlay active';
  overlay.addEventListener('click', function () { runTourStep(i + 1); });
  document.body.appendChild(overlay);

  var card = document.createElement('div'); card.className = 'tour-card';
  card.innerHTML = '<h4>' + esc((i + 1) + '. ' + step.title) + '</h4><p>' + esc(step.body) + '</p>' +
    '<div class="tour-actions">' +
      (i > 0 ? '<button class="btn sm" data-act="prev">‹ back</button>' : '') +
      '<button class="btn sm" data-act="skip">skip</button>' +
      '<button class="btn sm accent" data-act="next">' + (i === TOUR_STEPS.length - 1 ? 'done' : 'next ›') + '</button>' +
    '</div>';
  document.body.appendChild(card);
  card.querySelector('[data-act="next"]').addEventListener('click', function (e) { e.stopPropagation(); runTourStep(i + 1); });
  if (i > 0) card.querySelector('[data-act="prev"]').addEventListener('click', function (e) { e.stopPropagation(); runTourStep(i - 1); });
  card.querySelector('[data-act="skip"]').addEventListener('click', function (e) { e.stopPropagation(); endTour(); });

  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    var spot = document.createElement('div'); spot.className = 'tour-spotlight';
    document.body.appendChild(spot);
    /* defer position until after scroll */
    setTimeout(function () {
      var r = target.getBoundingClientRect();
      spot.style.top = (r.top - 4) + 'px';
      spot.style.left = (r.left - 4) + 'px';
      spot.style.width = (r.width + 8) + 'px';
      spot.style.height = (r.height + 8) + 'px';
      var cr = card.getBoundingClientRect();
      var top = r.bottom + 12;
      if (top + cr.height > window.innerHeight - 12) top = Math.max(12, r.top - cr.height - 12);
      card.style.top = top + 'px';
      var left = Math.max(12, Math.min(r.left, window.innerWidth - cr.width - 12));
      card.style.left = left + 'px';
    }, 180);
  } else {
    var cr2 = card.getBoundingClientRect();
    card.style.top = Math.max(12, (window.innerHeight - cr2.height) / 2) + 'px';
    card.style.left = Math.max(12, (window.innerWidth - cr2.width) / 2) + 'px';
  }
}

/* ── wiring ────────────────────────────────────────────────────────────────── */
function wire() {
  initTheme(); initAccent(); applyI18n(); loadToken();

  $('btn-theme').addEventListener('click', toggleTheme);
  qsa('#accent-picker .swatch').forEach(function (b) {
    b.addEventListener('click', function () {
      var cur = document.documentElement.getAttribute('data-accent') || '';
      applyAccent(cur === b.dataset.accent ? '' : b.dataset.accent);
    });
  });

  $('btn-connect').addEventListener('click', connect);
  $('in-token').addEventListener('keydown', function (e) { if (e.key === 'Enter') connect(); });
  $('btn-logout').addEventListener('click', logout);

  qsa('#tabs button').forEach(function (b) {
    b.addEventListener('click', function () { showView(b.dataset.tab); });
  });

  $('btn-seat-mint').addEventListener('click', mintSeat);
  $('btn-llm-save').addEventListener('click', saveLLM);
  $('btn-llm-test').addEventListener('click', testLLM);
  $('btn-owui-rotate').addEventListener('click', rotateKey);
  $('btn-owui-copy-filter').addEventListener('click', function () {
    copyText($('owui-filter').textContent).then(function (ok) {
      setMsg('owui-msg', ok ? 'ok' : 'err', ok ? 'filter script copied.' : 'copy failed — select and copy manually.');
    });
  });

  $('btn-help').addEventListener('click', function () { showView('help'); });
  $('btn-tour').addEventListener('click', startTour);
  $('firstrun-go').addEventListener('click', startTour);
  $('firstrun-dismiss').addEventListener('click', dismissTourPrompt);

  $('modal-cancel').addEventListener('click', closeModal);
  $('modal-ok').addEventListener('click', function () { var cb = _confirmCb; closeModal(); if (cb) cb(); });

  $('reveal-close').addEventListener('click', closeReveal);
  $('reveal-copy').addEventListener('click', function () {
    var secret = $('reveal').dataset.secret || '';
    copyText(secret).then(function (ok) {
      setMsg('reveal-msg', ok ? 'ok' : 'err', ok ? 'copied.' : 'copy failed — select the text manually.');
    });
  });

  /* if already holding a token, skip straight into the console */
  if (S.token) enterApp();
}

document.addEventListener('DOMContentLoaded', wire);
