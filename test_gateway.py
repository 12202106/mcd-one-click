"""Offline checks for credential isolation, failure handling and result fidelity."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp import types

import gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = patch.object(gateway, "config", return_value={
            "gateway_token": "test-gateway-token", "upstream_headers": {"Authorization": "Bearer upstream-secret"},
        })
        self.cfg.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url=gateway.ORIGIN)
        self.headers = {"Authorization": "Bearer test-gateway-token"}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.cfg.stop()

    async def test_upstream_token_is_not_a_gateway_token(self):
        with patch.object(gateway, "get_tools", new_callable=AsyncMock) as listing:
            response = await self.client.get("/mcp-mcd/api/tools", headers={"Authorization": "Bearer upstream-secret"})
            self.assertEqual(response.status_code, 401)
            listing.assert_not_called()

    async def test_preserves_structured_results_and_tool_errors(self):
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text="business error")],
            structuredContent={"code": "unavailable"}, isError=True,
        )
        with patch.object(gateway.one_click, "call", new_callable=AsyncMock, return_value=result) as invoke:
            response = await self.client.post("/mcp-mcd/api/call", headers=self.headers,
                                              json={"name": "mcd_one_click", "arguments": {"action": "run"}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), result.model_dump(by_alias=True, exclude_none=True))
            invoke.assert_awaited_once_with({"action": "run"})

    async def test_upstream_errors_are_sanitized_without_retry(self):
        with patch.object(gateway, "streamablehttp_client", side_effect=RuntimeError("upstream-secret")) as transport:
            response = await self.client.post("/mcp-mcd/api/call", headers=self.headers,
                                              json={"name": "mcd_one_click", "arguments": {"action": "catalog"}})
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("upstream-secret", response.text)
            transport.assert_called_once()

    async def test_cross_origin_rejection_precedes_account_action(self):
        with patch.object(gateway, "invoke", new_callable=AsyncMock) as invoke:
            response = await self.client.post("/mcp-mcd/api/call",
                                              headers={**self.headers, "Origin": "https://evil.example"},
                                              json={"name": "mcd_one_click", "arguments": {"action": "catalog"}})
            self.assertEqual(response.status_code, 403)
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
