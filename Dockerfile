# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[api]"

# Pre-download the GliNER model while gliner is on PATH via the venv
RUN python -c "from gliner import GLiNER; GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"

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
COPY docker-entrypoint.sh ./

RUN chmod +x docker-entrypoint.sh

ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8001

ENTRYPOINT ["./docker-entrypoint.sh"]
