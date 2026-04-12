# ─────────────────────────────────────────────────────────────
#  Family Health — Dockerfile
#  Multi-arch: linux/amd64 + linux/arm64
# ─────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Build-time version args — injected by GitHub Actions
ARG APP_VERSION=dev
ARG APP_BUILD=local

# Bake version into image as env vars readable at runtime
ENV APP_VERSION=${APP_VERSION}
ENV APP_BUILD=${APP_BUILD}

# Minimal system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && find /usr/local/lib/python3.11/site-packages -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.11/site-packages -type d -name "test"  -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.11/site-packages -name "*.pyc" -delete \
    && find /usr/local/lib/python3.11/site-packages -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

COPY app/ .

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "2", "--log-level", "info"]