"""One public Spark tool with server-side read workflows and reviewed writes."""
import copy
import json
import secrets
import time

from jsonschema import Draft202012Validator, ValidationError
from mcp import types

NAME = "mcd_one_click"
# 本变量不负责点亮 Gemini 的顿悟灯，但负责让工具名保持一个。
WRITE_TOOLS = frozenset({"create-order", "mall-create-order", "party-order-create", "draw-lottery",
                         "auto-bind-coupons", "cancel-order", "delivery-create-address"})
DEFINITION = {
    "name": NAME,
    "description": "一键麦当劳：唯一入口，内部自动串联查询，旧独立工具不再对外提供。prepare_order：payload_json 含 beType(1自取/2外送/5得来速/6团餐)、可选 city/keyword/addressId/storeCode/items，自动查地址→门店→菜单与优惠，有 items 时算价；不下单。run：payload_json={steps:[{id,tool,arguments}]}，一次执行最多12个只读步骤；后续参数可用 {$ref:'步骤id#/data/...'} 引用前面真实结果。catalog：payload_json 可含 names 数组，查询内部操作的实时参数定义。prepare_action：payload_json={tool,arguments,steps?,context?}，生成账号变更预览；订单自动算价，兑换context含spuId。必须把预览中的门店、商品、费用/积分和变更展示给用户，得到明确确认后，才用 confirm_action+confirmation_token+confirmed=true执行；确认票据只能执行一次，超时不自动重试。不会替用户猜测ID或确认下单。",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["prepare_order", "run", "catalog", "prepare_action", "confirm_action"]},
        "payload_json": {"type": "string", "description": "任务参数 JSON 对象字符串，默认 {}", "default": "{}"},
        "confirmation_token": {"type": "string", "description": "prepare_action 返回的单次确认票据"},
        "confirmed": {"type": "boolean", "description": "仅在用户明确确认已展示的具体变更和费用后设为 true", "default": False},
    }, "required": ["action"]},
    # This single entry can execute writes. Never label it read-only to suppress consent.
    "annotations": {"title": "一键麦当劳", "readOnlyHint": False, "destructiveHint": True,
                    "idempotentHint": False, "openWorldHint": True},
}
INSTRUCTIONS = """Use only mcd_one_click. Do not call old individual tool names or mcd_read_batch.
For ordering start with prepare_order to fetch addresses, stores, menus and discounts in
one request. Do not query addresses for pickup. Use run for a complete chain of read-only
steps; references are resolved on the server against actual prior results. catalog exposes
internal schemas when needed; it does not add external tools. Missing choices or IDs must
come from the user or actual results. Never invent prices, codes, availability or images.
For an account change use prepare_action, show the exact operation and returned review,
then wait for the user's explicit confirmation before confirm_action. Show store, items,
discounts, fees and total before ordering, or the actual points cost before redemption.
The unique entry is not blanket consent. Never auto-confirm or retry a timed-out write.
Render returned food/coupon images when present. Prices marked fen must be shown as CNY
yuan by dividing by 100. Preserve original terms and vendor errors."""


class FlowError(Exception):
    pass


def result(data, error=False):
    return types.CallToolResult(isError=error, structuredContent=data,
        content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))])


def unpack(value):
    if value.structuredContent is not None:
        return value.structuredContent
    texts = [part.text for part in value.content if isinstance(part, types.TextContent)]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            pass
    return {"content": value.model_dump(mode="json", exclude_none=True)["content"]}


def business_data(value):
    data = unpack(value)
    if value.isError:
        raise FlowError("Upstream operation failed; inspect returned vendor result")
    if isinstance(data, dict) and data.get("code") not in (None, 0, 200, "0", "200"):
        raise FlowError("Upstream business error; no later step or account change was executed")
    return data.get("data", data) if isinstance(data, dict) else data


