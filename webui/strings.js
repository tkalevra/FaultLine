/*
  FaultLine FOSS control plane — webui string map
  SPDX-License-Identifier: AGPL-3.0-only
  License: GNU AGPL v3 — see ./LICENSE in the repo root.

  i18n convention (CANADIAN FRENCH / fr-CA on this branch — foss-ca-fr):
  - Every user-visible string lives here, keyed by a stable dotted id.
  - HTML references strings via data-i18n="key"; app.js applies them on load
    and after every render via applyI18n().
  - JS reads strings via t("key"). Dynamic/long bodies can also be looked up.
  - A translation pass (foss-it / foss-es / foss-ca-fr) swaps ONLY this file.
    Keep keys stable.

  ── THIS IS CANADIAN FRENCH (fr-CA), NOT FRANCE FRENCH ──────────────────────
  Terminology follows the Office québécois de la langue française (OQLF) —
  Grand dictionnaire terminologique (GDT) / Banque de dépannage linguistique
  (BDL). Sources actually consulted, with the GDT record used:

  - "log in" / "sign in"   -> «ouvrir une session»   GDT record 2072335
                              («se loguer» is flagged there as a calque to avoid)
  - "log out" / "sign out" -> «fermer une session»   GDT record 2072333
  - "token" (auth)         -> «jeton» / «jeton d'authentification»  GDT 26557556
    ⚠️ DELIBERATE DIVERGENCE from foss-it / foss-es, which both keep "token" in
    English. Quebec French genuinely translates it; leaving "token" here would
    read as France/anglicised usage, which is the exact thing this branch is for.
  - "clipboard"            -> «presse-papiers»       GDT 8408473
  - "hosting"              -> «hébergement»          GDT 2070529
  - "revoke"               -> «révoquer»             GDT 17493434
  - "endpoint" (API/HTTP)  -> «point de terminaison». NOTE: TERMIUM Plus gives
    «terminal final» / «point terminal» for endpoint, but that record is the
    HARDWARE sense (a network-attached device). For the HTTP/API sense the
    industry-standard French — and what a fr-CA reader will recognise — is
    «point de terminaison». This one is not a Canada-vs-France split.
  - "support"              -> «soutien technique» (fr-CA prefers «soutien»;
                              «support» in this sense is an anglicism)
  - "plan" (commercial)    -> «forfait» (the standard fr-CA word for a plan)
  - "anytime"              -> «en tout temps» (idiomatic fr-CA)

  NOTE, honestly: the classic fr-CA markers (courriel, clavardage,
  baladodiffusion, téléverser, mot de passe) do NOT occur in this string set —
  it contains no email, chat, podcast, upload or password surface. The fr-CA
  choices that DO bite here are the session verbs, «jeton», «soutien»,
  «forfait», «en tout temps», and the typography below.

  ── TYPOGRAPHY (verified against the BDL, not guessed) ──────────────────────
  Per the OQLF Banque de dépannage linguistique, "Espacement avant et après les
  signes de ponctuation et les symboles":
    ":"  -> ESPACE INSÉCABLE before, ordinary space after. Written in the
            strings below as the explicit escape \u00a0 rather than a literal
            U+00A0 byte, so the space is auditable in source.
    ";"  -> NO space before (the BDL: the Office "opte pour l'absence d'espace").
    "!"  -> NO space before.
    "?"  -> NO space before.
  This is where fr-CA departs from typographic practice in France, which puts a
  thin/non-breaking space before all four. Do NOT "fix" the missing spaces
  before ; ! ? — they are correct here and they are cited.
  EXCEPTION, deliberate: literal technical tokens keep their code form with no
  inserted space — "Authorization: Bearer", URLs, menu paths. The BDL itself
  carves out digital time formats (13:52:45); the same logic applies to code.

  ── WHAT IS NOT TRANSLATED ──────────────────────────────────────────────────
  - Proper names, verbatim: FaultLine, "GNU Affero General Public License v3",
    MCP, PostgreSQL, Qdrant, OpenWebUI, Ollama, LM Studio, Claude Desktop,
    opencode, AGPL v3, FOSS, SaaS, Docker.
  - Literal UI paths of OTHER products, verbatim, because the reader has to find
    them on an English screen: Chat Controls → Advanced Params → Function
    Calling → Legacy, Workspace → Functions.
  - Code/identifier literals: user_id, .env keys, file paths, HTTP headers.
  - Conventionally-English terms kept as-is (same call foss-it/foss-es made):
    backend, stack, bearer, Base URL, API, LLM, inlet/outlet, legacy.
  - "seat" -> «poste» (the established fr-CA licensing unit — «licence par
    poste»); "Brain" -> «Cerveau» (a product metaphor, so it reads better
    translated — the same call foss-it/foss-es made with Cervello/Cerebro).
*/
(function () {
  'use strict';

  var STRINGS = {
    "lic.line": "Sous licence GNU Affero General Public License v3 — libre d'utilisation, d'étude, de modification et de partage.",
    "lic.contact": "Pour les autorisations ou la collaboration,",
    "lic.full": "lire la licence intégrale",
    "sb.scope.signedout": "session non ouverte",
    "sb.scope.signedin":  "opérateur",
    "sb.instance": "instance",
    "sb.mode.offline": "hors ligne",
    "sb.mode.online":  "en ligne",
    "sb.mode.degraded":"dégradé",

    "head.title": "Plan de contrôle FaultLine",
    "head.subtitle": "auto-hébergé · AGPL v3 · instance unique",
    "head.help": "? Aide",
    "head.logout": "fermer la session",

    "login.title": "Ouverture de session — opérateur",
    "login.intro": "Collez votre jeton d'administration une seule fois. C'est le jeton bearer de l'opérateur pour l'API de contrôle de cette instance; il est conservé uniquement dans ce navigateur et envoyé dans l'en-tête Authorization: Bearer à chaque appel.",
    "login.token": "Jeton d'administration",
    "login.token.note": "jeton bearer de l'opérateur",
    "login.connect": "▸ se connecter",
    "login.where": "Où puis-je obtenir le jeton?",

    "tab.dashboard": "Tableau de bord",
    "tab.seats":  "Postes et jetons",
    "tab.brain":  "Cerveau LLM",
    "tab.openwebui": "OpenWebUI",
    "tab.help":   "Aide",
    "tab.compare":"FOSS ou SaaS",

    "dash.health": "État du stack",
    "dash.summary":"Résumé de l'instance",
    "dash.seats":  "Postes",
    "dash.brain":  "Cerveau",
    "dash.llmkey": "Clé API du LLM",

    "seats.title": "Postes",
    "seats.title.note": "jusqu'à 5 · FOSS",
    "seats.help": "Un poste correspond à la mémoire d'un utilisateur final, cloisonnée dans son propre schéma de base de données. La création d'un poste retourne un jeton de connexion une seule fois — conservez-le immédiatement. Utilisez-le comme jeton Bearer dans un client mono-utilisateur (Claude Desktop, opencode); OpenWebUI se raccorde séparément au moyen de la clé MCP. Chaque poste est identifié par son user_id.",
    "seats.mint":  "＋ créer un poste",
    "seats.list":  "Liste des postes",
    "seats.col.label": "Étiquette",
    "seats.col.created":"Création",
    "seats.col.status": "État",
    "seats.col.actions":"Actions",
    "seats.empty": "aucun poste pour l'instant — créez-en un ci-dessus",
    "seats.cap.reached": "L'édition FOSS prend en charge jusqu'à 5 postes. Un plus grand nombre de postes est offert dans les forfaits hébergés — voir https://faultline.ca/pricing",
    "seats.revoke.confirm": "Révoquer ce poste? Sa mémoire est conservée, mais son jeton de connexion cesse de fonctionner immédiatement.",

    "brain.title": "Cerveau LLM",
    "brain.title.note": "raccordez votre modèle",
    "brain.what": "Qu'est-ce que le Cerveau?",
    "brain.type": "Type de backend",
    "brain.baseurl": "URL de base",
    "brain.baseurl.note": "hôte + port, sans chemin",
    "brain.model": "Modèle",
    "brain.model.note": "facultatif",
    "brain.apikey": "Clé API",
    "brain.save": "▸ enregistrer la configuration du Cerveau",
    "brain.test": "⚡ tester la connexion",
    "brain.saved": "enregistré — redémarrez le conteneur backend pour que les changements prennent effet.",
    "brain.key.set": "définie",
    "brain.key.unset": "non définie",

    "owui.title": "Intégration à OpenWebUI",
    "owui.mcp": "URL de l'outil MCP",
    "owui.keyset": "clé MCP définie",
    "owui.rotate": "↻ renouveler la clé MCP",
    "owui.steps.title": "Raccorder OpenWebUI (méthode recommandée)",
    "owui.steps.intro": "La méthode prise en charge est le serveur d'outils OpenAPI — il transmet de façon fiable l'en-tête d'identité par utilisateur sur lequel FaultLine cloisonne les données. Vérifié avec OpenWebUI v0.10.x; les chemins s'appliquent à partir de la v0.6.31.",
    "owui.filter": "Script de filtre (inlet/outlet hérité)",
    "owui.filter.placeholder": "-- raccordement backend à venir -- le script de filtre actif est servi par GET /api/dashboard/openwebui",
    "owui.copy": "⎘ copier",
    "owui.weak": "Un modèle peu performant n'appelle pas les outils? Passez à Chat Controls → Advanced Params → Function Calling → Legacy. Le filtre inlet/outlet hérité se trouve dans openwebui/faultline_function.py (Workspace → Functions) pour l'injection automatique.",
    "owui.rotate.warn": "Le renouvellement génère une nouvelle clé MCP et invalide l'ancienne immédiatement. Toute configuration d'outil OpenWebUI utilisant l'ancienne clé doit être mise à jour.",

    "help.tour": "Visite guidée",
    "help.tour.intro": "Un parcours en 5 étapes de cette console. Se lance une fois à la première visite; rejouable en tout temps.",
    "help.tour.start": "▸ démarrer la visite",
    "help.memory": "Comment fonctionne la mémoire",
    "help.expand": "/expand — intelligence de domaine",
    "help.correct": "Corrections et rétractations",
    "help.connect": "Raccorder un modèle",

    "cmp.title": "FOSS ou SaaS",
    "cmp.title.note": "franc · côte à côte",
    "cmp.intro": "Le même moteur, deux façons de l'exploiter. Ceci est factuel, sans argumentaire de vente — choisissez ce qui vous convient.",
    "cmp.foss": "Auto-hébergé (FOSS)",
    "cmp.saas": "FaultLine SaaS",
    "cmp.row.license": "Licence",
    "cmp.row.seats": "Postes",
    "cmp.row.hosting": "Hébergement",
    "cmp.row.brain": "Cerveau LLM",
    "cmp.row.data": "Emplacement des données",
    "cmp.row.updates": "Mises à jour",
    "cmp.row.audit": "Journal d'audit / historique des événements",
    "cmp.row.multi": "Cloisonnement multilocataire",
    "cmp.row.serve": "Mode service / entraînement (agent en lecture seule)",
    "cmp.row.support": "Soutien technique",
    "cmp.row.auth": "Authentification du plan de contrôle",
    "cmp.row.price": "Prix",
    "cmp.cta": "Découvrir FaultLine SaaS → https://faultline.ca",

    "modal.cancel": "annuler",
    "modal.confirm": "confirmer",
    "reveal.copy": "⎘ copier dans le presse-papiers",
    "reveal.close": "Je l'ai conservé — fermer",
    "reveal.token.title": "Jeton de poste — affiché une seule fois",
    "reveal.token.warn": "Conservez-le maintenant — il ne sera plus jamais affiché. Il s'agit d'un jeton de POSTE\u00a0: utilisez-le comme jeton Bearer dans un client mono-utilisateur tel que Claude Desktop ou opencode. OpenWebUI n'utilise pas les jetons de poste — raccordez-le plutôt une seule fois avec la clé MCP de cette instance.",
    "reveal.mcpkey.title": "Nouvelle clé MCP — affichée une seule fois",
    "reveal.mcpkey.warn": "Conservez-la maintenant. Elle ne sera plus jamais affichée. L'ancienne clé cesse de fonctionner immédiatement.",

    "tour.welcome": "Bienvenue",
    "tour.firstrun": "Vous êtes nouveau ici? Faites une visite de 30 secondes du plan de contrôle.",
    "tour.dismiss": "plus tard",
    "tour.go": "▸ démarrer la visite",
    "tour.back": "‹ retour",
    "tour.skip": "passer",
    "tour.next": "suivant ›",
    "tour.done": "terminé",

    "tour.s1.title": "Barre d'état",
    "tour.s1.body": "État de l'instance, version et voyant d'état en direct. L'état est interrogé toutes les 10 s.",
    "tour.s2.title": "Six onglets",
    "tour.s2.body": "Tableau de bord, Postes et jetons, Cerveau LLM, OpenWebUI, Aide, et le comparatif FOSS ou SaaS.",
    "tour.s3.title": "Postes",
    "tour.s3.body": "Créez jusqu'à 5 postes — un par personne. Chaque jeton n'est affiché QU'UNE SEULE FOIS; conservez-le donc immédiatement. Un jeton de poste sert à un client mono-utilisateur (Claude Desktop, opencode). OpenWebUI fonctionne autrement\u00a0: raccordez-le une seule fois dans l'onglet OpenWebUI avec la clé MCP de l'instance, et chaque utilisateur dont la session est ouverte est cloisonné automatiquement.",
    "tour.s4.title": "Cerveau LLM",
    "tour.s4.body": "Pointez FaultLine vers un modèle que vous exécutez déjà (Ollama, LM Studio, OpenWebUI ou une API hébergée). Redémarrez le backend après l'enregistrement.",
    "tour.s5.title": "OpenWebUI",
    "tour.s5.body": "La méthode de raccordement prise en charge et votre URL d'outil MCP. Renouvelez la clé MCP en tout temps.",

    "footer": "FAULTLINE · MÉMOIRE VALIDÉE À L'ÉCRITURE · FOSS · AGPL V3",

    "err.network": "erreur de réseau — point de terminaison inaccessible",
    "err.401": "401 — jeton refusé. Ouvrez une session de nouveau.",
    "err.pending": "raccordement backend à venir",
    "err.pending.detail": "Ce point de terminaison de contrôle n'est pas encore actif sur le backend. L'interface s'affiche correctement; son raccordement par l'opérateur est distinct de cette console."
  };

  function t(key) {
    var v = STRINGS[key];
    return v == null ? key : v;
  }

  window.FL_STRINGS = STRINGS;
  window.t = t;
})();
