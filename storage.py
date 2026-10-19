"""Optional persistent Redis REST backend; GETDEL enforces one-use grants across instances."""
import hashlib
import json
import math
import os
import time
from urllib.parse import urlsplit

import httpx


class StorageUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Persistent state unavailable. Do not automatically retry account changes.")


class RedisStore:
    def __init__(self):
        self.url = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
        parsed = urlsplit(self.url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("UPSTASH_REDIS_REST_URL must be an HTTPS origin")
        self.token = os.environ["UPSTASH_REDIS_REST_TOKEN"]
        self.namespace = os.getenv("MCD_STATE_NAMESPACE", "mcd-one-click")

    def key(self, kind, key):
        return f"{self.namespace}:{kind}:{hashlib.sha256(key.encode()).hexdigest()}"

    def command(self, *args):
        # Commands and credentials stay in the body/header; never in a URL or log.
        # No automatic retry: GETDEL may already have committed when a reply is lost.
        try:
            response = httpx.post(self.url, json=list(args),
                                  headers={"Authorization": "Bearer " + self.token}, timeout=8,
                                  follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
                raise ValueError()
            return payload["result"]
        except Exception:
            raise StorageUnavailable() from None

    def put(self, kind, key, data, expires):
        ttl = math.floor(expires - time.time())
        if ttl <= 0:
            return
        envelope = json.dumps({"expires": expires, "data": data}, ensure_ascii=True)
        if self.command("SET", self.key(kind, key), envelope, "EX", ttl) != "OK":
            raise StorageUnavailable()

    def get(self, kind, key, consume=False):
        value = self.command("GETDEL" if consume else "GET", self.key(kind, key))
        if value is None:
            return None
        try:
            envelope = json.loads(value)
            return envelope["data"] if envelope["expires"] > time.time() else None
        except (ValueError, TypeError, KeyError):
            raise StorageUnavailable() from None
