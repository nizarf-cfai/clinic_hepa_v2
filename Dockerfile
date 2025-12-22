FROM python:3.11-slim

# Install system dependencies required for some python packages
# (We shouldn't need audio libs, but this prevents some build errors)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Cloud Run expects the app to listen on the $PORT environment variable
CMD exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}