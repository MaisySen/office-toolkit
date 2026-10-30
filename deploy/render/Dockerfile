# Office Toolkit on a free Docker host (Render free web service).
# Same runtime as the root Dockerfile: LibreOffice and Tesseract, no
# Ghostscript. Differences: runs as uid 1000, listens on $PORT, restores
# and snapshots the auth database (deploy/render/persist.py) because the
# free host disk is not persistent, and refuses requests that did not
# come through the Cloudflare proxy (deploy/render/wsgi.py).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user && mkdir -p /data && chown user /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn huggingface_hub
COPY --chown=user . .

USER user
ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    TOOLKIT_AUTH_DB=/data/toolkit-auth.db \
    TOOLKIT_SECURE_COOKIES=1
EXPOSE 10000
CMD ["sh", "/app/deploy/render/start.sh"]
