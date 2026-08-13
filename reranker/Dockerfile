FROM python:3.12-slim

WORKDIR /app

# Install from reranker/requirements.txt so its pinned versions are the single
# source of truth (see CLAUDE.md "Dependency Pinning"). This step used to
# install an unpinned inline list, which bypassed those pins entirely and also
# omitted pydantic and the OpenTelemetry packages — so the tracing this service
# is configured for in docker-compose.yml silently degraded to disabled.
# The PyTorch CPU index keeps the image small: `torch==X` also matches `X+cpu`,
# which that index serves and pip prefers over the PyPI (CUDA) wheel.
COPY reranker/requirements.txt .
RUN pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

COPY shared/ ./shared/
COPY reranker/service.py .

# Modell beim Build vorladen (schnellerer Start)
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

EXPOSE 8082

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8082"]
