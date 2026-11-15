# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip

COPY pyproject.toml .

# ── CPU-ONLY TORCH (do not remove — this is ~2GB of image) ────────────────────────────────
# GLiNER2 depends on torch. The DEFAULT PyPI torch wheel BUNDLES THE FULL CUDA RUNTIME
# (nvidia-cudnn, cublas, nccl, ...): a 526 MB download and ~2.5 GB installed of GPU libraries
# that this deployment can NEVER use — the canonical deployment is a CPU-ONLY host by
# design (docker-compose.yml ships no GPU service). It is pure dead weight in every image,
# every layer, every pull, every deploy.
#
# Installing the CPU-only wheel FIRST satisfies the dependency, so the later `pip install -e .`
# resolves torch as already-present and never drags in the CUDA build.
# If a GPU box is ever added, override this ARG with the default index.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX_URL}

# Install base + api dependencies (includes redis for re_embedder)
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir ".[api]"

# Fail LOUD if a CUDA build sneaks back in: a GPU torch on a CPU-only host is 2GB of dead
# weight, and silently tolerating it is how it got here in the first place.
RUN python -c "import torch, sys; v=torch.__version__; print('torch', v);     sys.exit('FATAL: CUDA torch installed on a CPU-only image (' + v + ')') if '+cu' in v or torch.version.cuda else print('CPU-only torch OK')"

# ── BAKED MODEL CACHE ROOT — declared ONCE per stage, every loader reads the env ───────────
# The runtime process is NON-ROOT (`USER faultline`, runtime stage). Every model cache must
# therefore live under ONE neutral, world-readable root that is NOT keyed on $HOME, because:
#   * `/root` is `drwx------` on python:3.11-slim — a non-root user cannot even TRAVERSE it, so
#     a cache baked at `/root/.cache/...` is EACCES for the runtime user regardless of the modes
#     of anything beneath it (a `chmod -R` on `/root/.cache` never touches `/root` itself).
#   * huggingface_hub resolves its cache from `HF_HOME`, which "Defaults to ~/.cache/huggingface
#     unless XDG_CACHE_HOME is set"; the hub cache is `HF_HUB_CACHE`, "Defaults to $HF_HOME/hub"
#     (https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables).
#     `adduser --system --no-create-home` gives the runtime user HOME=/nonexistent, so an
#     $HOME-keyed cache silently resolves to a DIFFERENT directory for the runtime user, and
#     `HF_HUB_OFFLINE=1` then refuses to download ("If no cache file is detected, an error is
#     raised") — the GLiNER2 load fails at startup, not at build.
#   * Fixing modes AFTER the COPY with `chmod -R` is an overlayfs COPY-UP: "all OverlayFS copy_up
#     operations copy the entire file, even if the file is large and only a small part of it's
#     being modified" (https://docs.docker.com/engine/storage/drivers/overlayfs-driver/) — the
#     whole ~1.3 GB model cache is duplicated into a new layer. Permissions are therefore fixed
#     HERE, in the builder, BEFORE the single runtime COPY; the runtime stage never chmods.
# The root is an ARG→ENV so it is declared once and every consumer reads the env (embedder.py
# reads FASTEMBED_CACHE_PATH; huggingface_hub/transformers/gliner2 read HF_HOME; nltk reads
# NLTK_DATA). It is RE-DECLARED in the runtime stage — builder ENVs do NOT carry (see the
# SPACY_MODEL note below) — and both stages MUST agree or the baked cache is orphaned.
ARG FL_CACHE_ROOT=/opt/faultline/cache
ENV FL_CACHE_ROOT=${FL_CACHE_ROOT}
ENV HF_HOME=${FL_CACHE_ROOT}/huggingface
ENV FASTEMBED_CACHE_PATH=${FL_CACHE_ROOT}/fastembed
ENV NLTK_DATA=${FL_CACHE_ROOT}/nltk_data
RUN mkdir -p "${HF_HOME}" "${FASTEMBED_CACHE_PATH}" "${NLTK_DATA}"

# Pre-download the GLiNER2 model while gliner2 is on PATH via the venv. The hub cache lands under
# $HF_HOME/hub (see above) — the SAME path the runtime stage points HF_HOME at.
RUN python -c "from gliner2 import GLiNER2; GLiNER2.from_pretrained('fastino/gliner2-base-v1')"

