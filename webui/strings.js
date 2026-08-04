/*
  FaultLine FOSS control plane — webui string map
  SPDX-License-Identifier: AGPL-3.0-only
  License: GNU AGPL v3 — see ./LICENSE in the repo root.

  i18n convention (SPANISH on this branch — foss-es):
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
  - "seat" -> "puesto"; "Brain" -> "Cerebro" (a product metaphor rather than a
    technical term, so it reads better translated).
*/
(function () {
  'use strict';

  var STRINGS = {
    "lic.line": "Con licencia GNU Affero General Public License v3 \u2014 libre para ejecutar, estudiar, modificar y compartir.",
    "lic.contact": "Para permisos o colaboraci\u00f3n,",
    "lic.full": "leer la licencia completa",
    "sb.scope.signedout": "sin iniciar sesión",
    "sb.scope.signedin":  "operador",
    "sb.instance": "instancia",
    "sb.mode.offline": "offline",
    "sb.mode.online":  "online",
    "sb.mode.degraded":"degradado",

    "head.title": "Panel de control de FaultLine",
    "head.subtitle": "self-hosted · AGPL v3 · instancia única",
    "head.help": "? Ayuda",
    "head.logout": "cerrar sesión",

    "login.title": "Acceso de operador",
    "login.intro": "Pega tu token de administración una sola vez. Es el bearer del operador para la API de control de esta instancia, se guarda solo en este navegador y se envía como Authorization: Bearer en cada llamada.",
    "login.token": "Token de administración",
    "login.token.note": "bearer del operador",
    "login.connect": "▸ conectar",
    "login.where": "¿Dónde consigo el token?",

    "tab.dashboard": "Dashboard",
    "tab.seats":  "Puestos y tokens",
    "tab.brain":  "Cerebro LLM",
    "tab.openwebui": "OpenWebUI",
    "tab.help":   "Ayuda",
    "tab.compare":"FOSS vs SaaS",

    "dash.health": "Estado del stack",
    "dash.summary":"Resumen de la instancia",
    "dash.seats":  "Puestos",
    "dash.brain":  "Cerebro",
    "dash.llmkey": "Clave API del LLM",

    "seats.title": "Puestos",
    "seats.title.note": "hasta 5 · FOSS",
    "seats.help": "Un puesto es el almacén de memoria de un único usuario final, aislado en su propio esquema de base de datos. Al crear un puesto, el token de conexión se muestra una sola vez: guárdalo de inmediato. Úsalo como token Bearer en un cliente de usuario único (Claude Desktop, opencode); OpenWebUI se configura por separado con la clave MCP. Cada puesto se identifica por su user_id.",
    "seats.mint":  "＋ crear puesto",
    "seats.list":  "Lista de puestos",
    "seats.col.label": "Etiqueta",
    "seats.col.created":"Creado",
    "seats.col.status": "Estado",
    "seats.col.actions":"Acciones",
    "seats.empty": "todavía no hay puestos — crea uno arriba",
    "seats.cap.reached": "La edición FOSS admite hasta 5 puestos. Para un número mayor de puestos están disponibles los planes hosted — consulta https://faultline.ca/pricing",
    "seats.revoke.confirm": "¿Revocar este puesto? Su almacén de memoria se conserva, pero su token de conexión deja de funcionar de inmediato.",

    "brain.title": "Cerebro LLM",
    "brain.title.note": "conecta tu modelo",
    "brain.what": "¿Qué es el Cerebro?",
    "brain.type": "Tipo de backend",
    "brain.baseurl": "Base URL",
    "brain.baseurl.note": "host + puerto, sin ruta",
    "brain.model": "Modelo",
    "brain.model.note": "opcional",
    "brain.apikey": "Clave API",
    "brain.save": "▸ guardar la configuración del Cerebro",
    "brain.test": "⚡ probar la conexión",
    "brain.saved": "guardado — reinicia el contenedor del backend para que los cambios surtan efecto.",
    "brain.key.set": "establecida",
    "brain.key.unset": "sin establecer",

    "owui.title": "Integración con OpenWebUI",
    "owui.mcp": "URL de la herramienta MCP",
    "owui.keyset": "clave MCP establecida",
    "owui.rotate": "↻ rotar la clave MCP",
    "owui.steps.title": "Conectar OpenWebUI (ruta recomendada)",
    "owui.steps.intro": "La ruta admitida es el tool server OpenAPI: reenvía de forma fiable la cabecera de identidad por usuario sobre la que FaultLine define el ámbito. Verificado con OpenWebUI v0.10.x; las rutas se aplican desde la v0.6.31 en adelante.",
    "owui.filter": "Script del filtro (inlet/outlet legacy)",
    "owui.filter.placeholder": "-- conexión con el backend pendiente -- el script del filtro activo lo sirve GET /api/dashboard/openwebui",
    "owui.copy": "⎘ copiar",
    "owui.weak": "¿El modelo no llama a las herramientas? Cambia a Chat Controls → Advanced Params → Function Calling → Legacy. El filtro legacy inlet/outlet está en openwebui/faultline_function.py (Workspace → Functions) para la inyección automática.",
    "owui.rotate.warn": "Al rotar se genera una nueva clave MCP y la anterior queda invalidada de inmediato. Debe actualizarse cualquier configuración de herramientas de OpenWebUI que use la clave antigua.",

    "help.tour": "Tour guiado",
    "help.tour.intro": "Un recorrido de 5 paradas por esta consola. Se inicia automáticamente en la primera visita; puedes repetirlo cuando quieras.",
    "help.tour.start": "▸ iniciar el tour",
    "help.memory": "Cómo funciona la memoria",
    "help.expand": "/expand — inteligencia de dominio",
    "help.correct": "Correcciones y retractaciones",
    "help.connect": "Conectar un modelo",

    "cmp.title": "FOSS vs SaaS",
    "cmp.title.note": "objetivo · en paralelo",
    "cmp.intro": "El mismo motor, dos formas de ejecutarlo. Esta comparación es objetiva, no comercial: elige la que te convenga.",
    "cmp.foss": "Self-hosted (FOSS)",
    "cmp.saas": "FaultLine SaaS",
    "cmp.row.license": "Licencia",
    "cmp.row.seats": "Puestos",
    "cmp.row.hosting": "Hosting",
    "cmp.row.brain": "Cerebro LLM",
    "cmp.row.data": "Ubicación de los datos",
    "cmp.row.updates": "Actualizaciones",
    "cmp.row.audit": "Registro de auditoría / historial de eventos",
    "cmp.row.multi": "Aislamiento multiinquilino",
    "cmp.row.serve": "Modo Serve / Train (agente de solo lectura)",
    "cmp.row.support": "Soporte",
    "cmp.row.auth": "Autenticación del control plane",
    "cmp.row.price": "Precio",
    "cmp.cta": "Descubre FaultLine SaaS → https://faultline.ca",

    "modal.cancel": "cancelar",
    "modal.confirm": "confirmar",
    "reveal.copy": "⎘ copiar al portapapeles",
    "reveal.close": "Ya lo he guardado — cerrar",
    "reveal.token.title": "Token del puesto — se muestra una sola vez",
    "reveal.token.warn": "Guárdalo ahora — no volverá a mostrarse. Este es un token de PUESTO: úsalo como token Bearer en un cliente de usuario único como Claude Desktop u opencode. OpenWebUI no usa tokens de puesto — configúralo una sola vez con la clave MCP de esta instancia.",
    "reveal.mcpkey.title": "Nueva clave MCP — se muestra una sola vez",
    "reveal.mcpkey.warn": "Guárdala ahora. No volverá a mostrarse. La clave anterior deja de funcionar de inmediato.",

    "tour.welcome": "Bienvenido",
    "tour.firstrun": "¿Es tu primera vez? Haz un recorrido de 30 segundos por el panel de control.",
    "tour.dismiss": "más tarde",
    "tour.go": "▸ iniciar el tour",
    "tour.back": "‹ atrás",
    "tour.skip": "omitir",
    "tour.next": "siguiente ›",
    "tour.done": "listo",

    "tour.s1.title": "Barra de estado",
    "tour.s1.body": "Estado de la instancia, versión e indicador de estado en tiempo real. El estado se consulta cada 10 s.",
    "tour.s2.title": "Seis pestañas",
    "tour.s2.body": "Dashboard, Puestos y tokens, Cerebro LLM, OpenWebUI, Ayuda y la comparación FOSS vs SaaS.",
    "tour.s3.title": "Puestos",
    "tour.s3.body": "Crea hasta 5 puestos, uno por persona. Cada token se muestra UNA SOLA VEZ, así que guárdalo de inmediato. Un token de puesto sirve para un cliente de usuario único (Claude Desktop, opencode). OpenWebUI funciona de otra manera: se configura una sola vez en la pestaña OpenWebUI con la clave MCP de la instancia, y cada usuario con sesión iniciada obtiene su ámbito automáticamente.",
    "tour.s4.title": "Cerebro LLM",
    "tour.s4.body": "Apunta FaultLine a un modelo que ya estés ejecutando (Ollama, LM Studio, OpenWebUI o una API hosted). Reinicia el backend después de guardar.",
    "tour.s5.title": "OpenWebUI",
    "tour.s5.body": "La ruta de configuración admitida y la URL de tu herramienta MCP. Puedes rotar la clave MCP cuando quieras.",

    "footer": "FAULTLINE · MEMORIA CON ESCRITURA VALIDADA · FOSS · AGPL V3",

    "err.network": "error de red — endpoint inaccesible",
    "err.401": "401 — token rechazado. Vuelve a iniciar sesión.",
    "err.pending": "conexión con el backend pendiente",
    "err.pending.detail": "Este endpoint de control todavía no está activo en el backend. La interfaz se muestra sin errores; su configuración por parte del operador es independiente de esta consola."
  };

  function t(key) {
    var v = STRINGS[key];
    return v == null ? key : v;
  }

  window.FL_STRINGS = STRINGS;
  window.t = t;
})();
