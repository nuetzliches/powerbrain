FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m spacy download de_core_news_md

COPY . .

EXPOSE 8081

CMD ["uvicorn", "ingestion_api:app", "--host", "0.0.0.0", "--port", "8081"]
