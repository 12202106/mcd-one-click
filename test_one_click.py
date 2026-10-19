import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import types

import gateway
import one_click
import spark_auth


def reply(data, error=False):
    return types.CallToolResult(structuredContent={"data": data}, isError=error,
        content=[types.TextContent(type="text", text=json.dumps({"data": data}))])


class OneClickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(spark_auth, "STATE", Path(self.tmp.name)/"state.db")
        self.db.start()
        names = list(gateway.READ_ONLY_TOOLS | one_click.WRITE_TOOLS)
        self.tools = [types.Tool(name=n, inputSchema={"type": "object"}) for n in names]
        price = next(t for t in self.tools if t.name == "calculate-price")
        price.inputSchema = {"type": "object", "properties": {k: {} for k in ["storeCode", "orderType", "beType", "items"]}}
        self.listing = AsyncMock(return_value=self.tools)
        self.invoke = AsyncMock()
        self.engine = one_click.OneClick(self.listing, self.invoke, gateway.READ_ONLY_TOOLS, spark_auth.Store(), lambda: "owner-stamp")

    async def asyncTearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    async def call(self, action, payload=None, **kwargs):
        value = await self.engine.call({"action": action, "payload_json": json.dumps(payload or {}), **kwargs})
        return value, value.structuredContent

    async def test_chain_resolves_returned_address_and_store_ids_in_one_call(self):
        self.invoke.side_effect = [reply({"addresses": [{"addressId": "real-address"}]}),
            reply([{"storeCode": "real-store"}]), reply({"menu": []})]
        steps = [
            {"id": "address", "tool": "delivery-query-addresses", "arguments": {}},
            {"id": "store", "tool": "delivery-query-stores", "arguments": {"addressId": {"$ref": "address#/data/addresses/0/addressId"}, "beType": 2}},
            {"id": "menu", "tool": "query-meals", "arguments": {"storeCode": {"$ref": "store#/data/0/storeCode"}, "beType": 2, "orderType": 2}},
        ]
        value, data = await self.call("run", {"steps": steps})
        self.assertFalse(value.isError)
        self.assertEqual(len(data["steps"]), 3)
        self.assertEqual(self.invoke.await_args_list[1].args[1]["addressId"], "real-address")
        self.assertEqual(self.invoke.await_args_list[2].args[1]["storeCode"], "real-store")

    async def test_mixed_write_plan_is_rejected_before_any_query(self):
        value, _ = await self.call("run", {"steps": [{"id": "time", "tool": "now-time-info"}, {"id": "write", "tool": "create-order"}]})
        self.assertTrue(value.isError)
        self.invoke.assert_not_called()

    async def test_missing_reference_does_not_guess_or_execute_later_steps(self):
        self.invoke.return_value = reply({"addresses": []})
        value, _ = await self.call("run", {"steps": [{"id": "address", "tool": "delivery-query-addresses"},
            {"id": "store", "tool": "delivery-query-stores", "arguments": {"addressId": {"$ref": "address#/data/addresses/0/addressId"}}}]})
        self.assertTrue(value.isError)
        self.assertEqual(self.invoke.await_count, 1)

    async def test_order_preview_fetches_address_store_menu_discounts_and_quote(self):
        self.invoke.side_effect = [reply({"addresses": [{"addressId": "address-id"}]}),
            reply([{"storeCode": "store-id", "beCode": "delivery", "businessStatus": True}]),
            reply({"menu": []}), reply({"coupons": []}), reply({"price": 1490})]
        value, data = await self.call("prepare_order", {"beType": 2, "items": [{"productCode": "chosen-item", "quantity": 1}]})
        self.assertFalse(value.isError)
        self.assertEqual(data["status"], "preview_ready")
        self.assertEqual([c.args[0] for c in self.invoke.await_args_list],
            ["delivery-query-addresses", "delivery-query-stores", "query-meals", "query-store-coupons", "calculate-price"])
        self.assertEqual(data["order_parameters"]["beCode"], "delivery")

    async def test_pickup_preview_never_fetches_delivery_addresses(self):
        self.invoke.side_effect = [reply([{"storeCode": "pickup", "businessStatus": True}]), reply({}), reply({})]
        value, data = await self.call("prepare_order", {"beType": 1, "city": "用户提供的城市", "keyword": "用户提供的地标"})
        self.assertFalse(value.isError)
        self.assertEqual([c.args[0] for c in self.invoke.await_args_list], ["query-nearby-stores", "query-meals", "query-store-coupons"])
        self.assertNotIn("beCode", data["order_parameters"])

    async def test_multiple_addresses_requires_user_choice(self):
        self.invoke.return_value = reply({"addresses": [{"addressId": "one"}, {"addressId": "two"}]})
        _, data = await self.call("prepare_order", {"beType": 2})
        self.assertEqual(data["field"], "addressId")
        self.assertEqual(data["status"], "needs_choice")
        self.assertEqual(self.invoke.await_count, 1)

    async def prepare(self):
        payload = {"tool": "create-order", "arguments": {"storeCode": "chosen-store", "beType": 1, "orderType": 1,
                   "items": [{"productCode": "chosen-product", "quantity": 1}]}}
        _, data = await self.call("prepare_action", payload)
        return data

    async def test_write_requires_live_quote_explicit_confirmation_and_is_single_use(self):
        self.invoke.side_effect = [reply({"price": 1490}), reply({"price": 1490}), reply({"orderId": "vendor-order"})]
        prepared = await self.prepare()
        self.assertEqual(prepared["review"]["payable_fen"], 1490)
        self.assertEqual(self.invoke.await_count, 1)
        token = prepared["confirmation_token"]
        value, _ = await self.call("confirm_action", confirmation_token=token, confirmed=False)
        self.assertTrue(value.isError)
        self.assertEqual(self.invoke.await_count, 1)
        # Altering the payload on confirmation cannot alter the reviewed operation.
        value, data = await self.call("confirm_action", {"arguments": {"items": [{"quantity": 99}]}}, confirmation_token=token, confirmed=True)
        self.assertEqual(data["status"], "executed")
        self.assertEqual(self.invoke.await_args.args[1]["items"][0]["quantity"], 1)
        value, data = await self.call("confirm_action", confirmation_token=token, confirmed=True)
        self.assertTrue(value.isError)
        self.assertEqual(data["status"], "already_used_or_expired")
        self.assertEqual([c.args[0] for c in self.invoke.await_args_list].count("create-order"), 1)

    async def test_changed_quote_requires_new_confirmation_without_order(self):
        self.invoke.side_effect = [reply({"price": 1490}), reply({"price": 1590})]
        prepared = await self.prepare()
        _, data = await self.call("confirm_action", confirmation_token=prepared["confirmation_token"], confirmed=True)
        self.assertEqual(data["status"], "confirmation_required")
        self.assertEqual(data["review"]["payable_fen"], 1590)
        self.assertNotEqual(data["confirmation_token"], prepared["confirmation_token"])
        self.assertNotIn("create-order", [c.args[0] for c in self.invoke.await_args_list])

    async def test_write_timeout_does_not_allow_replay(self):
        self.invoke.side_effect = [reply({"price": 1490}), reply({"price": 1490}), RuntimeError("simulated upstream timeout")]
        prepared = await self.prepare()
        with self.assertRaises(RuntimeError):
            await self.call("confirm_action", confirmation_token=prepared["confirmation_token"], confirmed=True)
        value, _ = await self.call("confirm_action", confirmation_token=prepared["confirmation_token"], confirmed=True)
        self.assertTrue(value.isError)
        self.assertEqual([c.args[0] for c in self.invoke.await_args_list].count("create-order"), 1)

    async def test_missing_quote_does_not_offer_order_confirmation(self):
        self.invoke.return_value = reply({"message": "unavailable"})
        value, data = await self.call("prepare_action", {"tool": "create-order", "arguments": {
            "items": [{"productCode": "chosen", "quantity": 1}]}})
        self.assertTrue(value.isError)
        self.assertNotIn("confirmation_token", data)


if __name__ == "__main__":
    unittest.main()
