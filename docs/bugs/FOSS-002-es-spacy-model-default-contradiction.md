# FOSS-002 — es wizard writes SPACY_MODEL=es_core_news_sm while the es branch's own measured default is es_core_news_md (weaker parses shipped by default)

- **Branch matrix:** es 761cf914 reproduced · it 9f2724ac N/A (it is internally consistent: sm everywhere) · main c0fe9322 N/A (en_core_web_sm consistent)
- **Severity:** Medium-High (fresh Spanish installs get measurably weaker linguistic capture than the branch's own documentation says is the default)
- **Discovered:** 2026-10-17, fresh-install gauntlet, es first

## Symptom

Install the es branch through the documented wizard. The generated `.env` contains:

    SPACY_MODEL=es_core_news_sm

…but the SAME file's own comment block says:

    [es branch] Default es_core_news_md — the model MUST match the install language.
    ... sm remains a valid smaller-image choice (weaker parses: sentence-initial 1sg preterite
    verbs mis-tag as PROPN, possessive DETs mis-parse as flat names) — md is the measured default.

And `git show foss/es:.env.example` pins `SPACY_MODEL=es_core_news_md`, the compose build arg default is the same md value, and the Dockerfile ARG is `es_core_news_md`. **Only the wizard's language table (`quickstart.py::_LANGUAGES["es"]["spacy_model"]`) disagrees** — so a wizard install bakes and runs the weaker sm model, while every hand-configured path uses md.

Measured in the built image: `docker history faultline-es:latest` shows `ENV SPACY_MODEL=es_core_news_sm`.

## Root cause

`quickstart.py`'s `_LANGUAGES` table carries `"spacy_model": "es_core_news_sm"` for es (it carries `it_core_news_sm` for it, where that IS the branch default — the table is only wrong for es). The wizard's value flows into `.env`, compose resolves the build arg from it, and the image bakes sm. The branch's measured default (md) never reaches a wizard install.

Consequence visible in this very gauntlet: the fresh es install refused the edge (user, owns, servidor) because the sm-model parse/typing produced Person for "servidor", and the possessive chains the branch is proud of run on exactly the model the branch itself calls weaker for those shapes.

## Reproduce

1. Fresh clone, ./setup.sh, choose Espanol, complete the wizard.
2. grep ^SPACY_MODEL .env  -> es_core_news_sm
3. git show foss/es:.env.example | grep ^SPACY_MODEL -> es_core_news_md. Contradiction.

## Fix direction (gauntlet work order)

1. Owner decision recorded IN the fix: either (a) change _LANGUAGES["es"]["spacy_model"] to es_core_news_md so the wizard matches the branch's measured default (preferred — the .env comment documents sm's specific failure modes), or (b) re-measure and flip the whole es branch to sm consistently. Do NOT silently pick one; state the choice and the measurement in the commit message.
2. Add a parity pin: parse quickstart.py _LANGUAGES es entry and assert it equals the .env.example / Dockerfile ARG / compose default value (single source of truth: read the files, fail on mismatch). Same pin pattern for it and main.
3. **You are NOT your own critic.** When green, request a clean critic agent; the critic re-derives the default from the four files independently and runs semgrep + codeql over the diff.
