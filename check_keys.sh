#!/bin/bash

# Ollama Cloud Proxy - Key Health Check Script
# This script triggers the internal health check for all API keys.

# Configuration
PROXY_URL=${OLLAMA_PROXY_URL:-"http://localhost:11434"}
AUTH_TOKEN=${PROXY_AUTH_TOKEN:-""}

if [ -z "$AUTH_TOKEN" ]; then
    echo "Warning: PROXY_AUTH_TOKEN is not set. If the proxy requires authentication, this check will fail."
fi

echo "Checking key health at $PROXY_URL..."

curl -s -X GET "$PROXY_URL/health/keys" \
     -H "Authorization: Bearer $AUTH_TOKEN" \
     -H "Content-Type: application/json" | jq .

if [ $? -ne 0 ]; then
    echo "Error: Failed to connect to the proxy or jq is not installed."
    exit 1
fi
