FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    sentence-transformers \
    torch --index-url https://download.pytorch.org/whl/cpu

COPY service.py .

# Modell beim Build vorladen (schnellerer Start)
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

EXPOSE 8082

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8082"]
