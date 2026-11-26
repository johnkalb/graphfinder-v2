FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libsodium23 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Nightly data files come from the graph-history Space, not git (data_store.py).
# HISTORY_SPACE is a BUILD_TIME secret on the DO app, passed in as a build arg.
ARG HISTORY_SPACE
RUN pip install --no-cache-dir boto3 && python data_store.py fetch

EXPOSE 8000

CMD ["uvicorn", "webapp.pathfinder:app", "--host", "0.0.0.0", "--port", "8000"]
