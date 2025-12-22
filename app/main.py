import os
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# Configuration from environment variables
OLLAMA_CLOUD_URL = "https://ollama.com/api"
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
PROXY_AUTH_TOKEN = os.getenv("PROXY_AUTH_TOKEN")
# If set to "true", authentication check will be skipped
ALLOW_UNAUTHENTICATED_ACCESS = (
    os.getenv("ALLOW_UNAUTHENTICATED_ACCESS", "false").lower() == "true"
)

if not OLLAMA_API_KEY:
    raise ValueError("OLLAMA_API_KEY environment variable is required")


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

    # Forward headers but replace Authorization with the real Ollama API Key
    headers = {
        "Authorization": f"Bearer {OLLAMA_API_KEY}",
        "Content-Type": request.headers.get("Content-Type", "application/json"),
    }

    client = httpx.AsyncClient(timeout=None)

    # 3. Handle Streaming or normal response
    try:
        req = client.build_request(
            method, url, content=content, params=params, headers=headers
        )
        response = await client.send(req, stream=True)

        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=dict(response.headers),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=11434)
