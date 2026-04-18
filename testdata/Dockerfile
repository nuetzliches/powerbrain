FROM python:3.12-slim

WORKDIR /seed

# Build context is the repo root (see docker-compose.yml: context: .)
COPY testdata/documents.json testdata/documents_pii.json testdata/graph_seed.json ./
COPY testdata/seed.py testdata/entrypoint.sh ./
COPY scripts/seed_graph.py ./

RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
