# To check key health externally, use:
# curl -H "Authorization: Bearer $PROXY_AUTH_TOKEN" http://localhost:11434/health/keys

import asyncio
import gzip
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

app = FastAPI()

APP_VERSION = os.getenv("APP_VERSION", "v1.20.9")

# Setup Logging
LOG_FILE = "data/proxy.log"
os.makedirs("data", exist_ok=True)


class StreamToLogger:
    def __init__(self, logger_name, level, original_stream):
        self.logger = logging.getLogger(logger_name)
        self.level = level
        self.original_stream = original_stream

    def write(self, buf):
        # Only log if there is actual content
        if buf.strip():
            for line in buf.rstrip().splitlines():
                self.logger.log(self.level, line.rstrip())

    def flush(self):
        self.original_stream.flush()

    def __getattr__(self, name):
        # Delegate attributes like isatty, fileno, encoding etc. to the original stream
        return getattr(self.original_stream, name)


class DashboardLogHandler(logging.Handler):
    def __init__(self, capacity=1000):
        super().__init__()
        self.logs = deque(maxlen=capacity)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.logs.append(
                {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "level": record.levelname.ljust(5),
                    "message": msg,
                }
            )
        except Exception:
            self.handleError(record)


dashboard_log_handler = DashboardLogHandler()
dashboard_log_handler.setFormatter(logging.Formatter("%(message)s"))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(dashboard_log_handler)

file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
root_logger.addHandler(file_handler)

original_stdout = sys.stdout
original_stderr = sys.stderr

console_handler = logging.StreamHandler(original_stdout)
console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)
root_logger.addHandler(console_handler)

sys.stdout = StreamToLogger("sys.stdout", logging.INFO, original_stdout)
sys.stderr = StreamToLogger("sys.stderr", logging.ERROR, original_stderr)

# Uvicorn Setup
for u_name in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
    u_log = logging.getLogger(u_name)
    u_log.handlers = []
    u_log.propagate = True

_builtin_print = print


def print(*args, **kwargs):
    msg = " ".join(str(arg) for arg in args)
    logging.info(msg)


# Store latest rate limit headers per key index
rate_limit_store = {}
# Penalty box for keys that returned 429/50x: {key_index: expiration_timestamp}
key_penalty_box: Dict[int, float] = {}
# Track backoff levels for keys: {key_index: level_index}
key_backoff_levels: Dict[int, int] = {}
key_backoff_levels_50x: Dict[int, int] = {}
# Global cache for key health results
cached_health_results: Dict[str, dict] = {}
last_health_check_timestamp: float = 0

# Backoff stages in seconds: 15m, 1h, 2h, 6h, 12h, 24h
BACKOFF_STAGES = [
    15 * 60,  # 15 min
    1 * 60 * 60,  # 1 hour
    2 * 60 * 60,  # 2 hours
    6 * 60 * 60,  # 6 hours
    12 * 60 * 60,  # 12 hours
    24 * 60 * 60,  # 24 hours
]

# Backoff stages for 50x errors: 30s, 2m, 5m, 15m, 1h
BACKOFF_STAGES_50X = [
    30,  # 30 sec
    120,  # 2 min
    300,  # 5 min (user's requested max for initial repeat)
    900,  # 15 min
    3600,  # 1 hour
]

# Persistent HTTP client for streaming stability
http_client = httpx.AsyncClient(timeout=None)

# Database setup
DB_PATH = "data/usage.db"
os.makedirs("data", exist_ok=True)


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                client_ip TEXT,
                key_index INTEGER,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER
            )
        """)
        # Simple migration to add client_ip if it doesn't exist
        try:
            conn.execute("ALTER TABLE usage ADD COLUMN client_ip TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Performance indexes
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage (timestamp)"
        )


init_db()


def init_extra_tables():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                client_ip TEXT,
                method TEXT,
                endpoint TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                file_path TEXT
            )
        """)
        # Performance indexes
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests (timestamp)"
        )


init_extra_tables()


def record_usage(
    client_ip: str,
    key_index: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO usage (client_ip, key_index, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?)",
                (client_ip, key_index, model, prompt_tokens, completion_tokens),
            )
    except Exception as e:
        print(f"Error recording usage: {e}")


def store_request_file(client_ip: str, request_body: bytes) -> str:
    """Store the raw request body compressed as gzip immediately.

    Directory hierarchy:
        data/requests/<client_ip>/<YYYY-MM-DD>/...

    Returns the relative file path for DB storage.
    """
    safe_ip = client_ip.replace(":", "_")
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    base_dir = os.path.join("data", "requests", safe_ip, date_str)
    os.makedirs(base_dir, exist_ok=True)
    filename = (
        datetime.utcnow().strftime("%Y%m%dT%H%M%S") + f"_{uuid.uuid4().hex}.json.gz"
    )
    file_path = os.path.join(base_dir, filename)
    try:
        with gzip.open(file_path, "wb") as f:
            f.write(request_body or b"")
    except Exception as e:
        print(f"Error storing request file: {e}")
    return file_path


def create_request_log(
    client_ip: str,
    method: str,
    endpoint: str,
    file_path: str,
) -> Optional[int]:
    """Create an initial request log entry and return its ID."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                """
                INSERT INTO requests (
                    client_ip,
                    method,
                    endpoint,
                    file_path,
                    model
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (client_ip, method, endpoint, file_path),
            )
            return cursor.lastrowid
    except Exception as e:
        print(f"Error creating request log: {e}")
        return None


def update_request_log(
    request_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
):
    """Update a request log entry with token counts and actual model."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                UPDATE requests
                SET model = ?, prompt_tokens = ?, completion_tokens = ?
                WHERE id = ?
                """,
                (model, prompt_tokens, completion_tokens, request_id),
            )
    except Exception as e:
        print(f"Error updating request log: {e}")


# Configuration
OLLAMA_CLOUD_URL = "https://ollama.com"
PROXY_AUTH_TOKEN = os.getenv("PROXY_AUTH_TOKEN")
ALLOW_UNAUTHENTICATED_ACCESS = (
    os.getenv("ALLOW_UNAUTHENTICATED_ACCESS", "false").lower() == "true"
)
CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")


def load_keys() -> List[str]:
    """Load API keys exclusively from the config file."""
    keys = []
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f)
                if isinstance(config, dict) and "keys" in config:
                    keys = [str(k) for k in config["keys"] if k]
        except Exception as e:
            print(f"Error loading config file: {e}")

    if not keys:
        print(
            f"CRITICAL: No API keys found in {CONFIG_PATH}. Please provide keys in the config file."
        )

    return keys


