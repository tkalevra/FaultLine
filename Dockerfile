# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip

COPY pyproject.toml .
# Install base + api dependencies (includes redis for re_embedder)
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir ".[api]"

# Pre-download the GLiNER2 model while gliner2 is on PATH via the venv
RUN python -c "from gliner2 import GLiNER2; GLiNER2.from_pretrained('fastino/gliner2-base-v1')"

# Pre-download the fastembed model for local CPU embeddings (avoids hitting external LLM).
# Model id is PARAMETRIZED (no hardcoded literal) — config owns it; ENV propagates to runtime
# so the baked cache matches what _get_local_embedder() loads.
ARG FASTEMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
ENV FASTEMBED_MODEL=${FASTEMBED_MODEL}
ENV FASTEMBED_CACHE_PATH=/root/.cache/fastembed
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

ENTRYPOINT ["./docker-entrypoint.sh"]
