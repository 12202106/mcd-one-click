"""Public deployment settings. Credentials come only from the environment or a private file."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()
BASE = "/mcp-mcd"
SERVERLESS = os.getenv("MCD_SERVERLESS", "0") == "1" or bool(os.getenv("VERCEL"))


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise RuntimeError("Use an origin such as https://your-project.vercel.app, without a path")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Public origins must use HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


ORIGIN = origin(os.getenv("PUBLIC_ORIGIN", "https://localhost:8013"))
INTERNAL_ORIGIN = origin(os.environ["INTERNAL_ORIGIN"]) if os.getenv("INTERNAL_ORIGIN") else ORIGIN
ALLOWED_ORIGINS = {ORIGIN} | {
    origin(item.strip()) for item in os.getenv("MCD_CORS_ORIGINS", "").split(",") if item.strip()
}
ALLOWED_HOSTS = {urlsplit(ORIGIN).netloc, urlsplit(INTERNAL_ORIGIN).netloc,
                 "127.0.0.1:8013", "localhost:8013"}


def credentials():
    value = {}
    # Optional compatibility with a private config on a persistent server.
    if os.getenv("MCD_GATEWAY_CONFIG"):
        value = json.loads(Path(os.environ["MCD_GATEWAY_CONFIG"]).read_text())
    if os.getenv("MCD_GATEWAY_TOKEN"):
        value["gateway_token"] = os.environ["MCD_GATEWAY_TOKEN"]
    if os.getenv("MCD_UPSTREAM_TOKEN"):
        value["upstream_headers"] = {"Authorization": "Bearer " + os.environ["MCD_UPSTREAM_TOKEN"]}
    token = value.get("gateway_token", "")
    if not isinstance(token, str) or len(token) < 32 or not value.get("upstream_headers"):
        raise RuntimeError("Configure MCD_GATEWAY_TOKEN (32+ characters) and MCD_UPSTREAM_TOKEN")
    return value
