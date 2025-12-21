FROM ubuntu:22.04

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Set environment variables
ENV OLLAMA_HOST=0.0.0.0
ENV OLLAMA_MODELS=/root/.ollama/models

# Expose the default Ollama port
EXPOSE 11434

# Create a directory for Ollama configuration and data
# This will be used for persistent storage of authentication and models
VOLUME ["/root/.ollama"]

# Start Ollama serve
CMD ["ollama", "serve"]