# Pre-download the fastembed model for local CPU embeddings (avoids hitting external LLM).
# Model id is PARAMETRIZED (no hardcoded literal) — config owns it; ENV propagates to runtime
# so the baked cache matches what _get_local_embedder() loads. Cache dir = FASTEMBED_CACHE_PATH
# (declared above, under FL_CACHE_ROOT).
ARG FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
ENV FASTEMBED_MODEL=${FASTEMBED_MODEL}
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['FASTEMBED_MODEL'])"

# BAKE the spaCy linguistic-layer model into the image (NO runtime `spacy download`).
# The model name is PARAMETRIZED (no hardcoded literal in the RUN line) — config owns it.
# Default `en_core_web_sm` — the VALIDATED model the spine dependency chains are tuned to.
# (`en_core_web_md` was A/B-tested and shifts the inchoative/compound-anchor arcs, breaking the
# chains — opt-in only, NOT the default; re-tune the chains before switching.) All shipped models
# are PINNED, py3-none-any (Python-version-independent) GitHub-release wheels → install as normal
# packages into /venv, carried over by the venv COPY below. spaCy itself comes from pyproject
# (spacy>=3.7,<3.9 → 3.8.x). If this model is ever missing/unset, linguistics.py no-ops.
# Override at build with `--build-arg SPACY_MODEL=en_core_web_md` (re-tune chains first).
ARG SPACY_MODEL=en_core_web_sm
ARG SPACY_MODEL_VERSION=3.8.0
ENV SPACY_MODEL=${SPACY_MODEL}
RUN pip install --no-cache-dir \
    "https://github.com/explosion/spacy-models/releases/download/${SPACY_MODEL}-${SPACY_MODEL_VERSION}/${SPACY_MODEL}-${SPACY_MODEL_VERSION}-py3-none-any.whl"
# Fail the build loudly if the linguistic-layer model isn't loadable (catch a bad bake at build).
RUN python -c "import spacy, os; m=os.environ['SPACY_MODEL']; spacy.load(m, disable=['ner']); print('spaCy', m, 'baked OK')"

# ── DOC-LANE SECOND MODEL — ADDITIVE bake (the parse-model split) ─────────────────
# en_core_web_trf is the DOCUMENT-lane parse-model candidate. ADDITIVE BY CONTRACT: the
# sm bake above is UNTOUCHED and stays the conversational default (SPACY_MODEL); trf is
# an EXTRA wheel so a deployment MAY split the parse model per lane. trf is a
# transformer pipeline: it needs spacy_transformers + curated-transformers + torch.
# CPU-ONLY torch is installed FIRST in this builder (ARG TORCH_INDEX_URL above) and
# gliner2 already pulled transformers, so trf's dependency resolution reuses them and
# never drags in the CUDA build (same contract as the GLiNER2/torch note at the top).
#
# RUNTIME ENABLEMENT IS AN OPERATOR CHOICE, DELIBERATELY NOT AN IMAGE DEFAULT: this block
# bakes the wheel but does NOT export DOC_SPACY_MODEL as a runtime ENV. The code default
# is disabled (empty/absent DOC_SPACY_MODEL = the document lane parses with SPACY_MODEL
# byte-identically). Rollout: set DOC_SPACY_MODEL=en_core_web_trf in compose/.env.
# Rollback: unset it — the wheel then sits unused in the image.
#
# A/B DISCIPLINE (measured 2026-11-06 on the private line's benchie A/B harness):
# en_core_web_md SHIFTED ARCS and broke the spine chains; trf is a DIFFERENT tagger/
# parser stack and MUST be benched the same way before the lever is pulled in prod — the
# local A/B (benchie + chain audit + litmus junk rate) is the gate, not the bake.
ARG DOC_SPACY_MODEL_BAKE=en_core_web_trf
ARG DOC_SPACY_MODEL_VERSION=3.8.0
# The guard IS the build: pip exits nonzero on a bad/missing wheel and this RUN has no
# fallback clause — a failed install fails the build loudly, it never silently continues
# with only sm baked.
RUN pip install --no-cache-dir \
    "spacy-transformers" \
    "https://github.com/explosion/spacy-models/releases/download/${DOC_SPACY_MODEL_BAKE}-${DOC_SPACY_MODEL_VERSION}/${DOC_SPACY_MODEL_BAKE}-${DOC_SPACY_MODEL_VERSION}-py3-none-any.whl" \
    || { echo "FATAL: the doc-lane spaCy model wheel failed to install (${DOC_SPACY_MODEL_BAKE}-${DOC_SPACY_MODEL_VERSION}) — the parse-model split cannot ship half-baked" >&2; exit 1; }
