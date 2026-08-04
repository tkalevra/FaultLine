/*
  FaultLine FOSS control plane — webui string map
  SPDX-License-Identifier: AGPL-3.0-only
  License: GNU AGPL v3 — see ./LICENSE in the repo root.

  i18n convention (ITALIAN on this branch — foss-it):
  - Every user-visible string lives here, keyed by a stable dotted id.
  - HTML references strings via data-i18n="key"; app.js applies them on load
    and after every render via applyI18n().
  - JS reads strings via t("key"). Dynamic/long bodies can also be looked up.
  - A translation pass (foss-it / foss-es) swaps ONLY this file. Keep keys stable.

  Translation notes:
  - "GNU Affero General Public License v3" is the licence's PROPER NAME and is
    never translated.
  - Conventionally-English technical terms stay in English: Docker, endpoint,
    token, bearer, MCP, OpenWebUI, backend, stack, hosting, self-hosted, Base URL.
  - "seat" -> "postazione"; "Brain" -> "Cervello" (a product metaphor rather than
    a technical term, so it reads better translated).
*/
(function () {
  'use strict';

  var STRINGS = {
    "sb.scope.signedout": "non autenticato",
    "sb.scope.signedin":  "operatore",
    "sb.instance": "istanza",
    "sb.mode.offline": "offline",
    "sb.mode.online":  "online",
    "sb.mode.degraded":"degradato",

    "head.title": "Pannello di controllo FaultLine",
    "head.subtitle": "self-hosted · AGPL v3 · istanza singola",
    "head.help": "? Aiuto",
    "head.logout": "esci",

    "login.title": "Accesso operatore",
    "login.intro": "Incolla una sola volta il tuo token di amministrazione. È il bearer dell'operatore per l'API di controllo di questa istanza, memorizzato solo in questo browser e inviato come Authorization: Bearer a ogni chiamata.",
    "login.token": "Token di amministrazione",
    "login.connect": "▸ connetti",
    "login.where": "Dove trovo il token?",

    "tab.dashboard": "Dashboard",
    "tab.seats":  "Postazioni e token",
    "tab.brain":  "Cervello LLM",
    "tab.openwebui": "OpenWebUI",
    "tab.help":   "Aiuto",
    "tab.compare":"FOSS vs SaaS",

    "dash.health": "Stato dello stack",
    "dash.summary":"Riepilogo dell'istanza",
    "dash.seats":  "Postazioni",
    "dash.brain":  "Cervello",
    "dash.llmkey": "Chiave API LLM",

    "seats.title": "Postazioni",
    "seats.help": "Una postazione è l'archivio di memoria di un singolo utente finale, isolato nel proprio schema di database. Creando una postazione, il token di connessione viene restituito una sola volta: conservalo subito. Usalo come token Bearer in un client a utente singolo (Claude Desktop, opencode); OpenWebUI si configura separatamente con la chiave MCP. Ogni postazione è identificata dal proprio user_id.",
    "seats.mint":  "＋ crea postazione",
    "seats.list":  "Elenco delle postazioni",
    "seats.col.label": "Etichetta",
    "seats.col.created":"Creata",
    "seats.col.status": "Stato",
    "seats.col.actions":"Azioni",
    "seats.empty": "nessuna postazione — creane una qui sopra",
    "seats.cap.reached": "L'edizione FOSS supporta fino a 5 postazioni. Per un numero maggiore di postazioni sono disponibili i piani hosted — vedi https://faultline.ca/pricing",
    "seats.revoke.confirm": "Revocare questa postazione? Il suo archivio di memoria viene conservato, ma il token di connessione smette di funzionare immediatamente.",

    "brain.title": "Cervello LLM",
    "brain.what": "Che cos'è il Cervello?",
    "brain.type": "Tipo di backend",
    "brain.baseurl": "Base URL",
    "brain.model": "Modello",
    "brain.apikey": "Chiave API",
    "brain.save": "▸ salva la configurazione del Cervello",
    "brain.test": "⚡ verifica la connessione",
    "brain.saved": "salvato — riavvia il container del backend perché le modifiche abbiano effetto.",
    "brain.key.set": "impostata",
    "brain.key.unset": "non impostata",

    "owui.title": "Integrazione con OpenWebUI",
    "owui.mcp": "URL dello strumento MCP",
    "owui.keyset": "chiave MCP impostata",
    "owui.rotate": "↻ ruota la chiave MCP",
    "owui.steps.title": "Collegare OpenWebUI (percorso consigliato)",
    "owui.steps.intro": "Il percorso supportato è il tool server OpenAPI: inoltra in modo affidabile l'header di identità per utente su cui FaultLine definisce lo scope. Verificato con OpenWebUI v0.10.x; i percorsi valgono dalla v0.6.31 in poi.",
    "owui.filter": "Script del filtro (inlet/outlet legacy)",
    "owui.filter.placeholder": "-- collegamento al backend non ancora attivo -- lo script del filtro attivo viene servito da GET /api/dashboard/openwebui",
    "owui.copy": "⎘ copia",
    "owui.weak": "Il modello non richiama gli strumenti? Passa a Chat Controls → Advanced Params → Function Calling → Legacy. Il filtro legacy inlet/outlet si trova in openwebui/faultline_function.py (Workspace → Functions) per l'iniezione automatica.",
    "owui.rotate.warn": "La rotazione genera una nuova chiave MCP e invalida immediatamente quella precedente. Ogni configurazione di strumenti in OpenWebUI che usa la vecchia chiave deve essere aggiornata.",

    "help.tour": "Tour guidato",
    "help.tour.intro": "Una panoramica di questa console in 5 tappe. Parte automaticamente alla prima visita; puoi ripeterlo quando vuoi.",
    "help.tour.start": "▸ avvia il tour",
    "help.memory": "Come funziona la memoria",
    "help.expand": "/expand — intelligenza di dominio",
    "help.correct": "Correzioni e ritrattazioni",
    "help.connect": "Collegare un modello",

    "cmp.title": "FOSS vs SaaS",
    "cmp.intro": "Lo stesso motore, due modi di eseguirlo. Questo confronto è fattuale, non promozionale: scegli ciò che fa al caso tuo.",
    "cmp.foss": "Self-hosted (FOSS)",
    "cmp.saas": "FaultLine SaaS",
    "cmp.row.license": "Licenza",
    "cmp.row.seats": "Postazioni",
    "cmp.row.hosting": "Hosting",
    "cmp.row.brain": "Cervello LLM",
    "cmp.row.data": "Posizione dei dati",
    "cmp.row.updates": "Aggiornamenti",
    "cmp.row.audit": "Log di audit / cronologia eventi",
    "cmp.row.multi": "Isolamento multi-tenant",
    "cmp.row.serve": "Modalità Serve / Train (agente in sola lettura)",
    "cmp.row.support": "Supporto",
    "cmp.row.auth": "Autenticazione del control plane",
    "cmp.row.price": "Prezzo",
    "cmp.cta": "Scopri FaultLine SaaS → https://faultline.ca",

    "modal.cancel": "annulla",
    "modal.confirm": "conferma",
    "reveal.copy": "⎘ copia negli appunti",
    "reveal.close": "L'ho salvato — chiudi",
    "reveal.token.title": "Token della postazione — mostrato una sola volta",
    "reveal.token.warn": "Conservalo ora — non verrà mostrato di nuovo. Questo è un token di POSTAZIONE: usalo come token Bearer in un client a utente singolo come Claude Desktop o opencode. OpenWebUI non usa i token di postazione — configuralo invece una sola volta con la chiave MCP di questa istanza.",
    "reveal.mcpkey.title": "Nuova chiave MCP — mostrata una sola volta",
    "reveal.mcpkey.warn": "Conservala ora. Non verrà mostrata di nuovo. La chiave precedente smette di funzionare immediatamente.",

    "lic.badge": "AGPL v3",
    "lic.sub": "software libero — con obblighi",
    "lic.body": "FaultLine FOSS è concesso in licenza secondo la GNU Affero General Public License v3. Puoi eseguirlo, studiarlo, modificarlo e condividerlo liberamente. A questo si accompagnano due condizioni: se lo modifichi e ne consenti l'uso ad altri attraverso una rete, devi offrire loro il codice sorgente modificato; e tutto ciò che ridistribuisci resta sotto la stessa licenza.",
    "lic.scope": "Questa è l'edizione self-hosted. La tua memoria, il tuo database, il tuo endpoint del modello — a noi non viene inviato nulla. Effettuando l'accesso, gestisci la tua istanza secondo questi termini.",
    "lic.full": "leggi la licenza completa",
    "lic.gnu": "AGPL v3 su gnu.org",

    "tour.welcome": "Benvenuto",
    "tour.firstrun": "È la prima volta? Fai un tour di 30 secondi del pannello di controllo.",
    "tour.dismiss": "più tardi",
    "tour.go": "▸ avvia il tour",
    "tour.back": "‹ indietro",
    "tour.skip": "salta",
    "tour.next": "avanti ›",
    "tour.done": "fine",

    "tour.s1.title": "Barra di stato",
    "tour.s1.body": "Stato dell'istanza, versione e indicatore di stato in tempo reale. Lo stato viene aggiornato ogni 10 s.",
    "tour.s2.title": "Sei schede",
    "tour.s2.body": "Dashboard, Postazioni e token, Cervello LLM, OpenWebUI, Aiuto e il confronto FOSS vs SaaS.",
    "tour.s3.title": "Postazioni",
    "tour.s3.body": "Crea fino a 5 postazioni, una per persona. Ogni token viene mostrato UNA SOLA VOLTA, quindi conservalo subito. Un token di postazione serve per un client a utente singolo (Claude Desktop, opencode). OpenWebUI funziona diversamente: si configura una sola volta nella scheda OpenWebUI con la chiave MCP dell'istanza, e ogni utente autenticato ottiene automaticamente il proprio scope.",
    "tour.s4.title": "Cervello LLM",
    "tour.s4.body": "Collega FaultLine a un modello che già esegui (Ollama, LM Studio, OpenWebUI o un'API hosted). Riavvia il backend dopo aver salvato.",
    "tour.s5.title": "OpenWebUI",
    "tour.s5.body": "Il percorso di configurazione supportato e l'URL del tuo strumento MCP. Puoi ruotare la chiave MCP in qualsiasi momento.",

    "footer": "FAULTLINE · MEMORIA CON SCRITTURA VALIDATA · FOSS · AGPL V3",

    "err.network": "errore di rete — endpoint non raggiungibile",
    "err.401": "401 — token rifiutato. Effettua di nuovo l'accesso.",
    "err.pending": "collegamento al backend non ancora attivo",
    "err.pending.detail": "Questo endpoint di controllo non è ancora attivo sul backend. L'interfaccia si adatta senza errori; la sua configurazione da parte dell'operatore è separata da questa console."
  };

  function t(key) {
    var v = STRINGS[key];
    return v == null ? key : v;
  }

  window.FL_STRINGS = STRINGS;
  window.t = t;
})();
