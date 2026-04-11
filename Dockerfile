# ─────────────────────────────────────────────────────────────
#  Family Health — Dockerfile
#  Multi-arch: linux/amd64 + linux/arm64
# ─────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Minimal system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
        # OpenCV runtime
        libgl1 \
        libglib2.0-0 \
        # pdf2image needs poppler
        poppler-utils \
        # healthcheck
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Install Python deps — separate layer so code changes don't re-install
COPY app/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    # Remove test files, __pycache__, .pyc from site-packages to trim size
    && find /usr/local/lib/python3.11/site-packages -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.11/site-packages -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.11/site-packages -name "*.pyc" -delete \
    && find /usr/local/lib/python3.11/site-packages -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Copy application code
COPY app/ .

# Persistent data volume
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "2", "--log-level", "info"]