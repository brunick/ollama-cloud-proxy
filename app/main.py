import os
from typing import List, Optional

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# Configuration
OLLAMA_CLOUD_URL = "https://ollama.com/api"
PROXY_AUTH_TOKEN = os.getenv("PROXY_AUTH_TOKEN")
ALLOW_UNAUTHENTICATED_ACCESS = (
    os.getenv("ALLOW_UNAUTHENTICATED_ACCESS", "false").lower() == "true"
)
CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")


def load_keys() -> List[str]:
    keys = []
    # 1. Try loading from config file
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f)
                if isinstance(config, dict) and "keys" in config:
                    keys = [str(k) for k in config["keys"] if k]
        except Exception as e:
            print(f"Error loading config file: {e}")

    # 2. Fallback to environment variables
    if not keys:
        env_keys = os.getenv("OLLAMA_API_KEYS", os.getenv("OLLAMA_API_KEY", ""))
        keys = [k.strip() for k in env_keys.split(",") if k.strip()]

    return keys


OLLAMA_API_KEYS = load_keys()
if not OLLAMA_API_KEYS:
    raise ValueError("No OLLAMA_API_KEYS found in config or environment variables")

current_key_index = 0


async def verify_auth(auth_header: Optional[str]):
    """Simple security layer to prevent unauthorized access to the proxy."""
    # Skip check if unauthenticated access is explicitly allowed
    if ALLOW_UNAUTHENTICATED_ACCESS:
        return

    # If no token is configured, we require one by default unless allowed above
    if not PROXY_AUTH_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Server configuration error: PROXY_AUTH_TOKEN is not set",
        )

    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Unauthorized: Missing or invalid token"
        )

    token = auth_header.split(" ")[1]
    if token != PROXY_AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid proxy token")


@app.get("/")
async def health_check():
    """Health check endpoint to verify proxy status and Ollama Cloud connectivity."""
    status = {"status": "ok", "ollama_cloud": "unknown"}
    try:
        async with httpx.AsyncClient() as client:
            # Check if we can reach Ollama Cloud API
            response = await client.get(OLLAMA_CLOUD_URL, timeout=5.0)
            if response.status_code < 500:
                status["ollama_cloud"] = "reachable"
            else:
                status["ollama_cloud"] = "unreachable"
    except Exception:
        status["ollama_cloud"] = "error"

    return status


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_ollama(
    request: Request, path: str, authorization: Optional[str] = Header(None)
):
    # 1. Verify access to this proxy
    await verify_auth(authorization)

    # 2. Prepare request to Ollama Cloud
    # Ollama Cloud API base is already https://ollama.com/api
    clean_path = path
    if path.startswith("api/"):
        clean_path = path[4:]
    elif path == "api":
        clean_path = ""

    url = f"{OLLAMA_CLOUD_URL}/{clean_path}".rstrip("/")
    method = request.method
    content = await request.body()
    params = request.query_params

    global current_key_index
    client = httpx.AsyncClient(timeout=None)

    # 3. Handle Streaming or normal response with retry logic for 429
    for attempt in range(len(OLLAMA_API_KEYS)):
        current_key = OLLAMA_API_KEYS[current_key_index]

        headers = {
            "Authorization": f"Bearer {current_key}",
            "Content-Type": request.headers.get("Content-Type", "application/json"),
        }

        try:
            req = client.build_request(
                method, url, content=content, params=params, headers=headers
            )
            response = await client.send(req, stream=True)

            # If quota exceeded, rotate key and try again
            if response.status_code == 429 and len(OLLAMA_API_KEYS) > 1:
                print(
                    f"Key {current_key_index} exceeded quota (429). Rotating to next key."
                )
                await response.aclose()
                current_key_index = (current_key_index + 1) % len(OLLAMA_API_KEYS)
                continue

            return StreamingResponse(
                response.aiter_raw(),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=None,  # Ensure response is closed correctly
            )
        except Exception as e:
            if attempt == len(OLLAMA_API_KEYS) - 1:
                raise HTTPException(status_code=500, detail=str(e))
            print(f"Request failed with error: {e}. Retrying with next key.")
            current_key_index = (current_key_index + 1) % len(OLLAMA_API_KEYS)

    raise HTTPException(status_code=429, detail="All API keys have exceeded quota")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=11434)