# Fail the build loudly if the doc-lane model is present but NOT LOADABLE (a wheel that
# installs but cannot build its transformer pipeline — mismatched spacy_transformers /
# torch — must stop the build, never silently degrade the doc lane back to sm at runtime).
RUN DOC_MODEL_NAME=${DOC_SPACY_MODEL_BAKE} python -c "import spacy, spacy_transformers, torch, os; m=os.environ['DOC_MODEL_NAME']; nlp=spacy.load(m, disable=['ner']); d=nlp('Photosynthesis converts carbon dioxide and water into glucose.'); assert any(t.pos_=='VERB' for t in d), 'doc-lane model produced no VERB — pipeline broken'; print('spaCy doc-lane model', m, 'baked OK (torch', torch.__version__ + ')')"
# Fail the build loudly if dateparser (deterministic temporal-ingest date normalizer) is missing.
# It installs from pyproject (dateparser>=1.2,<2.0); this asserts the wheel + its data are present.
RUN python -c "import dateparser; print('dateparser baked OK', dateparser.parse('March 15th') is not None)"
# BAKE the WordNet corpus into the image (NO runtime download — offline, CPU-cloud-safe). This is
# the deterministic hypernym source for the SYNC L4 is-a ladder (src/api/wordnet_ladder.py). nltk
# itself installs from pyproject (nltk>=3.8,<4.0); the CORPUS is separate data, downloaded to a
# baked NLTK_DATA path (declared above, under FL_CACHE_ROOT). `wordnet` = the noun is-a hierarchy;
# `omw-1.4` = the Open Multilingual WordNet dependency modern nltk resolves lemmas through. If absent
# at runtime, wordnet_ladder.py no-ops (WordNet miss → async LLM climb), so this is a capability
# bake, not a hard startup dep.
RUN python -m nltk.downloader -d ${NLTK_DATA} wordnet omw-1.4
# nltk's downloader writes the corpus zips through a 0600 temp file + os.replace, so they land
# `-rw-------` (MEASURED 2026-09-14: wordnet.zip / omw-1.4.zip both 0600 — under a non-root USER the
# WordNet ladder silently no-opped on the previous image). Open them to the world HERE, in the
# builder, BEFORE the runtime COPY: a builder-stage layer is never shipped, so this is not a
# copy-up in the runtime image. Files only, explicit, non-recursive-flag — the assertion below is
# the safety net for anything else a future bake leaves closed.
RUN find "${NLTK_DATA}" -type f ! -perm -o=r -exec chmod a+r {} +
# Fail the build loudly if the WordNet corpus isn't loadable (catch a bad bake at build).
RUN python -c "from nltk.corpus import wordnet as wn; wn.ensure_loaded(); assert wn.synsets('dermatologist','n'), 'WordNet corpus missing'; print('WordNet corpus baked OK')"

# Fail the build loudly if ANY baked cache entry is not world-readable (files) / traversable
# (dirs). The runtime user is not root and the runtime stage does NOT chmod (copy-up, see above),
# so the modes the bake produced are the modes the runtime user gets. Catch a bad bake here.
RUN bad=$(find "${FL_CACHE_ROOT}" \( -type d ! -perm -o=rx \) -o \( -type f ! -perm -o=r \) | head -20); \
    if [ -n "$bad" ]; then echo "FATAL: baked cache entries not world-readable under ${FL_CACHE_ROOT}:"; echo "$bad"; exit 1; fi; \
    echo "baked cache world-readable OK: $(du -sh ${FL_CACHE_ROOT} | cut -f1)"

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Create the dedicated non-root runtime user FIRST — before any COPY — so no layer below is ever
# re-written to fix ownership/modes (every such fix is an overlayfs copy-up of what it touches).
# EXPLICIT UID/GID: "Users and groups in an image are assigned a non-deterministic UID/GID ... if
# it's critical, you should assign an explicit UID/GID" — and "Avoid installing or using sudo"
# (https://docs.docker.com/build/building/best-practices/#user). Declared once as ARGs so a
# bind-mount host can match them. No home dir (-d /nonexistent, no -m): the runtime NEVER needs
# $HOME — every cache is pinned to FL_CACHE_ROOT below. --no-log-init avoids the lastlog sparse
# file a large UID would otherwise create.
ARG FL_UID=10001
ARG FL_GID=10001
RUN groupadd -r -g "${FL_GID}" faultline \
    && useradd --no-log-init -r -u "${FL_UID}" -g faultline -d /nonexistent -s /usr/sbin/nologin faultline

