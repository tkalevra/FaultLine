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

# Pre-download the fastembed nomic model for local CPU embeddings (avoids hitting external LLM)
ENV FASTEMBED_CACHE_PATH=/root/.cache/fastembed
RUN python -c "import os; os.environ['FASTEMBED_CACHE_PATH']='/root/.cache/fastembed'; from fastembed import TextEmbedding; TextEmbedding('nomic-ai/nomic-embed-text-v1.5')"

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
# Model is pre-cached in the image — prevent HuggingFace update checks at runtime.
# Without this, from_pretrained() blocks startup for ~3 minutes in restricted networks.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
