# FOIA Redaction Tool — backend image.
#
# Per build plan: a backend container arrives in Phase 3. The image bakes in
# the optional binary deps (tesseract, LibreOffice) that Phase 2 degrades
# gracefully without, so inside the container they are always present.
#
# Build:
#   docker build -t foia-backend -f backend/Dockerfile backend
#
# Run (mount data + config from host):
#   docker run --rm -v "$PWD/data:/data" -v "$PWD/backend/config:/config:ro" \
#     -e FOIA_DB_PATH=/data/foia.db \
#     -e FOIA_ATTACHMENT_DIR=/data/attachments \
#     -e FOIA_CONFIG_FILE=/config/district.yaml \
#     foia-backend \
#     python ingest.py --file /data/incoming.mbox

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# System deps:
#   tesseract-ocr       — OCR binary for Phase 2
#   tesseract-ocr-eng   — English language pack
#   libreoffice         — Office → PDF conversion
#   poppler-utils       — PDF utilities (used indirectly by some paths)
# Use `--no-install-recommends` to keep the image lean.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      libreoffice \
      libreoffice-core \
      libreoffice-common \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first to keep this layer cacheable.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy the application.
COPY foia/ /app/foia/
COPY scripts/ /app/scripts/
COPY ingest.py extract.py detect.py evaluate.py resolve.py redact.py export.py qa.py serve.py /app/
COPY pyproject.toml /app/pyproject.toml

# Container defaults — override with -e on `docker run`.
ENV FOIA_DB_PATH=/data/foia.db \
    FOIA_ATTACHMENT_DIR=/data/attachments \
    FOIA_EXPORT_DIR=/data/exports \
    FOIA_CONFIG_FILE=/config/district.yaml \
    FOIA_OCR_ENABLED=true \
    FOIA_OFFICE_ENABLED=true \
    FOIA_LIBREOFFICE_CMD=soffice

VOLUME ["/data", "/config"]
EXPOSE 8000

# Default behaviour: boot the Phase 5 API server on :8000. Override with
# `docker run foia-backend python ingest.py --file /data/incoming.mbox`
# for one-shot CLI work.
CMD ["python", "serve.py", "--host", "0.0.0.0", "--port", "8000"]

# Tag the image with the highest implemented phase so consumers can branch
# on it. Bump in lockstep with foia.api version.
LABEL org.foia-redaction.phase="10"
