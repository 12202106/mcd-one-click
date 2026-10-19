"""Offline serverless contract tests. These are NOT cloud deployment tests."""
import concurrent.futures
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import httpx

import gateway
import settings
import spark_auth
import storage
import test_spark

REDIS_ENV = {"UPSTASH_REDIS_REST_URL": "https://redis.example", "UPSTASH_REDIS_REST_TOKEN": "dummy-redis-token"}


class FakeRedisREST:
    """An in-memory protocol stub shared by several Store objects, never a real Redis test."""
    def __init__(self):
        self.records = {}
        self.commands = []
        self.lock = threading.Lock()

    def post(self, url, *, json, headers, timeout, follow_redirects):
        assert url == "https://redis.example"
        assert headers["Authorization"] == "Bearer dummy-redis-token"
        assert not follow_redirects
        command, key, *args = json
        with self.lock:
            self.commands.append(json)
            old = self.records.get(key)
            if old and old[1] <= time.time():
                self.records.pop(key)
            if command == "SET":
                value, ex, ttl = args
                assert ex == "EX" and ttl > 0
                self.records[key] = (value, time.time() + ttl)
                result = "OK"
            elif command == "GETDEL":
                result = self.records.pop(key, (None, 0))[0]
            elif command == "GET":
                result = self.records.get(key, (None, 0))[0]
            else:
                raise AssertionError("Unexpected Redis command")
        return httpx.Response(200, json={"result": result}, request=httpx.Request("POST", url))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRedisREST()
        self.env = patch.dict(os.environ, REDIS_ENV)
        self.http = patch.object(storage.httpx, "post", side_effect=self.fake.post)
        self.env.start()
        self.http.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.http.stop)

    def test_consume_is_atomic_across_store_instances_and_keys_are_hashed(self):
        first, second = spark_auth.Store(), spark_auth.Store()
        first.put("code", "dummy-authorization-code", {"client_id": "test"}, time.time()+100)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: second.get("code", "dummy-authorization-code", consume=True), range(20)))
        self.assertEqual(sum(value is not None for value in values), 1)
        self.assertNotIn("dummy-authorization-code", self.fake.commands[0][1])
        self.assertTrue(all(command[0] == "GETDEL" for command in self.fake.commands[1:]))

    def test_expired_record_and_namespace_are_isolated(self):
        first = storage.RedisStore()
        first.put("code", "test", {"x": 1}, time.time()+100)
        with patch.dict(os.environ, {"MCD_STATE_NAMESPACE": "other-instance"}):
            self.assertIsNone(storage.RedisStore().get("code", "test"))
        with patch.object(storage.time, "time", return_value=time.time()+101):
            self.assertIsNone(first.get("code", "test"))

    def test_storage_failures_are_sanitized_and_not_retried(self):
        with patch.object(storage.httpx, "post", side_effect=RuntimeError("dummy-secret-in-vendor-error")) as post:
            with self.assertRaises(storage.StorageUnavailable) as raised:
                storage.RedisStore().get("oneclick_pending", "test", consume=True)
            self.assertNotIn("dummy-secret", str(raised.exception))
            post.assert_called_once()

    def test_serverless_refuses_ephemeral_sqlite_and_partial_redis_configuration(self):
        with patch.object(spark_auth, "SERVERLESS", True):
            with patch.dict(os.environ, {"UPSTASH_REDIS_REST_URL": "", "UPSTASH_REDIS_REST_TOKEN": ""}):
                with self.assertRaisesRegex(RuntimeError, "persistent"):
                    spark_auth.Store()
            with patch.dict(os.environ, {"UPSTASH_REDIS_REST_TOKEN": ""}):
                with self.assertRaisesRegex(RuntimeError, "both"):
                    spark_auth.Store()

    def test_config_rejects_non_origins_and_short_gateway_token(self):
        for bad in ["https://user:password@example.com", "https://example.com/path", "https://example.com?token=x", "http://example.com"]:
            with self.assertRaises(RuntimeError):
                settings.origin(bad)
        with patch.dict(os.environ, {"MCD_GATEWAY_TOKEN": "short", "MCD_UPSTREAM_TOKEN": "dummy", "MCD_GATEWAY_CONFIG": ""}):
            with self.assertRaises(RuntimeError):
                settings.credentials()


class ServerlessSparkTests(test_spark.SparkTests):
    """Re-run the complete OAuth/PKCE contract with fake Redis and no ASGI lifespan."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake = FakeRedisREST()
        for item in [patch.dict(os.environ, REDIS_ENV),
                     patch.object(storage.httpx, "post", side_effect=self.fake.post),
                     patch.object(spark_auth, "SERVERLESS", True),
                     patch.object(gateway, "SERVERLESS", True)]:
            item.start()
            self.addCleanup(item.stop)
        store = spark_auth.Store()
        for item in [patch.object(gateway.spark_provider, "store", store), patch.object(gateway.one_click, "store", store)]:
            item.start()
            self.addCleanup(item.stop)

    async def test_json_endpoint_and_metadata_work_without_lifespan(self):
        metadata = (await self.client.get("/.well-known/oauth-protected-resource/mcp-mcd/spark")).json()
        self.assertEqual(metadata["resource"], settings.ORIGIN + "/mcp-mcd/spark")
        headers = {"Authorization": "Bearer owner-secret"}
        result = await self.client.post("/mcp-mcd/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(result.json()["result"]["tools"][0]["name"], "mcd_one_click")
        self.assertEqual((await self.client.get("/mcp-mcd/sse", headers=headers)).status_code, 405)

    async def test_pages_cors_preflight_and_credential_isolation(self):
        # Production origins come from MCD_CORS_ORIGINS; use an isolated wrapper here.
        from starlette.middleware.cors import CORSMiddleware
        origin = "https://example-user.github.io"
        app = CORSMiddleware(gateway.SafeAudit(gateway.BoundedRequests()), allow_origins=[origin],
                             allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
        with patch.object(gateway, "ALLOWED_ORIGINS", gateway.ALLOWED_ORIGINS | {origin}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.ORIGIN) as client:
                response = await client.options("/mcp-mcd/api/tools", headers={"Origin": origin,
                    "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
                result = await client.get("/mcp-mcd/api/tools", headers={"Origin": origin})
                self.assertEqual(result.status_code, 401)
                self.assertNotIn("Access-Control-Allow-Credentials", result.headers)
                self.assertEqual((await client.options("/mcp-mcd/api/tools", headers={"Origin": "https://evil.example",
                    "Access-Control-Request-Method": "GET"})).status_code, 400)

    async def test_storage_failure_returns_controlled_503_without_write(self):
        with patch.object(storage.httpx, "post", side_effect=RuntimeError("dummy-redis-secret")):
            response = await self.client.post("/mcp-mcd/spark", headers={"Authorization": "Bearer dummy-oauth-access-token"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("dummy-redis-secret", response.text)


if __name__ == "__main__":
    unittest.main()
