FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY swim_worker/ swim_worker/
COPY ca.crt ca.crt
CMD ["python", "-m", "swim_worker"]