WORKDIR /app

# ONE neutral, world-readable cache root — MUST match the builder's declaration (builder ENVs do
# not carry into this stage; a mismatch orphans the baked cache and every loader falls to its
# $HOME default, which for the runtime user is /nonexistent). Declared before the COPY so the
# COPY destination and the env agree by construction.
ARG FL_CACHE_ROOT=/opt/faultline/cache
ENV FL_CACHE_ROOT=${FL_CACHE_ROOT}
# huggingface_hub / transformers / gliner2: hub cache = $HF_HOME/hub (HF_HUB_CACHE default).
ENV HF_HOME=${FL_CACHE_ROOT}/huggingface
# fastembed (embedder.py::_get_local_embedder reads this env and passes it as cache_dir).
ENV FASTEMBED_CACHE_PATH=${FL_CACHE_ROOT}/fastembed
# nltk (wordnet_ladder.py): absent → wordnet_ladder no-ops (async LLM climb), never a hard crash.
ENV NLTK_DATA=${FL_CACHE_ROOT}/nltk_data

COPY --from=builder /venv /venv
# The ENTIRE baked model cache (HF hub: GLiNER2; fastembed: nomic; nltk: WordNet) in ONE COPY,
# modes as the builder produced them (root-owned 755/644 — the runtime user READS, never writes).
# NO `RUN chmod` after this line: that is an overlayfs copy-up of ~1.3 GB (builder-stage note).
# `COPY --chown` / `--chmod` (https://docs.docker.com/reference/dockerfile/#copy) are the
# in-layer alternatives and are deliberately NOT used here: --chown would hand the runtime user
# WRITE on the baked models (they are immutable by design), and --chmod is unnecessary because
# the builder already asserted every entry is world-readable. Root-owned + o=rX is the target.
COPY --from=builder ${FL_CACHE_ROOT} ${FL_CACHE_ROOT}

COPY src/       ./src/
COPY migrations/ ./migrations/
COPY tools/     ./tools/
COPY docker-entrypoint.sh ./

RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

# Fail the build loudly if the runtime user could not READ what it needs: /app (code, migrations,
# entrypoint) and the baked cache. A read-only `find` copies nothing up; a closed entry here would
# otherwise surface at runtime as EACCES inside a loader (the exact wound this stage was rebuilt
# to close). The runtime stage never chmods — a hit here means fix the bake or the checkout.
RUN bad=$(find /app "${FL_CACHE_ROOT}" \( -type d ! -perm -o=rx \) -o \( -type f ! -perm -o=r \) | head -20); \
    if [ -n "$bad" ]; then echo "FATAL: entries not readable by the runtime user:"; echo "$bad"; exit 1; fi; \
    echo "runtime tree readable by non-root OK"

ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
# Model identity MUST reach RUNTIME. This is a multi-stage build: the builder-stage ENVs
# (SPACY_MODEL/FASTEMBED_MODEL above) do NOT carry into this runtime stage, so without
# re-declaring them here the code reads None → the load-bearing spaCy spine silently no-ops
# (caused a 9/10→2/10 regression). Re-declare the config-layer ARG defaults + propagate as ENV
# so linguistics.py / embedder.py / llm_output_validator.py (which read these env vars — NO code
# literals) resolve at runtime. Override via --build-arg or the compose `environment:` block.
ARG SPACY_MODEL=en_core_web_sm
ENV SPACY_MODEL=${SPACY_MODEL}
ARG FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
ENV FASTEMBED_MODEL=${FASTEMBED_MODEL}
ARG EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
ARG EMBEDDING_MODEL_VERSION=nomic-v1.5
ENV EMBEDDING_MODEL_VERSION=${EMBEDDING_MODEL_VERSION}
# Model is pre-cached in the image — prevent HuggingFace update checks at runtime.
# Without this, from_pretrained() blocks startup for ~3 minutes in restricted networks.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

EXPOSE 8000

# Run as the dedicated non-root user (created above, before the COPYs). /app and FL_CACHE_ROOT are
# root-owned and READ-ONLY to this user by construction — the app never writes to either at
# runtime; the only runtime writes are /tmp (see DEV/review/gauntlet/dockerfile-nonroot-cache).
USER faultline

ENTRYPOINT ["./docker-entrypoint.sh"]