OLLAMA_API_KEYS = load_keys()
if not OLLAMA_API_KEYS:
    raise ValueError(
        "No API keys found in config/config.yaml. Environment variables are no longer supported."
    )


def get_best_key_index(exclude: Optional[set] = None) -> int:
    """Find the best key index based on usage in the last 2 hours and penalty box."""
    now = time.time()
    exclude = exclude or set()

    # Filter out keys in penalty box and excluded keys
    available_indices = [
        i
        for i in range(len(OLLAMA_API_KEYS))
        if (i not in key_penalty_box or key_penalty_box[i] < now) and i not in exclude
    ]

    # If all keys are penalized or excluded, use the one that expires soonest (among non-excluded)
    if not available_indices:
        remaining_non_excluded = [
            i for i in range(len(OLLAMA_API_KEYS)) if i not in exclude
        ]
        if not remaining_non_excluded:
            return None

        penalized_non_excluded = {
            i: key_penalty_box[i]
            for i in remaining_non_excluded
            if i in key_penalty_box
        }
        if not penalized_non_excluded:
            return remaining_non_excluded[0]

        return min(penalized_non_excluded, key=penalized_non_excluded.get)

    # If only one key available, return it
    if len(available_indices) == 1:
        return available_indices[0]

    # Query usage for available keys in the last 2 hours
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join(["?"] * len(available_indices))
            query = f"""
                SELECT key_index, SUM(prompt_tokens + completion_tokens) as usage
                FROM usage
                WHERE timestamp >= datetime('now', '-2 hours')
                AND key_index IN ({placeholders})
                GROUP BY key_index
            """
            rows = conn.execute(query, available_indices).fetchall()
            usage_map = {row["key_index"]: row["usage"] for row in rows}

            # Find the index with minimum usage among available
            best_index = available_indices[0]
            min_usage = usage_map.get(best_index, 0)

            for idx in available_indices:
                usage = usage_map.get(idx, 0)
                if usage < min_usage:
                    min_usage = usage
                    best_index = idx
            return best_index
    except Exception as e:
        print(f"Error determining best key: {e}")
        return available_indices[0]


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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return ""


@app.get("/")
async def root_redirect():
    """Redirect root to dashboard."""
    return RedirectResponse(url="/dashboard")


@app.get("/health")
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
                    strftime('%Y-%m-%dT%H:00:00Z', timestamp) as bucket,
                    client_ip,
                    key_index,
                    model,
                    COUNT(*) as requests,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens
                FROM usage
                GROUP BY bucket, client_ip, key_index, model
                ORDER BY bucket DESC
            """
            rows = conn.execute(query).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving stats: {str(e)}")


@app.post("/health/keys/{key_index}/reset")
async def reset_key_penalty(
    key_index: int, authorization: Optional[str] = Header(None)
):
    """Manually reset the penalty and backoff, then perform an immediate check."""
    await verify_auth(authorization)

    if key_index >= len(OLLAMA_API_KEYS):
        raise HTTPException(status_code=404, detail="Key index out of range")

    # Clear existing penalties to allow the check to proceed
    if key_index in key_penalty_box:
        del key_penalty_box[key_index]
    if key_index in key_backoff_levels:
        del key_backoff_levels[key_index]
    if key_index in key_backoff_levels_50x:
        del key_backoff_levels_50x[key_index]

    # Perform immediate health check for this specific key
    key = OLLAMA_API_KEYS[key_index]
    now = time.time()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            # Use generation test for consistency with background worker
            payload = {"model": "gemma3:4b-cloud", "prompt": "test", "stream": False}
            response = await client.post(
                f"{OLLAMA_CLOUD_URL}/api/generate",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )

            status_text = f"❌ ERROR {response.status_code}"
            if response.status_code == 200:
                status_text = "✅ OK"
            elif response.status_code == 429:
                status_text = "🚫 STILL RATE LIMITED"
                key_penalty_box[key_index] = now + BACKOFF_STAGES[0]
                key_backoff_levels[key_index] = 0

            # Immediately update cache
            cached_health_results[f"key_{key_index}"] = {
                "status": status_text,
                "penalty_active": key_index in key_penalty_box
                and key_penalty_box[key_index] > now,
                "expires_in": int(key_penalty_box[key_index] - now)
                if key_index in key_penalty_box
                else 0,
                "backoff_level": key_backoff_levels.get(key_index, 0),
                "usage_2h": cached_health_results.get(f"key_{key_index}", {}).get(
                    "usage_2h", 0
                ),
            }

            return {"status": status_text, "key_index": key_index}
        except Exception as e:
            status_text = "📡 OFFLINE"
            cached_health_results[f"key_{key_index}"] = {
                "status": status_text,
                "penalty_active": False,
                "expires_in": 0,
                "backoff_level": 0,
                "usage_2h": 0,
            }
            return {"status": status_text, "error": str(e), "key_index": key_index}


@app.post("/health/keys/{key_index}/penalize")
async def penalize_key_manually(
    key_index: int, authorization: Optional[str] = Header(None)
):
    """Manually put a key in the penalty box."""
    await verify_auth(authorization)

    if key_index >= len(OLLAMA_API_KEYS):
        raise HTTPException(status_code=404, detail="Key index out of range")

    # Set to first backoff level (15 min) or keep existing if higher
    current_level = key_backoff_levels.get(key_index, 0)
    key_backoff_levels[key_index] = current_level
    expires_in = BACKOFF_STAGES[current_level]
    key_penalty_box[key_index] = time.time() + expires_in

    # Update cache
    cached_health_results[f"key_{key_index}"] = {
        "status": "⏳ PENALIZED",
        "penalty_active": True,
        "expires_in": expires_in,
        "backoff_level": current_level,
        "usage_2h": cached_health_results.get(f"key_{key_index}", {}).get(
            "usage_2h", 0
        ),
    }

    return {
        "status": "penalized_manually",
        "key_index": key_index,
        "expires_in": expires_in,
    }


async def perform_keys_health_check(force_all: bool = False):
    """
    Internal logic to check keys.
    If force_all is False, it only checks keys that are not currently penalized
    or whose penalty has expired.
    """
    global cached_health_results, last_health_check_timestamp
    now = time.time()

    async def check_single_key(i: int, key: str, client: httpx.AsyncClient):
        is_penalized = i in key_penalty_box and key_penalty_box[i] > now

        if is_penalized and not force_all:
            return f"key_{i}", {
                "status": "⏳ PENALIZED",
                "penalty_active": True,
                "expires_in": int(key_penalty_box[i] - now),
                "backoff_level": key_backoff_levels.get(i, 0),
                "usage_2h": 0,
            }

        try:
            payload = {
                "model": "gemma3:4b-cloud",
                "prompt": "test",
                "stream": True,
            }
            response = await client.post(
                f"{OLLAMA_CLOUD_URL}/api/generate",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code == 200:
                status = "✅ OK"
                if i in key_penalty_box:
                    del key_penalty_box[i]
                if i in key_backoff_levels:
                    del key_backoff_levels[i]
                if i in key_backoff_levels_50x:
                    del key_backoff_levels_50x[i]
            elif response.status_code == 429:
                status = "🚫 RATE LIMITED"
                current_level = key_backoff_levels.get(i, -1) + 1
                level = min(current_level, len(BACKOFF_STAGES) - 1)
                key_backoff_levels[i] = level
                key_penalty_box[i] = now + BACKOFF_STAGES[level]
            else:
                status = f"❌ ERROR {response.status_code}"

            return f"key_{i}", {
                "status": status,
                "penalty_active": i in key_penalty_box and key_penalty_box[i] > now,
                "expires_in": int(key_penalty_box[i] - now)
                if i in key_penalty_box
                else 0,
                "backoff_level": key_backoff_levels.get(i, 0),
                "usage_2h": 0,
            }
        except Exception as e:
            return f"key_{i}", {"status": "📡 OFFLINE", "error": str(e)}

    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [
            check_single_key(i, key, client) for i, key in enumerate(OLLAMA_API_KEYS)
        ]
        check_results = await asyncio.gather(*tasks)
        results = dict(check_results)

    # Add usage info
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        usage_rows = conn.execute("""
            SELECT key_index, SUM(prompt_tokens + completion_tokens) as usage
            FROM usage WHERE timestamp >= datetime('now', '-2 hours')
            GROUP BY key_index
        """).fetchall()
        for row in usage_rows:
            key_id = f"key_{row['key_index']}"
            if key_id in results:
                results[key_id]["usage_2h"] = row["usage"]

    cached_health_results = results
    last_health_check_timestamp = now
    return results


@app.get("/health/keys")
async def check_keys_health(
    force: bool = False, authorization: Optional[str] = Header(None)
):
    """Get health status for all keys. Returns cached results by default."""
    await verify_auth(authorization)
    if force or not cached_health_results:
        return await perform_keys_health_check(force_all=force)
    return cached_health_results


async def background_health_worker():
    """Background task that periodically re-tests keys that are ready."""
    print("Background Health Worker started.")
    while True:
        try:
            # Re-test keys whose penalty has expired
            await perform_keys_health_check(force_all=False)
        except Exception as e:
            print(f"Background worker error: {e}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def startup_event():
    # Start the background health worker
    asyncio.create_task(background_health_worker())


@app.get("/stats/minute")
async def get_minute_stats(window: int = 60):
    """Returns token usage aggregated by minute for the last 'window' minutes."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            # Use current_timestamp - window minutes
            query = """
                SELECT
                    strftime('%Y-%m-%dT%H:%M:00Z', timestamp) as minute,
                    model,
                    SUM(prompt_tokens + completion_tokens) as total_tokens
                FROM usage
                WHERE timestamp >= datetime('now', ?)
                GROUP BY minute, model
                ORDER BY minute ASC
            """
            rows = conn.execute(query, (f"-{window} minutes",)).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error retrieving minute stats: {str(e)}"
        )


