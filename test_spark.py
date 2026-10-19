"""Spark's HTTP/OAuth contract, exercised without external services or account changes."""
import base64
import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import types

import gateway
import spark_auth


class SparkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(spark_auth, "STATE", Path(self.tmp.name) / "oauth.db")
        self.db.start()
        self.cfg = patch.object(gateway, "config", return_value={
            "gateway_token": "owner-secret", "upstream_headers": {"Authorization": "Bearer upstream-secret"},
        })
        self.cfg.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url=spark_auth.ORIGIN)
        self.redirect = "https://oauth-redirect.googleusercontent.com/r/test-spark"

    async def asyncTearDown(self):
        await self.client.aclose()
        self.cfg.stop()
        self.db.stop()
        self.tmp.cleanup()

    async def register(self):
        r = await self.client.post("/mcp-mcd/oauth/register", json={
            "client_name": "Google", "redirect_uris": [self.redirect],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["client_id"]

    async def authorize(self, client_id):
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = await self.client.get("/mcp-mcd/oauth/authorize", params={
            "client_id": client_id, "redirect_uri": self.redirect, "response_type": "code",
            "state": "test-state", "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": spark_auth.RESOURCE,
        })
        self.assertEqual(r.status_code, 302, r.text)
        r = await self.client.get(r.headers["location"])
        self.assertEqual(r.status_code, 200)
        self.assertIn("oauth-redirect.googleusercontent.com", r.headers["content-security-policy"])
        self.assertEqual(r.headers["referrer-policy"], "same-origin")
        self.assertNotIn("owner-secret", r.text)
        values = {name: re.search('name="' + name + '" value="([^"]+)"', r.text).group(1) for name in ["ticket", "csrf"]}
        return verifier, values

    async def grant(self):
        client_id = await self.register()
        verifier, values = await self.authorize(client_id)
        r = await self.client.post("/mcp-mcd/oauth/consent", data={**values, "decision": "approve", "owner_token": "owner-secret"})
        self.assertEqual(r.status_code, 303, r.text)
        query = parse_qs(urlsplit(r.headers["location"]).query)
        self.assertEqual(query["state"], ["test-state"])
        data = {"grant_type": "authorization_code", "client_id": client_id, "code": query["code"][0],
                "redirect_uri": self.redirect, "code_verifier": verifier, "resource": spark_auth.RESOURCE}
        return client_id, data

    async def test_discovery_and_http_without_sse(self):
        r = await self.client.head("/mcp-mcd/spark")
        self.assertEqual(r.status_code, 401)
        self.assertIn(spark_auth.PRM_PATH, r.headers["www-authenticate"])
        for path in [spark_auth.PRM_PATH, "/.well-known/oauth-protected-resource", spark_auth.ASM_PATH, "/.well-known/oauth-authorization-server"]:
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
        headers = {"Authorization": "Bearer owner-secret", "Accept": "application/json"}
        r = await self.client.post("/mcp-mcd/spark", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r.headers["content-type"], "application/json")
        self.assertNotIn("mcp-session-id", r.headers)
        r = await self.client.get("/mcp-mcd/spark", headers=headers)
        self.assertEqual(r.status_code, 405)
        with patch.object(gateway, "invoke", new_callable=AsyncMock) as invoke:
            r = await self.client.post("/mcp-mcd/spark", headers=headers, json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "mcd_one_click", "arguments": {"action": "run"}}})
            self.assertEqual(r.status_code, 202)
            invoke.assert_not_called()

    async def test_only_one_tool_is_exposed_without_upstream_lookup(self):
        with patch.object(gateway, "get_tools", new_callable=AsyncMock) as listing:
            r = await self.client.post("/mcp-mcd/spark", headers={"Authorization": "Bearer owner-secret"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            tools = r.json()["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["mcd_one_click"])
            self.assertFalse(tools[0]["annotations"]["readOnlyHint"])
            listing.assert_not_called()

    async def test_old_tool_names_are_not_callable(self):
        for name in ["query-nearby-stores", "delivery-query-addresses", "mcd_read_batch", "create-order"]:
            with self.subTest(name=name), patch.object(gateway, "invoke", new_callable=AsyncMock) as invoke:
                r = await self.client.post("/mcp-mcd/spark", headers={"Authorization": "Bearer owner-secret"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}})
                self.assertEqual(r.json()["error"]["code"], -32601)
                invoke.assert_not_called()

    async def test_protocol_audit_does_not_record_credentials_or_business_data(self):
        response = types.CallToolResult(content=[types.TextContent(type="text", text="private-result")])
        with patch.object(gateway.one_click, "call", new_callable=AsyncMock, return_value=response):
            with self.assertLogs("mcd.audit", level="INFO") as audit:
                r = await self.client.post("/mcp-mcd/spark?secret=private-query", headers={"Authorization": "Bearer owner-secret"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mcd_one_click", "arguments": {"action": "run", "payload_json": "private-argument"}}})
        self.assertEqual(r.status_code, 200)
        log = " ".join(audit.output)
        self.assertIn('"rpc": "tools/call"', log)
        for value in ["owner-secret", "private-query", "private-argument", "private-result"]:
            self.assertNotIn(value, log)

    async def test_pkce_single_use_refresh_revoke_and_persistence(self):
        client_id, data = await self.grant()
        r = await self.client.post("/mcp-mcd/oauth/token", data={**data, "code_verifier": "wrong"})
        self.assertEqual(r.status_code, 400)
        r = await self.client.post("/mcp-mcd/oauth/token?resource=" + spark_auth.RESOURCE, data=data)
        self.assertEqual(r.status_code, 200, r.text)
        tokens = r.json()
        self.assertNotEqual(tokens["access_token"], "owner-secret")
        self.assertEqual((await self.client.post("/mcp-mcd/oauth/token", data=data)).status_code, 400)
        fresh_provider = spark_auth.SparkProvider(lambda: "owner-secret")
        self.assertIsNotNone(await fresh_provider.load_access_token(tokens["access_token"]))
        headers = {"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json"}
        result = types.CallToolResult(content=[types.TextContent(type="text", text="ok")], structuredContent={"ok": True})
        with patch.object(gateway.one_click, "call", new_callable=AsyncMock, return_value=result):
            r = await self.client.post("/mcp-mcd/spark", headers=headers, json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "mcd_one_click", "arguments": {"action": "run"}}})
            self.assertEqual(r.json()["result"]["structuredContent"], {"ok": True})
        # Resource-bound OAuth tokens cannot access unrelated legacy APIs.
        self.assertEqual((await self.client.get("/mcp-mcd/api/tools", headers=headers)).status_code, 401)
        refresh = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]}
        r = await self.client.post("/mcp-mcd/oauth/token", data=refresh)
        self.assertEqual(r.status_code, 200, r.text)
        rotated = r.json()
        self.assertNotEqual(rotated["refresh_token"], tokens["refresh_token"])
        self.assertEqual((await self.client.post("/mcp-mcd/oauth/token", data=refresh)).status_code, 400)
        r = await self.client.post("/mcp-mcd/oauth/revoke", data={"client_id": client_id, "token": rotated["refresh_token"], "token_type_hint": "refresh_token"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(await fresh_provider.load_access_token(tokens["access_token"]))
        self.assertIsNone(await fresh_provider.load_access_token(rotated["access_token"]))

    async def test_consent_requires_owner_token_and_csrf(self):
        client_id = await self.register()
        _, values = await self.authorize(client_id)
        r = await self.client.post("/mcp-mcd/oauth/consent", data={**values, "csrf": "wrong", "owner_token": "owner-secret"})
        self.assertEqual(r.status_code, 403)
        r = await self.client.post("/mcp-mcd/oauth/consent", data={**values, "owner_token": "upstream-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("location", r.headers)

    async def test_redirect_and_resource_are_validated(self):
        client_id = await self.register()
        r = await self.client.get("/mcp-mcd/oauth/authorize", params={"client_id": client_id,
             "response_type": "code", "redirect_uri": "https://evil.example/callback", "code_challenge": "a" * 43})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("location", r.headers)
        _, data = await self.grant()
        r = await self.client.post("/mcp-mcd/oauth/token", data={**data, "resource": "https://evil.example"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_target")


if __name__ == "__main__":
    unittest.main()
