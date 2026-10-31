# Office Toolkit on a free Docker host (Render free web service).
# Same runtime as the root Dockerfile: LibreOffice, Tesseract and an LGPL
# FFmpeg (R86), no
# Ghostscript. Differences: runs as uid 1000, listens on $PORT, restores
# and snapshots the auth database (deploy/render/persist.py) because the
# free host disk is not persistent, and refuses requests that did not
# come through the Cloudflare proxy (deploy/render/wsgi.py).
FROM python:3.12-slim

# OCR languages (P2): Nepali, Hindi and the common ones, Debian's fast
# models, about 22 MB together. fonts-dejavu-core gives the stamp, sign and
# compare pages a Unicode font (about 3 MB).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress tesseract-ocr \
      tesseract-ocr-nep tesseract-ocr-hin tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
      tesseract-ocr-ara tesseract-ocr-vie tesseract-ocr-ind tesseract-ocr-spa \
      tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-jpn tesseract-ocr-kor \
      tesseract-ocr-tha tesseract-ocr-fil fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# R86 video and audio: an LGPL build of FFmpeg (version3, no GPL parts:
# OpenH264 and libvpx are BSD, libopus BSD, LAME LGPL), only ffmpeg, ffprobe
# and the libav libraries (about 180 MB on disk). The build stops if the
# binary says it was configured with --enable-gpl or --enable-nonfree.
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-lgpl-shared-8.1.tar.xz
ADD ${FFMPEG_URL} /tmp/ffmpeg.tar.xz
RUN python -c "import tarfile; tarfile.open('/tmp/ffmpeg.tar.xz').extractall('/tmp/ff', filter='data')" \
    && d=$(ls -d /tmp/ff/ffmpeg-*) && mkdir -p /opt/ffmpeg/bin /opt/ffmpeg/lib \
    && cp "$d/bin/ffmpeg" "$d/bin/ffprobe" /opt/ffmpeg/bin/ \
    && cp -a "$d"/lib/*.so* /opt/ffmpeg/lib/ && cp "$d/LICENSE.txt" /opt/ffmpeg/ 2>/dev/null; \
    rm -rf /tmp/ff /tmp/ffmpeg.tar.xz \
    && ln -s /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg && ln -s /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe \
    && ffmpeg -hide_banner -version > /tmp/ffv && ! grep -q -e '--enable-gpl' -e '--enable-nonfree' /tmp/ffv \
    && grep -q -e '--enable-libopenh264' /tmp/ffv && rm /tmp/ffv

RUN useradd -m -u 1000 user && mkdir -p /data && chown user /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn huggingface_hub
COPY --chown=user . .
# Engine P2 (ops_p2b): LibreOffice renders HTML, Markdown and templates with
# the bundled Noto and Droid fonts (office_toolkit/fonts, about 4.8 MB, OFL
# and Apache 2.0), so Nepali, Hindi, Chinese, Japanese and Korean text has
# glyphs without the large font packages.
RUN cat /app/office_toolkit/fonts/parts/DroidSansFallback.ttf.part* > /app/office_toolkit/fonts/DroidSansFallback.ttf \
    && mkdir -p /usr/local/share/fonts/multies \
    && cp /app/office_toolkit/fonts/*.ttf /usr/local/share/fonts/multies/ \
    && (fc-cache -f >/dev/null 2>&1 || true)

USER user
ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    TOOLKIT_AUTH_DB=/data/toolkit-auth.db \
    TOOLKIT_SECURE_COOKIES=1
EXPOSE 10000
CMD ["sh", "/app/deploy/render/start.sh"]
