FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py mcp_client.py ./
COPY panels ./panels
COPY assets ./assets
COPY fixtures ./fixtures

EXPOSE 8095

# --server.headless=true suppresses the initial "Email" prompt in Docker.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8095", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
