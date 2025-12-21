FROM debian:bookworm-slim

# Install dependencies for Ollama and SSL certificates
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
VOLUME ["/root/.ollama"]

# Start Ollama serve
CMD ["ollama", "serve"]
