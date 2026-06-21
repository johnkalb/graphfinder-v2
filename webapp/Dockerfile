FROM python:3.12-slim

WORKDIR /app

# Copy requirements and install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY pathfinder.py .
COPY data/ ./data/

# Expose port
EXPOSE 8000

# Run
CMD ["uvicorn", "pathfinder:app", "--host", "0.0.0.0", "--port", "8000"]
 
