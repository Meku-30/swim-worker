FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY swim_worker/ swim_worker/
COPY ca.crt ca.crt
RUN useradd --create-home --shell /bin/bash appuser
USER appuser
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import redis, os; r=redis.Redis(host=os.environ.get('REDIS_HOST',''), port=int(os.environ.get('REDIS_PORT',6380)), password=os.environ.get('REDIS_PASSWORD',''), ssl=True, ssl_ca_certs='ca.crt'); r.ping()" || exit 1
CMD ["python", "-m", "swim_worker"]
