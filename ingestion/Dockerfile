FROM python:3.12-slim

WORKDIR /app

# Optional OCR fallback for scanned PDFs. Off by default because it adds
# ~120 MB (tesseract language packs + poppler).
ARG WITH_OCR=false

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
    && if [ "${WITH_OCR}" = "true" ] ; then \
         apt-get install -y --no-install-recommends tesseract-ocr poppler-utils ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY ingestion/requirements.txt .
COPY ingestion/adapters/office365/requirements.txt office365-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r office365-requirements.txt
RUN python -m spacy download de_core_news_md
RUN python -m spacy download en_core_web_lg

COPY shared/ /app/shared/
COPY ingestion/ /app/ingestion/

EXPOSE 8081

CMD ["uvicorn", "ingestion.ingestion_api:app", "--host", "0.0.0.0", "--port", "8081"]
