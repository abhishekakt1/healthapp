# ─────────────────────────────────────────────────────────────
#  Family Health — Dockerfile
#  Multi-arch: linux/amd64 + linux/arm64
# ─────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps for Pillow, OpenCV, ReportLab
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsm6 libxrender1 libxext6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache friendly)
COPY healthapp/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY healthapp/app/ .

# Data volume — DB and uploads persist here
VOLUME ["/data"]

EXPOSE 8080

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "2", "--log-level", "info"]