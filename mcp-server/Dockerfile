FROM python:3.12-slim

WORKDIR /app

COPY mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /app/shared/
COPY mcp-server/*.py ./
COPY init-db/ /app/init-db/

EXPOSE 8080

CMD ["python", "server.py"]
