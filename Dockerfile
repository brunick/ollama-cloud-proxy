FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Set environment variables
ARG APP_VERSION=v1.20.10
ENV APP_VERSION=${APP_VERSION}
ENV OLLAMA_HOST=0.0.0.0
ENV PORT=11434

# Expose the proxy port
EXPOSE 11434

# Start the Python proxy
CMD ["python", "main.py"]
