# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip

# Install CPU-only PyTorch FIRST, before anything that pulls it transitively.
# GLiNER2 -> gliner/peft depend on torch; the default Linux wheel drags in
# ~5 GB of nvidia-cuda-* packages we never use — extraction runs on CPU.
# Pre-seeding the CPU build means the dependency resolver below is satisfied
# and never fetches the CUDA wheels.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml .
# Install base + api dependencies (includes redis for re_embedder)
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir ".[api]"

# Pre-download the GLiNER2 model while gliner2 is on PATH via the venv
RUN python -c "from gliner2 import GLiNER2; GLiNER2.from_pretrained('fastino/gliner2-base-v1')"

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

RUN chmod +x docker-entrypoint.sh

ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
