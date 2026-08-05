# FaultLine — prise en charge du français canadien (branche `ca-fr`)

> ## ⚠️ AVIS — VERSION EXPÉRIMENTALE ET NON OFFICIELLE
>
> Cette branche (**`ca-fr`**) est une version **expérimentale** et **en cours de développement** de la
> prise en charge du français canadien dans FaultLine. **Ce n'est pas une version officielle et elle
> n'est pas prête pour la production.** Elle est fournie **telle quelle** (*as is*), **sans aucune
> garantie** de fonctionnement, d'exactitude ou de continuité.
>
> - Des fonctionnalités peuvent être **incomplètes**, changer sans préavis ou **ne pas fonctionner**.
> - Elle n'a **pas encore été validée** de bout en bout sur des données réelles en français.
> - L'extraction **déterministe** (moteur *spine*) n'existe **qu'en anglais**; en français,
>   l'extraction passe par la voie **LLM** (*rewrite*), donc elle est **moins prévisible**.
> - **À utiliser à vos risques.** Pour la version **stable** (en anglais), utilisez la branche
>   `master` / `main`.
>
> *(Experimental, unofficial Canadian French branch — not production-ready, provided as-is with no
> warranty. Not yet validated on real French data. Deterministic extraction is English-only; French
> rides the LLM rewrite lane and is less predictable. Use at your own risk; for the stable English
> version use `master`/`main`.)*

---

## Ce que c'est

FaultLine est une **mémoire en graphe de connaissances** pour LLM, par locataire et validée à
l'écriture : elle extrait les entités et les relations des messages de l'utilisateur et les conserve
sous forme structurée. Le **cœur de la mémoire est indépendant de la langue** (identifiants, graphe,
valeurs, dates) : un fait stocké n'est pas « en anglais ».

## Pourquoi le français CANADIEN et non le français de France

Ce n'est pas une nuance cosmétique. Deux choses changent concrètement.

**1. La terminologie.** L'interface suit l'**Office québécois de la langue française** (OQLF) —
*Grand dictionnaire terminologique* et *Banque de dépannage linguistique*. Par exemple :
« ouvrir une session » / « fermer une session » plutôt que *se loguer* (signalé comme calque à éviter
dans la fiche GDT 2072335), « jeton » plutôt que *token*, « presse-papiers », « soutien technique »
plutôt que *support*, « forfait » plutôt que *abonnement*.

**2. La typographie.** Selon la BDL (*Espacement avant et après les signes de ponctuation et les
symboles*), en français d'ici on met une **espace insécable devant le deux-points**, mais **aucune
espace devant `;`, `!` et `?`** — l'Office « opte pour l'absence d'espace ». C'est précisément là que
l'usage québécois se distingue de l'usage français, qui insère une espace fine devant les quatre.
Les chaînes de `webui/strings.js` respectent cette règle; ce n'est pas un oubli. L'espace insécable
y est écrite comme l'échappement `\u00a0` plutôt qu'en octet brut, pour qu'elle reste visible
dans le code source. Dans `quickstart.py`, qui écrit dans un terminal, on emploie une espace ordinaire devant
le deux-points : un U+00A0 s'affiche mal dans certaines consoles Windows, et un assistant
d'installation illisible serait un pire défaut qu'une espace sécable.

**3. La collation de la base de données.** `fr-CA` et `fr-FR` ne sont **pas** la même collation ICU.
Mesuré sur `postgres:16-alpine`, en triant `('cote','coté','côte','côté')` :

| collation ICU | ordre obtenu |
|---|---|
| `fr-CA` | `cote < côte < coté < côté` |
| `fr-FR` | `cote < coté < côte < côté` |

CLDR conserve la comparaison traditionnelle des accents **de droite à gauche** pour le français de
France et **pas** pour le français canadien. Cette branche utilise donc `--icu-locale=fr-CA`.

## Comment ça fonctionne en français (sur cette branche)

- L'**extraction** passe par la voie **LLM** (*rewrite*), qui comprend le français nativement.
- Le typage des entités utilise un modèle **GLiNER multilingue** (`gliner_multi`).
- Les dates sont traitées par `dateparser`, qui prend en charge le français.
- Le **moteur déterministe** (*spine*) reste anglais : des constructions comme *« I am 34 years
  old »* et *« j'ai 34 ans »* n'ont pas la même syntaxe, donc les règles anglaises ne s'appliquent
  pas au français.

## Langue et base de données (à lire avant l'installation)

La langue se choisit **au tout début** de `quickstart.py`, avant quoi que ce soit d'autre, et ce choix
fixe la **collation de PostgreSQL** au moyen d'ICU (`fr-CA`).

> ⚠️ **La collation est fixée à `initdb` et devient ensuite IMMUABLE.** Elle ne peut pas être changée
> sans une opération de vidage/restauration (*dump/restore*). C'est pourquoi la décision se prend à
> l'installation : une installation française qui atterrit sur une base créée avec la collation
> anglaise par défaut traînerait un **ordre de tri incorrect pour toujours**.

Si vous réutilisez un volume Postgres existant dont la collation ne correspond pas, le démarrage
**s'arrête volontairement** et vous présente les options réelles (recréer le volume,
vidage/restauration, ou accepter l'écart avec `FAULTLINE_ALLOW_DB_LOCALE_MISMATCH=true`). Rien n'est
modifié ni supprimé.

On utilise **ICU** plutôt qu'une locale du système parce que l'image postgres **ne contient aucune
locale `fr_*`**; `--locale=fr_CA.utf8` échouerait donc d'emblée.

## État et détails techniques

Cette branche `ca-fr` est **expérimentale** : l'extraction en français s'appuie sur la voie LLM
(*rewrite*), tandis que le moteur déterministe (*spine*) demeure anglais. Le modèle spaCy passe à
`fr_core_news_sm` pour que l'analyseur corresponde au texte — mais **ce n'est pas une garantie de
parité** : les chaînes du *spine* sont construites et validées contre l'anglais, et elles n'ont
**pas été mesurées** en français. Des fonctionnalités peuvent être incomplètes ou changer. Pour
contribuer ou signaler un problème, gardez l'avis ci-dessus à l'esprit.

## Ce qui n'est pas traduit

Les noms propres (FaultLine, MCP, PostgreSQL, Qdrant, OpenWebUI, Docker), le nom de la licence
« GNU Affero General Public License v3 », les identifiants de code (`user_id`, clés `.env`, chemins
de fichiers, en-têtes HTTP), et les chemins de menus d'autres logiciels (par exemple
`Chat Controls → Advanced Params → Function Calling → Legacy` dans OpenWebUI), parce qu'il faut
pouvoir les retrouver sur un écran en anglais.
