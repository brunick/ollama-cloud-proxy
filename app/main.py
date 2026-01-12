import json
import os
import sqlite3
from datetime import datetime
from typing import List, Optional

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# Database setup
DB_PATH = "data/usage.db"
os.makedirs("data", exist_ok=True)


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                key_index INTEGER,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER
            )
        """)


init_db()


def record_usage(
    key_index: int, model: str, prompt_tokens: int, completion_tokens: int
):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO usage (key_index, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?)",
                (key_index, model, prompt_tokens, completion_tokens),
            )
    except Exception as e:
        print(f"Error recording usage: {e}")


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
    status = {"status": "ok", "ollama_cloud": "unknown", "usage_summary": {}}
    try:
        async with httpx.AsyncClient() as client:
            # Check if we can reach Ollama Cloud API
            response = await client.get(OLLAMA_CLOUD_URL, timeout=5.0)
            if response.status_code < 500:
                status["ollama_cloud"] = "reachable"
            else:
                status["ollama_cloud"] = "unreachable"

        # Add a small summary to health check
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT COUNT(*) as total_req, SUM(prompt_tokens) as p_tokens, SUM(completion_tokens) as c_tokens FROM usage"
            ).fetchone()
            if row:
                status["usage_summary"] = dict(row)

    except Exception:
        status["ollama_cloud"] = "error"

    return status


@app.get("/stats")
async def get_stats():
    """Returns hourly aggregated usage statistics."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            # Aggregate by date, hour, key_index, and model
            query = """
                SELECT
                    strftime('%Y-%m-%d', timestamp) as date,
                    strftime('%H', timestamp) as hour,
                    key_index,
                    model,
                    COUNT(*) as requests,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens
                FROM usage
                GROUP BY date, hour, key_index, model
                ORDER BY date DESC, hour DESC
            """
            rows = conn.execute(query).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving stats: {str(e)}")


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

    async def log_stream_usage(response_iter, k_index):
        last_chunk = b""
        async for chunk in response_iter:
            yield chunk
            last_chunk = chunk  # Keep track of last chunk to find stats

        # After stream finishes, try to extract stats from the last chunk(s)
        try:
            # Ollama usually sends the final stats in the last line
            decoded = last_chunk.decode().strip().split("\n")[-1]
            data = json.loads(decoded)
            if data.get("done"):
                record_usage(
                    k_index,
                    data.get("model", "unknown"),
                    data.get("prompt_eval_count", 0),
                    data.get("eval_count", 0),
                )
        except Exception:
            pass

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

            # If not a stream or if we want to parse it later, we need to handle it.
            # But Ollama is mostly streaming or single JSON.
            # We wrap the iterator to catch the usage data at the end.
            return StreamingResponse(
                log_stream_usage(response.aiter_raw(), current_key_index),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=None,
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