@app.get("/stats/24h")
async def get_24h_stats():
    """Returns total tokens aggregated by hour for the last 24 hours."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT
                    strftime('%Y-%m-%dT%H:00:00Z', timestamp) as hour_bucket,
                    SUM(prompt_tokens + completion_tokens) as total_tokens
                FROM usage
                WHERE timestamp >= datetime('now', '-24 hours')
                GROUP BY hour_bucket
                ORDER BY hour_bucket ASC
            """
            rows = conn.execute(query).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error retrieving 24h stats: {str(e)}"
        )


@app.get("/queries")
async def get_queries(
    limit: int = 50,
    offset: int = 0,
    ip: Optional[str] = None,
    model: Optional[str] = None,
):
    """Returns individual query logs."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM requests WHERE 1=1"
            params = []
            if ip:
                query += " AND client_ip = ?"
                params.append(ip)
            if model:
                query += " AND model = ?"
                params.append(model)
            query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error retrieving queries: {str(e)}"
        )


@app.get("/queries/{query_id}/body")
async def get_query_body(query_id: int):
    """Returns the raw request body for a query."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT file_path FROM requests WHERE id = ?", (query_id,)
            ).fetchone()
            if not row or not row["file_path"]:
                raise HTTPException(status_code=404, detail="Request body not found")

            file_path = row["file_path"]
            if not os.path.exists(file_path):
                raise HTTPException(status_code=404, detail="File no longer exists")

            with gzip.open(file_path, "rb") as f:
                content = f.read()
                try:
                    return json.loads(content)
                except:
                    return {"raw": content.decode(errors="ignore")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading body: {str(e)}")


@app.get("/logs")
async def get_logs():
    """Returns the latest captured logs."""
    return list(dashboard_log_handler.logs)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serves a simple dashboard to view statistics and queries."""
    html_content = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ollama Proxy Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { background-color: #0f172a; color: #f8fafc; }
        .card { background-color: #1e293b; border: 1px solid #334155; }
        #logs-container { background-color: #020617; border: 1px solid #1e293b; }
        .scrollbar-hide::-webkit-scrollbar { display: none; }
        .scrollbar-hide { -ms-overflow-style: none; scrollbar-width: none; }
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="mb-8 flex justify-between items-center">
            <div>
                <h1 class="text-3xl font-bold flex items-center gap-2">
                    <i data-lucide="bar-chart-3" class="text-blue-400"></i>
                    Ollama Proxy Dashboard
                </h1>
                <p class="text-slate-400 flex items-center gap-2">
                    Monitoring and Usage Statistics
                    <span class="px-2 py-0.5 bg-slate-800 text-slate-500 rounded text-[10px] font-mono border border-slate-700">{APP_VERSION}</span>
                </p>
            </div>
            <div class="flex items-center gap-6">
                <div class="flex items-center gap-4 text-xs font-medium">
                    <div class="flex items-center gap-1.5 px-3 py-1.5 bg-slate-800/50 rounded-full border border-slate-700">
                        <span id="proxy-status-dot" class="w-2 h-2 rounded-full bg-slate-500"></span>
                        <span class="text-slate-400 uppercase tracking-wider">Proxy</span>
                    </div>
                    <div class="flex items-center gap-1.5 px-3 py-1.5 bg-slate-800/50 rounded-full border border-slate-700">
                        <span id="cloud-status-dot" class="w-2 h-2 rounded-full bg-slate-500"></span>
                        <span class="text-slate-400 uppercase tracking-wider">Ollama Cloud</span>
                    </div>
                </div>
                <button onclick="loadStats(true)" class="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg flex items-center gap-2 transition">
                    <i data-lucide="refresh-cw" size="18"></i> Refresh
                </button>
            </div>
        </header>

        <!-- Tab Navigation -->
        <div class="flex border-b border-slate-700 mb-8 gap-2">
            <button onclick="switchTab('dashboard')" id="btn-tab-dashboard" class="px-6 py-2 font-medium border-b-2 border-blue-500 text-blue-400 flex items-center gap-2 transition-all">
                <i data-lucide="layout-dashboard" size="18"></i> Dashboard
            </button>
            <button onclick="switchTab('logs')" id="btn-tab-logs" class="px-6 py-2 font-medium border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 transition-all">
                <i data-lucide="terminal" size="18"></i> Server Logs
            </button>
        </div>

        <div id="tab-dashboard">
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <div class="md:col-span-3 card rounded-xl p-6">
                <h2 class="text-xl font-semibold mb-4 flex items-center gap-2">
                    <i data-lucide="key"></i> API Key Status & Load Balancing
                </h2>
                <div id="keys-wrapper">
                    <div class="grid grid-cols-1 md:grid-cols-3 gap-4" id="keys-container">
                        <!-- Key status cards will be injected here -->
                    </div>
                    <div id="keys-collapsed-container" class="hidden mt-4 pt-4 border-t border-slate-700">
                        <div class="grid grid-cols-1 md:grid-cols-3 gap-4" id="keys-container-more">
                            <!-- Additional keys will be injected here -->
                        </div>
                    </div>
                    <button id="show-more-keys-btn" onclick="toggleMoreKeys()" class="hidden w-full mt-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-400 text-xs rounded-lg transition flex items-center justify-center gap-2">
                        <span id="show-more-text">Show More Keys</span>
                        <i id="show-more-icon" data-lucide="chevron-down" size="14"></i>
                    </button>
                </div>
            </div>

            <div class="card rounded-xl p-6 relative overflow-hidden flex flex-col justify-center">
                <div class="relative z-10">
                    <h2 class="text-sm font-medium text-slate-400 mb-1 uppercase tracking-wider">Total Tokens (24h)</h2>
                    <div class="text-4xl font-bold text-white" id="total-tokens-count">0</div>
                </div>
                <div class="absolute bottom-0 left-0 w-full h-16 opacity-50">
                    <canvas id="sparklineChart"></canvas>
                </div>
            </div>
        </div>

        <div class="card rounded-xl p-6 mb-8">
            <div class="flex flex-col md:flex-row justify-between items-start md:items-center mb-4 gap-4">
                <h2 class="text-xl font-semibold flex items-center gap-2">
                    <i data-lucide="line-chart"></i> Token Usage
                </h2>
                <div class="flex items-center gap-2 bg-slate-800 p-1 rounded-lg border border-slate-700">
                    <button onclick="setTimeRange(10)" id="btn-10" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition text-slate-400 hover:text-white">10m</button>
                    <button onclick="setTimeRange(60)" id="btn-60" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition bg-blue-600 text-white">60m</button>
                    <button onclick="setTimeRange(120)" id="btn-120" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition text-slate-400 hover:text-white">2h</button>
                    <button onclick="setTimeRange(240)" id="btn-240" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition text-slate-400 hover:text-white">4h</button>
                    <button onclick="setTimeRange(360)" id="btn-360" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition text-slate-400 hover:text-white">6h</button>
                    <button onclick="setTimeRange(720)" id="btn-720" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition text-slate-400 hover:text-white">12h</button>
                    <button onclick="setTimeRange(1440)" id="btn-1440" class="time-range-btn px-3 py-1 rounded-md text-xs font-medium transition text-slate-400 hover:text-white">24h</button>
                </div>
            </div>
            <div class="h-64 w-full">
                <canvas id="usageChart"></canvas>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-8 mb-12">
            <div class="card rounded-xl p-6 flex flex-col h-[500px]">
                <h2 class="text-xl font-semibold mb-4 flex items-center gap-2 flex-none">
                    <i data-lucide="activity"></i> Aggregated Stats
                </h2>
                <div class="overflow-auto flex-1">
                    <table class="w-full text-sm text-left border-collapse">
                        <thead class="text-xs uppercase bg-slate-800 text-slate-300 sticky top-0 z-10 shadow-sm">
                            <tr>
                                <th class="px-4 py-2 bg-slate-800">Date/Hour</th>
                                <th class="px-4 py-2 bg-slate-800">IP</th>
                                <th class="px-4 py-2 bg-slate-800">Model</th>
                                <th class="px-4 py-2 text-right bg-slate-800">Reqs</th>
                                <th class="px-4 py-2 text-right bg-slate-800">Tokens</th>
                            </tr>
                        </thead>
                        <tbody id="stats-body"></tbody>
                    </table>
                </div>
            </div>

            <div class="card rounded-xl p-6 flex flex-col h-[500px]">
                <h2 class="text-xl font-semibold mb-4 flex items-center gap-2 flex-none">
                    <i data-lucide="list"></i> Recent Queries
                </h2>
                <div class="overflow-auto flex-1">
                    <table class="w-full text-sm text-left border-collapse">
                        <thead class="text-xs uppercase bg-slate-800 text-slate-300 sticky top-0 z-10 shadow-sm">
                            <tr>
                                <th class="px-4 py-2 bg-slate-800">Timestamp</th>
                                <th class="px-4 py-2 bg-slate-800">IP</th>
                                <th class="px-4 py-2 bg-slate-800">Model</th>
                                <th class="px-4 py-2 bg-slate-800">Tokens</th>
                                <th class="px-4 py-2 text-right bg-slate-800">Action</th>
                            </tr>
                        </thead>
                        <tbody id="queries-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="body-viewer" class="hidden fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
            <div class="card w-full max-w-4xl max-h-[80vh] rounded-2xl p-6 flex flex-col">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-bold">Request Body</h3>
                    <button onclick="closeViewer()" class="text-slate-400 hover:text-white">
                        <i data-lucide="x"></i>
                    </button>
                </div>
                <pre id="body-content" class="bg-slate-900 p-4 rounded-lg overflow-auto text-xs text-green-400 flex-1 font-mono"></pre>
            </div>
        </div>

        <!-- Logs Tab -->
        <div id="tab-logs" class="hidden">
            <div class="card rounded-xl p-6 flex flex-col h-[calc(100vh-250px)]">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-xl font-semibold flex items-center gap-2">
                        <i data-lucide="scroll-text"></i> Live Server Logs
                    </h2>
                    <div class="flex items-center gap-2">
                        <button onclick="loadLogs()" class="p-2 hover:bg-slate-800 rounded-lg text-slate-400 hover:text-white transition" title="Refresh Logs">
                            <i data-lucide="refresh-cw" size="18"></i>
                        </button>
                        <button onclick="clearLogsUI()" class="p-2 hover:bg-slate-800 rounded-lg text-slate-400 hover:text-red-400 transition" title="Clear View">
                            <i data-lucide="trash-2" size="18"></i>
                        </button>
                    </div>
                </div>
                <div id="logs-container" class="rounded-lg p-4 font-mono text-[11px] overflow-auto flex-1 scrollbar-hide">
                    <div class="text-slate-500 italic">Waiting for logs...</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let usageChart = null;
        let sparklineChart = null;
        let currentTimeRange = 60;
        let currentTab = 'dashboard';

        function switchTab(tab) {
            currentTab = tab;
            if (tab === 'dashboard') {
                document.getElementById('tab-dashboard').classList.remove('hidden');
                document.getElementById('tab-logs').classList.add('hidden');
                document.getElementById('btn-tab-dashboard').classList.add('border-blue-500', 'text-blue-400');
                document.getElementById('btn-tab-dashboard').classList.remove('border-transparent', 'text-slate-400');
                document.getElementById('btn-tab-logs').classList.remove('border-blue-500', 'text-blue-400');
                document.getElementById('btn-tab-logs').classList.add('border-transparent', 'text-slate-400');
            } else {
                document.getElementById('tab-dashboard').classList.add('hidden');
                document.getElementById('tab-logs').classList.remove('hidden');
                document.getElementById('btn-tab-logs').classList.add('border-blue-500', 'text-blue-400');
                document.getElementById('btn-tab-logs').classList.remove('border-transparent', 'text-slate-400');
                document.getElementById('btn-tab-dashboard').classList.remove('border-blue-500', 'text-blue-400');
                document.getElementById('btn-tab-dashboard').classList.add('border-transparent', 'text-slate-400');
                loadLogs();
            }
            lucide.createIcons();
        }

        async function loadLogs() {
            try {
                const res = await fetch('/logs');
                const logs = await res.json();
                const container = document.getElementById('logs-container');
                const isAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 50;

                if (logs.length === 0) {
                    container.innerHTML = '<div class="text-slate-500 italic">No logs available yet.</div>';
                    return;
                }

                container.innerHTML = logs.map(log => {
                    let levelClass = 'text-slate-400';
                    let msgClass = 'text-slate-300';
                    const level = log.level.trim();
                    if (level === 'ERROR' || level === 'CRITICAL') { levelClass = 'text-red-500'; msgClass = 'text-red-400'; }
                    else if (level === 'WARNING') { levelClass = 'text-yellow-500'; msgClass = 'text-yellow-300'; }
                    else if (level === 'DEBUG') { levelClass = 'text-slate-600'; msgClass = 'text-slate-500'; }

                    return `<div class="pb-0.5 leading-tight hover:bg-white/5 transition-colors">
                        <span class="text-[9px] text-slate-500 font-mono opacity-50 select-none">${log.timestamp}</span>
                        <span class="text-[9px] font-bold px-1 ${levelClass} font-mono">${log.level}</span>
                        <span class="${msgClass} ml-1 whitespace-pre-wrap break-all">${escapeHtml(log.message)}</span>
                    </div>`;
                }).join('');

                if (isAtBottom) {
                    container.scrollTop = container.scrollHeight;
                }
            } catch (err) {
                console.error("Failed to load logs", err);
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function clearLogsUI() {
            document.getElementById('logs-container').innerHTML = '<div class="text-slate-500 italic">Logs cleared (view only).</div>';
        }

        function setTimeRange(minutes) {
            currentTimeRange = minutes;
            document.querySelectorAll('.time-range-btn').forEach(btn => {
                btn.classList.remove('bg-blue-600', 'text-white');
                btn.classList.add('text-slate-400', 'hover:text-white');
            });
            const activeBtn = document.getElementById(`btn-${minutes}`);
            activeBtn.classList.add('bg-blue-600', 'text-white');
            activeBtn.classList.remove('text-slate-400', 'hover:text-white');
            loadStats();
        }

        async function loadStats(force = false) {
            try {
                const [statsRes, queriesRes, minuteRes, keysRes, dailyRes, healthRes] = await Promise.all([
                    fetch('/stats'),
                    fetch('/queries'),
                    fetch(`/stats/minute?window=${currentTimeRange}`),
                    fetch('/health/keys' + (force ? '?force=true' : '')),
                    fetch('/stats/24h'),
                    fetch('/health')
                ]);

                if (!statsRes.ok || !queriesRes.ok || !minuteRes.ok || !keysRes.ok || !dailyRes.ok || !healthRes.ok) {
                    throw new Error("One or more requests failed");
                }

                const stats = await statsRes.json();
                const queries = await queriesRes.json();
                const minuteData = await minuteRes.json();
                const keysData = await keysRes.json();
                const dailyData = await dailyRes.json();
                const healthData = await healthRes.json();

                if (!Array.isArray(stats) || !Array.isArray(queries) || !Array.isArray(minuteData) || !Array.isArray(dailyData)) {
                    console.error("Received non-array data from API", { stats, queries, minuteData, dailyData });
                    return;
                }

                // Update Health Indicators
                const proxyDot = document.getElementById('proxy-status-dot');
                const cloudDot = document.getElementById('cloud-status-dot');

                if (healthData.status === 'ok') {
                    proxyDot.className = 'w-2 h-2 rounded-full bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]';
                } else {
                    proxyDot.className = 'w-2 h-2 rounded-full bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.6)]';
                }

                if (healthData.ollama_cloud === 'reachable') {
                    cloudDot.className = 'w-2 h-2 rounded-full bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]';
                } else if (healthData.ollama_cloud === 'error') {
                    cloudDot.className = 'w-2 h-2 rounded-full bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.6)]';
                } else {
                    cloudDot.className = 'w-2 h-2 rounded-full bg-yellow-500 shadow-[0_0_8px_rgba(234,179,8,0.6)]';
                }

                const keysContainer = document.getElementById('keys-container');
                const keysContainerMore = document.getElementById('keys-container-more');
                const showMoreBtn = document.getElementById('show-more-keys-btn');
                const keysEntries = Object.entries(keysData);
                const limit = 6;
                const showCollapse = keysEntries.length > limit;

                const renderKeyCard = ([keyId, info]) => {
                    const idx = keyId.split('_')[1];
                    const isPenalized = info.penalty_active;
                    const statusColor = isPenalized ? 'text-red-400' : (info.status.includes('OK') ? 'text-green-400' : 'text-yellow-400');

                    let penaltyInfo = '';
                    if (isPenalized || info.backoff_level > 0) {
                        const timeStr = info.expires_in > 0 ?
                            (info.expires_in > 60 ? Math.ceil(info.expires_in/60) + 'm' : info.expires_in + 's') : 'Ready';
                        penaltyInfo = `
                            <div class="mt-2 pt-2 border-t border-slate-700/50">
                                <div class="flex justify-between items-center text-[10px]">
                                    <span class="text-slate-500 uppercase">Backoff Lvl ${info.backoff_level}</span>
                                    <span class="text-orange-400 font-mono">${timeStr}</span>
                                </div>
                            </div>
                        `;
                    }

                    return `
                        <div class="bg-slate-800/50 p-4 rounded-lg border ${isPenalized ? 'border-red-500/50' : 'border-slate-700'} transition-all">
                            <div class="flex justify-between items-start mb-2">
                                <span class="font-bold text-slate-300">${keyId}</span>
                                <div class="flex gap-2 items-center">
                                    ${!isPenalized ? `
                                    <button onclick="penalizeKey(${idx})" class="p-1 hover:bg-slate-700 rounded text-slate-500 hover:text-orange-400 transition" title="Pause / Penalize Key">
                                        <i data-lucide="pause-circle" size="12"></i>
                                    </button>
                                    ` : ''}
                                    <button onclick="resetKey(${idx})" class="p-1 hover:bg-slate-700 rounded text-slate-500 hover:text-white transition" title="Reset Penalty">
                                        <i data-lucide="rotate-ccw" size="12"></i>
                                    </button>
                                    <span class="text-xs ${statusColor} px-2 py-0.5 rounded bg-slate-900">${info.status}</span>
                                </div>
                            </div>
                            <div class="text-xs text-slate-400">
                                <div>Usage (2h): <span class="text-blue-400">${info.usage_2h.toLocaleString()}</span> tokens</div>
                                ${isPenalized ? `<div class="text-red-400 mt-1 flex items-center gap-1 font-medium"><i data-lucide="alert-circle" size="12"></i> Rate Limited</div>` : ''}
                                ${penaltyInfo}
                            </div>
                        </div>
                    `;
                };

                keysContainer.innerHTML = keysEntries.slice(0, showCollapse ? limit : keysEntries.length).map(renderKeyCard).join('');

                if (showCollapse) {
                    keysContainerMore.innerHTML = keysEntries.slice(limit).map(renderKeyCard).join('');
                    showMoreBtn.classList.remove('hidden');
                    document.getElementById('show-more-text').textContent = document.getElementById('keys-collapsed-container').classList.contains('hidden')
                        ? `Show ${keysEntries.length - limit} more keys`
                        : 'Show less';
                } else {
                    showMoreBtn.classList.add('hidden');
                    document.getElementById('keys-collapsed-container').classList.add('hidden');
                }

                const statsBody = document.getElementById('stats-body');
                if (stats.length === 0) {
                    statsBody.innerHTML = '<tr><td colspan="5" class="px-4 py-8 text-center text-slate-500">No statistics available yet</td></tr>';
                } else {
                    statsBody.innerHTML = stats.map(s => {
                        const localTime = new Date(s.bucket).toLocaleString();
                        return `
                        <tr class="border-b border-slate-700 hover:bg-slate-800/50">
                            <td class="px-4 py-3">${localTime}</td>
                            <td class="px-4 py-3 text-slate-400">${s.client_ip}</td>
                            <td class="px-4 py-3 font-mono">${s.model}</td>
                            <td class="px-4 py-3 text-right">${s.requests}</td>
                            <td class="px-4 py-3 text-right text-blue-400">${s.prompt_tokens + s.completion_tokens}</td>
                        </tr>
                        `;
                    }).join('');
                }

                const queriesBody = document.getElementById('queries-body');
                if (queries.length === 0) {
                    queriesBody.innerHTML = '<tr><td colspan="5" class="px-4 py-8 text-center text-slate-500">No queries found</td></tr>';
                } else {
                    queriesBody.innerHTML = queries.map(q => {
                        // DB timestamp is CURRENT_TIMESTAMP (UTC), but SQLite doesn't append 'Z'
                        const timestamp = q.timestamp.includes('Z') ? q.timestamp : q.timestamp + 'Z';
                        const localTime = new Date(timestamp).toLocaleString();
                        return `
                        <tr class="border-b border-slate-700 hover:bg-slate-800/50">
                            <td class="px-4 py-3 whitespace-nowrap">${localTime}</td>
                            <td class="px-4 py-3 text-slate-400">${q.client_ip}</td>
                            <td class="px-4 py-3 font-mono">${q.model}</td>
                            <td class="px-4 py-3 text-blue-400">${q.prompt_tokens + q.completion_tokens}</td>
                            <td class="px-4 py-3 text-right">
                                <button onclick="viewBody(${q.id})" class="text-blue-400 hover:underline">View</button>
                            </td>
                        </tr>
                        `;
                    }).join('');
                }

                updateChart(minuteData);
                updateSparkline(dailyData);
                lucide.createIcons();
            } catch (err) {
                console.error("Failed to load dashboard data", err);
            }
        }

        const colors = [
            '#60a5fa', '#34d399', '#a78bfa', '#fbbf24', '#f87171', '#22d3ee', '#fb7185'
        ];

        function updateChart(data) {
            const ctx = document.getElementById('usageChart').getContext('2d');

            const models = [...new Set(data.map(d => d.model))];
            const labels = [];

            // Prepare datasets for models
            const modelDatasets = models.map((model, idx) => ({
                label: model,
                data: [],
                borderColor: colors[idx % colors.length],
                backgroundColor: colors[idx % colors.length] + '44',
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                yAxisID: 'y',
                stack: 'models' // Stack models together
            }));

            const totalData = [];
            const now = new Date();
            const step = Math.max(1, Math.floor(currentTimeRange / 60)); // Show fewer points for longer ranges if needed, but here we keep minutes

            for (let i = currentTimeRange - 1; i >= 0; i--) {
                const d = new Date(now.getTime() - i * 60000);

                // Construct ISO string for matching (YYYY-MM-DDTHH:MM:00Z)
                const isoStr = d.toISOString().substring(0, 16) + ":00Z";

                // For display labels in local time
                const timeStrDisplay = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');

                const labelFreq = currentTimeRange <= 10 ? 2 : (currentTimeRange <= 120 ? 10 : (currentTimeRange <= 360 ? 30 : 60));
                if (i % labelFreq === 0 || i === currentTimeRange - 1 || i === 0) {
                    labels.push(timeStrDisplay);
                } else {
                    labels.push("");
                }

                let minuteTotal = 0;
                models.forEach((model, idx) => {
                    const entry = data.find(e => e.minute === isoStr && e.model === model);
                    const val = entry ? entry.total_tokens : 0;
                    modelDatasets[idx].data.push(val);
                    minuteTotal += val;
                });
                totalData.push(minuteTotal);
            }

            const totalDataset = {
                label: 'Total Sum',
                data: totalData,
                borderColor: '#ffffff',
                borderWidth: 2,
                borderDash: [5, 5],
                fill: false,
                tension: 0.4,
                pointRadius: 0,
                yAxisID: 'yTotal' // Use separate axis to avoid double stacking
            };

            const finalDatasets = [...modelDatasets, totalDataset];

            if (usageChart) {
                usageChart.data.labels = labels;
                usageChart.data.datasets = finalDatasets;
                usageChart.update();
            } else {
                usageChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: finalDatasets
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: 'index', intersect: false },
                        plugins: {
                            legend: {
                                display: true,
                                position: 'top',
                                labels: { color: '#94a3b8', boxWidth: 12 }
                            }
                        },
                        scales: {
                            y: {
                                stacked: true,
                                beginAtZero: true,
                                grid: { color: '#334155' },
                                ticks: { color: '#94a3b8' },
                                title: { display: true, text: 'Tokens (Stacked)', color: '#64748b' }
                            },
                            yTotal: {
                                display: false,
                                stacked: false,
                                beginAtZero: true,
                                // Sync this with y axis scale to ensure total line is visible
                                max: Math.max(...totalData) * 1.1
                            },
                            x: {
                                grid: { display: false },
                                ticks: { color: '#94a3b8', maxTicksLimit: 10 }
                            }
                        }
                    }
                });
            }
        }

        async function resetKey(idx) {
            try {
                const res = await fetch(`/health/keys/${idx}/reset`, { method: 'POST' });
                if (!res.ok) {
                    const error = await res.json();
                    alert("Reset failed: " + (error.detail || res.statusText));
                }
                loadStats();
            } catch (err) {
                console.error("Failed to reset key", err);
                alert("Network error while resetting key");
            }
        }

        async function penalizeKey(idx) {
            try {
                const res = await fetch(`/health/keys/${idx}/penalize`, { method: 'POST' });
                if (!res.ok) {
                    const error = await res.json();
                    alert("Penalize failed: " + (error.detail || res.statusText));
                }
                loadStats();
            } catch (err) {
                console.error("Failed to penalize key", err);
                alert("Network error while penalizing key");
            }
        }

        async function viewBody(id) {
            try {
                const res = await fetch('/queries/' + id + '/body');
                const data = await res.json();
                document.getElementById('body-content').textContent = JSON.stringify(data, null, 2);
                document.getElementById('body-viewer').classList.remove('hidden');
            } catch (err) {
                alert("Failed to load body");
            }
        }

        function updateSparkline(data) {
            const total = data.reduce((sum, d) => sum + d.total_tokens, 0);
            document.getElementById('total-tokens-count').textContent = total.toLocaleString();

            const ctx = document.getElementById('sparklineChart').getContext('2d');
            const labels = data.map(d => new Date(d.hour_bucket).toLocaleString());
            const values = data.map(d => d.total_tokens);

            if (sparklineChart) {
                sparklineChart.data.labels = labels;
                sparklineChart.data.datasets[0].data = values;
                sparklineChart.update();
            } else {
                sparklineChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            data: values,
                            borderColor: '#60a5fa',
                            backgroundColor: 'rgba(96, 165, 250, 0.2)',
                            fill: true,
                            tension: 0.4,
                            pointRadius: 0,
                            borderWidth: 2
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false }, tooltip: { enabled: false } },
                        scales: {
                            x: { display: false },
                            y: { display: false, beginAtZero: true }
                        }
                    }
                });
            }
        }

        function toggleMoreKeys() {
            const container = document.getElementById('keys-collapsed-container');
            const btnText = document.getElementById('show-more-text');
            const btnIcon = document.getElementById('show-more-icon');
            const isHidden = container.classList.toggle('hidden');

            const totalMore = document.getElementById('keys-container-more').children.length;
            btnText.textContent = isHidden ? `Show ${totalMore} more keys` : 'Show less';

            if (isHidden) {
                btnIcon.setAttribute('data-lucide', 'chevron-down');
            } else {
                btnIcon.setAttribute('data-lucide', 'chevron-up');
            }
            lucide.createIcons();
        }

        function closeViewer() {
            document.getElementById('body-viewer').classList.add('hidden');
        }

        loadStats();
        // Refresh every 10 seconds
        setInterval(() => {
            loadStats();
            if (currentTab === 'logs') loadLogs();
        }, 10000);
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content.replace("{APP_VERSION}", APP_VERSION))


@app.get("/ratelimits")
async def get_ratelimits(authorization: Optional[str] = Header(None)):
    """Returns the latest captured rate limit headers for all keys."""
    await verify_auth(authorization)
    return rate_limit_store


@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(None)):
    """OpenAI-compatible models endpoint."""
    return await _handle_proxy(None, "v1/models", authorization)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_ollama(
    request: Request, path: str, authorization: Optional[str] = Header(None)
):
    return await _handle_proxy(request, path, authorization)


async def _handle_proxy(
    request: Optional[Request], path: str, authorization: Optional[str] = Header(None)
):
    print(
        f"DEBUG [Entry]: {request.method if request else 'N/A'} /{path} from {request.client.host if request and request.client else 'unknown'}"
    )
    # 1. Verify access to this proxy
    try:
        await verify_auth(authorization)
    except Exception as e:
        print(f"DEBUG [Auth Failed]: {e}")
        raise

    # 2. Prepare request to Ollama Cloud
    # Ollama Cloud API base is https://ollama.com
    clean_path = path
    if not (path.startswith("v1/") or path.startswith("api/")):
        if path == "api" or path == "":
            clean_path = "api"
        else:
            clean_path = f"api/{path}"

    url = f"{OLLAMA_CLOUD_URL}/{clean_path}".rstrip("/")
    method = request.method if request else "GET"
    content = await request.body() if request else None
    params = request.query_params if request else None

    # Get client IP, considering potential proxies
    if request:
        client_ip = request.headers.get("X-Forwarded-For", request.client.host)
    else:
        client_ip = "127.0.0.1"

    if "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()

    # 2.5 Immediately store request body and create log entry
    print(
        f"DEBUG [Prep]: Target URL: {url}, Content-Length: {len(content) if content else 0}"
    )
    file_path = store_request_file(client_ip, content or b"")
    request_id = create_request_log(
        client_ip=client_ip, method=method, endpoint=clean_path, file_path=file_path
    )

    async def log_stream_usage(response_iter, k_index, c_ip, request_id):
        print(f"DEBUG [Stream]: Started for Key {k_index}")
        # Buffer the tail of the response to ensure we can parse the final JSON
        # Even if it's split across multiple network chunks.
        tail_buffer = b""
        max_tail_size = 4096  # 4KB is plenty for the final stats JSON

        async for chunk in response_iter:
            yield chunk
            tail_buffer = (tail_buffer + chunk)[-max_tail_size:]

        # After stream finishes, try to extract stats from the accumulated tail
        try:
            # Ollama sends newline-delimited JSON or a single JSON object.
            # We look for the last complete JSON object in the tail.
            decoded_tail = tail_buffer.decode(errors="ignore").strip()
            lines = decoded_tail.split("\n")

            # Iterate backwards to find the last valid JSON with stats
            for line in reversed(lines):
                line = line.strip()
                if not line or not (line.startswith("{") and line.endswith("}")):
                    continue

                try:
                    data = json.loads(line)
                    # For both streaming (done=True) and non-streaming responses
                    if data.get("done") or "eval_count" in data:
                        model_name = data.get("model", "unknown")
                        prompt_tokens = data.get("prompt_eval_count", 0)
                        completion_tokens = data.get("eval_count", 0)

                        # Record usage in existing table
                        record_usage(
                            c_ip,
                            k_index,
                            model_name,
                            prompt_tokens,
                            completion_tokens,
                        )

                        # Update existing request metadata
                        if request_id is not None:
                            update_request_log(
                                request_id=request_id,
                                model=model_name,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                            )
                        break
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            print(f"DEBUG [Stream Error]: {e}")
            traceback.print_exc()

    # 3. Handle Streaming or normal response with retry logic for 429
    attempted_indices = set()
    last_exception = None

    for attempt in range(len(OLLAMA_API_KEYS)):
        available_indices = [
            i for i in range(len(OLLAMA_API_KEYS)) if i not in attempted_indices
        ]
        if not available_indices:
            print("No more available indices for retry.")
            break

        current_key_index = get_best_key_index(exclude=attempted_indices)
        if current_key_index is None or current_key_index in attempted_indices:
            current_key_index = available_indices[0]

        attempted_indices.add(current_key_index)
        current_key = OLLAMA_API_KEYS[current_key_index]
        print(f"DEBUG [Attempt {attempt + 1}]: Selected Key Index {current_key_index}")

        headers = {
            "Authorization": f"Bearer {current_key}",
            "Content-Type": request.headers.get("Content-Type", "application/json")
            if request
            else "application/json",
        }

        response = None
        try:
            print(f"DEBUG [Attempt {attempt + 1}]: Sending request to {url}...")
            req = http_client.build_request(
                method, url, content=content, params=params, headers=headers
            )
            response = await http_client.send(req, stream=True)
            print(
                f"DEBUG [Attempt {attempt + 1}]: Received status {response.status_code}"
            )
            # Log headers for debugging (sensitive ones like Auth are not in response headers anyway)
            # print(f"DEBUG [Attempt {attempt + 1}]: Response Headers: {dict(response.headers)}")

            # If quota exceeded, penalize and retry if possible
            if response.status_code == 429:
                now = time.time()
                current_level = key_backoff_levels.get(current_key_index, -1) + 1
                level = min(current_level, len(BACKOFF_STAGES) - 1)
                key_backoff_levels[current_key_index] = level

                reset_after = BACKOFF_STAGES[level]
                if "x-ratelimit-reset" in response.headers:
                    try:
                        header_reset = int(response.headers["x-ratelimit-reset"])
                        reset_after = max(reset_after, header_reset)
                    except:
                        pass

                key_penalty_box[current_key_index] = now + reset_after
                print(
                    f"Key {current_key_index} (attempt {attempt + 1}) exceeded quota (429). Level {level}, Penalized for {reset_after}s."
                )

                if attempt < len(OLLAMA_API_KEYS) - 1:
                    await response.aclose()
                    continue

            # If server error (500, 502, 503, 504), penalize briefly and retry
            elif response.status_code in [500, 502, 503, 504]:
                now = time.time()
                # Progressive backoff for 50x errors
                current_level = key_backoff_levels_50x.get(current_key_index, -1) + 1
                level = min(current_level, len(BACKOFF_STAGES_50X) - 1)
                key_backoff_levels_50x[current_key_index] = level

                reset_after = BACKOFF_STAGES_50X[level]
                key_penalty_box[current_key_index] = now + reset_after

                print(
                    f"Key {current_key_index} (attempt {attempt + 1}) encountered upstream error ({response.status_code}). Level {level}, Penalized for {reset_after}s."
                )
                if attempt < len(OLLAMA_API_KEYS) - 1:
                    print(
                        f"DEBUG [Attempt {attempt + 1}]: Retrying due to 50x error..."
                    )
                    await response.aclose()
                    continue

            # Capture rate limit headers
            rl_headers = {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower().startswith("x-ratelimit-")
            }
            if rl_headers:
                rate_limit_store[f"key_{current_key_index}"] = rl_headers

            # Return the response
            print(
                f"DEBUG [Attempt {attempt + 1}]: Returning StreamingResponse with status {response.status_code}"
            )
            return StreamingResponse(
                log_stream_usage(
                    response.aiter_raw(),
                    current_key_index,
                    client_ip,
                    request_id,
                ),
                status_code=response.status_code,
                headers=dict(response.headers),
            )
        except Exception as e:
            last_exception = e
            print(
                f"CRITICAL: Key {current_key_index} (attempt {attempt + 1}) failed with exception: {e}"
            )
            traceback.print_exc()

            if response:
                try:
                    await response.aclose()
                except:
                    pass

            if attempt < len(OLLAMA_API_KEYS) - 1:
                continue
            break

    # If we reached here, all attempts failed with exceptions or exhausted keys
    print(f"DEBUG [Final]: All attempts failed. Last exception: {last_exception}")
    if last_exception:
        error_detail = str(last_exception)
        status_code = 500
    else:
        error_detail = "All API keys exhausted, rate-limited, or returned errors"
        status_code = 503  # Service Unavailable is more appropriate than 500

    raise HTTPException(status_code=status_code, detail=error_detail)


if __name__ == "__main__":
    import uvicorn

    # Configure uvicorn log format to include timestamps
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = (
        '%(asctime)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s'
    )
    log_config["formatters"]["default"]["fmt"] = (
        "%(asctime)s - %(levelname)s - %(message)s"
    )

    uvicorn.run(app, host="0.0.0.0", port=11434, log_config=log_config)
