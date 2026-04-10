FROM python:3.14-slim

WORKDIR /app

COPY pb-proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY pb-proxy/*.py ./

EXPOSE 8090

CMD ["python", "proxy.py"]
