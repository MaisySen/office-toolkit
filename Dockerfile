# Office Toolkit web app. System packages: LibreOffice (office conversion),
# Tesseract (OCR). No Ghostscript anywhere in the image (AGPL-free runtime).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc tesseract-ocr \
      tesseract-ocr-nep tesseract-ocr-hin tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
      tesseract-ocr-ara tesseract-ocr-vie tesseract-ocr-ind tesseract-ocr-spa \
      tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-jpn tesseract-ocr-kor \
      tesseract-ocr-tha tesseract-ocr-fil fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .

ENV PYTHONUNBUFFERED=1 \
    TOOLKIT_AUTH_DB=/data/toolkit-auth.db \
    TOOLKIT_SECURE_COOKIES=1
VOLUME /data
EXPOSE 8000

# Trust bootstrap: first registered account owns the install; keep
# TOOLKIT_REGISTRATION_OPEN unset afterwards.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "office_toolkit.web.app:app"]
