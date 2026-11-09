# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip

COPY pyproject.toml .

# ── CPU-ONLY TORCH — install FIRST, BEFORE the project deps ──────────────────
# gliner2[local] declares torch, but the DEFAULT PyPI torch wheel BUNDLES THE FULL CUDA
# RUNTIME (nvidia-cudnn, cublas, nccl, ...): a ~526 MB download and ~2.5 GB installed of
# GPU libraries that a CPU-only deployment can never use. Installing the CPU-only wheel
# FIRST satisfies the dependency, so the later `pip install -e .` resolves torch as
# already-present and never drags in the CUDA build. Do NOT remove this step: without it
# a dependency bump can silently leave the builder with NO torch at all, and the GLiNER2
# pre-download below dies with `ModuleNotFoundError: No module named 'torch'` — exactly
# how fresh-clone builds broke on every branch before this fix.
# Running a GPU host? Override the index: --build-arg TORCH_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX_URL}

# Install base + api dependencies (includes redis for re_embedder)
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir ".[api]"

# Fail LOUD if a CUDA build sneaks back in: a GPU torch on a CPU-only host is ~2 GB of dead
# weight, and silently tolerating it is how it got here in the first place.
RUN python -c "import torch, sys; v = torch.__version__; sys.exit('FATAL: CUDA torch installed on a CPU-only image (' + v + ')') if '+cu' in v or torch.version.cuda else print('CPU-only torch OK', v)"

# Pre-download the GLiNER2 model while gliner2 is on PATH via the venv.
# [es branch] MULTILINGUAL weights — `gliner2-base-v1` is tagged `en` only on HuggingFace, so it
# typed Spanish entities with English weights. `gliner2-multi-v1` (Apache-2.0, en/es/fr/de/it/pt)
# is the same GLiNER2 API and the same load call. This MUST be baked: the runtime stage sets
# HF_HUB_OFFLINE=1, so an unbaked model cannot be fetched at startup — it would just fail to load.
ARG GLINER_MODEL=fastino/gliner2-multi-v1
ENV GLINER_MODEL=${GLINER_MODEL}
RUN python -c "import os; from gliner2 import GLiNER2; GLiNER2.from_pretrained(os.environ['GLINER_MODEL']); print('GLiNER2', os.environ['GLINER_MODEL'], 'baked OK')"

# Pre-download the fastembed model for local CPU embeddings (avoids hitting external LLM).
# Model id is PARAMETRIZED (no hardcoded literal) — config owns it; ENV propagates to runtime
# so the baked cache matches what _get_local_embedder() loads.
ARG FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
ENV FASTEMBED_MODEL=${FASTEMBED_MODEL}
ENV FASTEMBED_CACHE_PATH=/root/.cache/fastembed
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['FASTEMBED_MODEL'])"

# BAKE the spaCy linguistic-layer model into the image (NO runtime `spacy download`).
# The model name is PARAMETRIZED (no hardcoded literal in the RUN line) — config owns it.
# [es branch] Default `es_core_news_sm` — the model MUST match the install language. The English
# model does not degrade on Spanish, it CORRUPTS: no verb/subject/object arcs, and no negation
# dependency at all, so "No uso el puerto 9004" stores as its affirmative. linguistics.py now
# refuses to load a model whose nlp.lang disagrees with FAULTLINE_LANGUAGE.
# The spine dependency chains were originally tuned against en sm's parse shapes and are NOT yet
# re-tuned for Spanish syntax — Spanish capture trails English until they are.
# (A larger model shifts the inchoative/compound-anchor arcs again, breaking the chains — opt-in
# only, NOT the default; re-tune the chains before switching.) All shipped models
# are PINNED, py3-none-any (Python-version-independent) GitHub-release wheels → install as normal
# packages into /venv, carried over by the venv COPY below. spaCy itself comes from pyproject
# (spacy>=3.7,<3.9 → 3.8.x). If this model is ever missing/unset, linguistics.py no-ops.
# [es branch] Default es_core_news_sm — MUST match the install language. The English model
# over Spanish text yields a run-on PROPN chunk with no verb/subject/object and no negation
# dep, i.e. silent corruption rather than degraded capture. linguistics.py now refuses to
# load a model whose nlp.lang disagrees with FAULTLINE_LANGUAGE.
# Override at build with `--build-arg SPACY_MODEL=es_core_news_md` (re-tune chains first).
ARG SPACY_MODEL=es_core_news_sm
ARG SPACY_MODEL_VERSION=3.8.0
ENV SPACY_MODEL=${SPACY_MODEL}
RUN pip install --no-cache-dir \
    "https://github.com/explosion/spacy-models/releases/download/${SPACY_MODEL}-${SPACY_MODEL_VERSION}/${SPACY_MODEL}-${SPACY_MODEL_VERSION}-py3-none-any.whl"
# Fail the build loudly if the linguistic-layer model isn't loadable (catch a bad bake at build).
RUN python -c "import spacy, os; m=os.environ['SPACY_MODEL']; spacy.load(m, disable=['ner']); print('spaCy', m, 'baked OK')"
# Fail the build loudly if dateparser (deterministic temporal-ingest date normalizer) is missing.
# It installs from pyproject (dateparser>=1.2,<2.0); this asserts the wheel + its data are present.
RUN python -c "import dateparser; print('dateparser baked OK', dateparser.parse('March 15th') is not None)"

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /venv /venv
# Copy cached HuggingFace model weights
COPY --from=builder /root/.cache /root/.cache

COPY src/       ./src/
COPY migrations/ ./migrations/
COPY tools/     ./tools/
COPY webui/     ./webui/
COPY openwebui/ ./openwebui/
COPY docker-entrypoint.sh ./

RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV FASTEMBED_CACHE_PATH=/root/.cache/fastembed
# Model identity MUST reach RUNTIME. This is a multi-stage build: the builder-stage ENVs
# (SPACY_MODEL/FASTEMBED_MODEL above) do NOT carry into this runtime stage, so without
# re-declaring them here the code reads None → the load-bearing spaCy spine silently no-ops
# (caused a 9/10→2/10 regression). Re-declare the config-layer ARG defaults + propagate as ENV
# so linguistics.py / embedder.py / llm_output_validator.py (which read these env vars — NO code
# literals) resolve at runtime. Override via --build-arg or the compose `environment:` block.
ARG SPACY_MODEL=es_core_news_sm
ENV SPACY_MODEL=${SPACY_MODEL}
# Same reasoning for the GLiNER2 entity-typing weights (main.py reads GLINER_MODEL). If this is
# not re-declared here, runtime falls back to the code default — which must therefore stay in
# lockstep with the ARG in the builder stage, or the baked model and the requested model differ
# and from_pretrained() fails closed under HF_HUB_OFFLINE=1.
ARG GLINER_MODEL=fastino/gliner2-multi-v1
ENV GLINER_MODEL=${GLINER_MODEL}
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

ENTRYPOINT ["./docker-entrypoint.sh"]