def resolve(value, outputs, depth=0):
    if depth > 30:
        raise FlowError("Plan nesting exceeds limit")
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref = value["$ref"]
            if not isinstance(ref, str) or "#" not in ref:
                raise FlowError("Use a prior step reference such as addresses#/data/addresses/0/addressId")
            name, pointer = ref.split("#", 1)
            if name not in outputs or (pointer and not pointer.startswith("/")):
                raise FlowError("Reference must point to an existing earlier step")
            found = outputs[name]
            try:
                for part in pointer.split("/")[1:] if pointer else []:
                    key = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(found, list):
                        if not key.isdecimal():
                            raise KeyError()
                        found = found[int(key)]
                    else:
                        found = found[key]
            except (KeyError, IndexError, TypeError):
                raise FlowError("A referenced ID or field was not returned; ask the user or inspect the prior result") from None
            return copy.deepcopy(found)
        return {key: resolve(item, outputs, depth+1) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, outputs, depth+1) for item in value]
    return value


class OneClick:
    def __init__(self, get_tools, invoke, readonly, store, owner_stamp):
        self.get_tools, self.invoke, self.readonly = get_tools, invoke, readonly
        self.store, self.owner_stamp = store, owner_stamp

    async def call(self, args):
        trace = []
        try:
            raw = args.get("payload_json", "{}")
            if not isinstance(raw, str):
                raise FlowError("payload_json must be a JSON object string")
            try:
                payload = json.loads(raw)
            except ValueError:
                raise FlowError("Invalid payload_json") from None
            if not isinstance(payload, dict):
                raise FlowError("payload_json must encode an object")
            action = args.get("action")
            if action == "confirm_action":
                return await self.confirm(args)
            if action not in {"catalog", "run", "prepare_order", "prepare_action"}:
                raise FlowError("Choose a supported one-click action")
            tools = {tool.name: tool for tool in await self.get_tools()}
            if action == "catalog":
                names = payload.get("names")
                if names is not None and (not isinstance(names, list) or any(not isinstance(n, str) for n in names)):
                    raise FlowError("catalog names must be an array of strings")
                return result({"internal_operations": [{"name": name, "description": t.description,
                    "inputSchema": t.inputSchema, "read_only": name in self.readonly}
                    for name, t in tools.items() if names is None or name in names],
                    "note": "These are internal operations, not externally callable MCP tools. Use run or prepare_action."})
            if action == "prepare_order":
                return await self.order_preview(payload, tools, trace)
            outputs = await self.run_steps(payload.get("steps", []), tools, trace)
            if action == "run":
                return result({"status": "complete", "steps": trace})
            name = payload.get("tool")
            if not isinstance(name, str) or name not in WRITE_TOOLS:
                raise FlowError("prepare_action requires one supported account-changing operation")
            arguments = resolve(payload.get("arguments", {}), outputs)
            self.validate(name, arguments, tools)
            context = resolve(payload.get("context", {}), outputs)
            if not isinstance(context, dict):
                raise FlowError("context must be an object")
            review = await self.review(name, arguments, context, tools, trace)
            return await self.queue(name, arguments, context, review, trace)
        except FlowError as exc:
            return result({"status": "needs_input", "message": str(exc), "steps": trace}, error=True)

    def validate(self, name, arguments, tools):
        if name not in tools or not isinstance(arguments, dict):
            raise FlowError("Unknown operation or invalid arguments object")
        try:
            Draft202012Validator(tools[name].inputSchema).validate(arguments)
        except ValidationError:
            raise FlowError("Invalid parameters for " + name + "; use catalog for its current inputSchema") from None

    async def read(self, name, arguments, tools, trace, step_id=None):
        if name not in self.readonly:
            raise FlowError("Read workflows cannot execute account changes")
        self.validate(name, arguments, tools)
        value = await self.invoke(name, arguments)
        trace.append({"id": step_id or str(len(trace)+1), "operation": name,
                      "result": value.model_dump(mode="json", by_alias=True, exclude_none=True)})
        business_data(value)
        return value

    async def run_steps(self, steps, tools, trace):
        # 此处正在串联查询，并未串联三十五个“允许”按钮。
        if not isinstance(steps, list) or len(steps) > 12:
            raise FlowError("steps must contain at most 12 read operations")
        seen = set()
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str) or step["tool"] not in self.readonly:
                raise FlowError("Every workflow step must be a supported read-only operation")
            name = step.get("id")
            if not isinstance(name, str) or not name or len(name) > 64 or name in seen or "#" in name:
                raise FlowError("Every step needs a unique id of at most 64 characters")
            seen.add(name)
        outputs = {}
        for step in steps:
            params = resolve(step.get("arguments", {}), outputs)
            value = await self.read(step["tool"], params, tools, trace, step["id"])
            outputs[step["id"]] = unpack(value)
        return outputs

    async def order_preview(self, payload, tools, trace):
        mode = payload.get("beType")
        if isinstance(mode, bool) or mode not in (1, 2, 5, 6):
            return result({"status": "needs_choice", "field": "beType", "choices": {"1": "到店自取", "2": "麦乐送", "5": "得来速", "6": "团餐"}})
        address = payload.get("addressId")
        if mode in (2, 6) and not address:
            data = business_data(await self.read("delivery-query-addresses", {}, tools, trace))
            addresses = data.get("addresses", []) if isinstance(data, dict) else data
            if not isinstance(addresses, list) or len(addresses) != 1:
                return result({"status": "needs_choice", "field": "addressId", "steps": trace,
                               "message": "Choose a returned delivery address, or explicitly request creating a new address"})
            address = addresses[0].get("addressId")
            if not address:
                raise FlowError("No valid saved address ID was returned")
        if mode in (2, 6):
            stores_result = await self.read("delivery-query-stores", {"addressId": address, "beType": mode}, tools, trace)
        else:
            search = {"beType": mode, "searchType": payload.get("searchType", 2)}
            for key in ("city", "keyword"):
                if payload.get(key):
                    search[key] = payload[key]
            if search["searchType"] == 2 and not search.get("keyword"):
                return result({"status": "needs_input", "field": "keyword", "message": "Provide the actual city/landmark or choose the saved-store search; do not look up delivery addresses for pickup"})
            stores_result = await self.read("query-nearby-stores", search, tools, trace)
        stores = business_data(stores_result)
        if not isinstance(stores, list):
            raise FlowError("Store lookup did not return the expected list; inspect the vendor result")
        candidates = [s for s in stores if isinstance(s, dict) and s.get("storeCode") and s.get("businessStatus") is not False]
        selected = next((s for s in candidates if s["storeCode"] == payload.get("storeCode")), None)
        if payload.get("storeCode") and selected is None:
            raise FlowError("The selected store is not in the returned available store list")
        if not candidates:
            return result({"status": "no_available_store", "steps": trace})
        selected = selected or candidates[0]
        params = {"storeCode": selected["storeCode"], "beType": mode, "orderType": 2 if mode in (2, 6) else 1}
        if mode in (2, 5, 6):
            if not selected.get("beCode"):
                raise FlowError("The selected channel requires a vendor-returned beCode")
            params["beCode"] = selected["beCode"]
        if payload.get("reservationDate"):
            params["reservationDate"] = payload["reservationDate"]
        await self.read("query-meals", params, tools, trace)
        await self.read("query-store-coupons", params, tools, trace)
        if "items" in payload:
            self.check_items(payload["items"])
            priced = {**params, **{k: payload[k] for k in ("items", "needTableware", "gmServiceCode", "withOrder") if k in payload}}
            await self.read("calculate-price", priced, tools, trace)
        return result({"status": "preview_ready", "selected_store": selected, "addressId": address,
            "order_parameters": params, "steps": trace,
            "message": "Preview only. Confirm the store and choose returned product IDs; use prepare_action to obtain a final quote before creating an order."})

    def check_items(self, items):
        if not isinstance(items, list) or not items:
            raise FlowError("An order requires actual selected items")
        for item in items:
            if not isinstance(item, dict) or not item.get("productCode") or type(item.get("quantity")) is not int or item["quantity"] < 1:
                raise FlowError("Each selected item needs a real productCode and positive integer quantity")

    async def review(self, name, arguments, context, tools, trace):
        review = {"operation": name, "arguments": arguments}
        if name == "create-order":
            self.check_items(arguments.get("items"))
            params = {k: v for k, v in arguments.items() if k in tools["calculate-price"].inputSchema.get("properties", {})}
            quote = business_data(await self.read("calculate-price", params, tools, trace))
            if not isinstance(quote, dict) or type(quote.get("price")) is not int or quote["price"] < 0:
                raise FlowError("No valid live payable amount was returned; ordering remains blocked")
            review.update({"quote": quote, "currency": "CNY", "unit": "fen", "payable_fen": quote["price"]})
        elif name in {"mall-create-order", "party-order-create"}:
            spu = context.get("spuId", arguments.get("spuId"))
            if spu is None:
                raise FlowError("Provide context.spuId from the selected product so live SKU cost can be reviewed")
            detail = business_data(await self.read("mall-product-detail", {"spuId": spu}, tools, trace))
            if not isinstance(detail, dict) or detail.get("shopId") != (2 if name == "mall-create-order" else 5):
                raise FlowError("The selected product belongs to a different redemption/party shop")
            skus = detail.get("skuList", []) if isinstance(detail, dict) else []
            sku = next((item for item in skus if item.get("skuId") == arguments.get("skuId")), None)
            if not sku or (sku.get("points") is None and sku.get("price") is None):
                raise FlowError("The selected SKU and its current cost were not returned")
            review.update({"product": detail, "selected_sku": sku, "quantity": arguments.get("count", 1)})
        elif name == "draw-lottery":
            info = business_data(await self.read("query-lottery-info", {}, tools, trace))
            decision = info.get("drawDecision", {}) if isinstance(info, dict) else {}
            if decision.get("resourceEligible") is not True or not decision.get("nextConsumption"):
                raise FlowError("The server did not return eligible lottery resources and actual next consumption")
            review["nextConsumption"] = decision["nextConsumption"]
        elif name == "cancel-order":
            review["order"] = business_data(await self.read("query-order", {"orderId": arguments["orderId"]}, tools, trace))
        elif name == "auto-bind-coupons":
            review["available_coupons"] = business_data(await self.read("available-coupons", {}, tools, trace))
        return review

    async def queue(self, name, arguments, context, review, trace):
        # 确认票据不是优惠券，不能拿去兑换第二次相同的订单。
        token = secrets.token_urlsafe(32)
        await self.store.aput("oneclick_pending", token, {"operation": name, "arguments": arguments, "context": context,
            "review": review, "owner_stamp": self.owner_stamp()}, time.time()+300)
        return result({"status": "confirmation_required", "review": review, "steps": trace,
            "confirmation_token": token, "expires_in": 300,
            "message": "Show this exact operation, items/store and live cost to the user. Wait for explicit confirmation, then call confirm_action. No account change has been executed."})

    def costs(self, review):
        quote = review.get("quote", {})
        return {"money": {k: quote[k] for k in ("price", "deliveryPrice", "packingPrice", "tablewarePrice", "discount") if k in quote},
                "sku": review.get("selected_sku"), "lottery": review.get("nextConsumption")}

    async def confirm(self, args):
        token = args.get("confirmation_token")
        if args.get("confirmed") is not True or not isinstance(token, str) or not token:
            raise FlowError("A valid confirmation token and the user's explicit confirmation are required")
        pending = await self.store.aget("oneclick_pending", token)
        if not pending or pending.get("owner_stamp") != self.owner_stamp():
            return result({"status": "already_used_or_expired", "message": "Do not repeat the write. Check its result or prepare a fresh review only when appropriate."}, error=True)
        if not await self.store.aget("oneclick_pending", token, consume=True):
            raise FlowError("This confirmation was already consumed; no duplicate operation was sent")
        tools = {t.name: t for t in await self.get_tools()}
        name, arguments = pending["operation"], pending["arguments"]
        self.validate(name, arguments, tools)
        trace = []
        latest = await self.review(name, arguments, pending["context"], tools, trace)
        if self.costs(latest) != self.costs(pending["review"]):
            return await self.queue(name, arguments, pending["context"], latest, trace)
        # Ticket consumed before the write. A timeout can never cause an automatic retry.
        # 薯条可以再来一份，同一个订单不能因为网络打嗝再来一份。
        value = await self.invoke(name, arguments)
        return result({"status": "executed", "operation": name,
                       "result": value.model_dump(mode="json", by_alias=True, exclude_none=True)}, error=bool(value.isError))
