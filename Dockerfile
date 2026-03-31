FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY swim_worker/ swim_worker/
COPY ca.crt ca.crt
CMD ["python", "-m", "swim_worker"]
