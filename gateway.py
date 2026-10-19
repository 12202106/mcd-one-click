"""Authenticated legacy SSE / Streamable HTTP / JSON bridge for McDonald's MCP."""
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.cors import CORSMiddleware
from settings import ORIGIN, ALLOWED_ORIGINS, ALLOWED_HOSTS, SERVERLESS, credentials
from storage import StorageUnavailable
from starlette.routing import Mount, Route

from spark_auth import SparkProvider, auth_routes, PRM_PATH, RESOURCE, digest
from one_click import OneClick, DEFINITION as ONE_CLICK_TOOL, INSTRUCTIONS as ONE_CLICK_INSTRUCTIONS

BASE = "/mcp-mcd"
HERE = Path(__file__).parent
INSTRUCTIONS = """McDonald's China MCP gateway. Preserve returned prices, images and terms.
Get explicit user confirmation of store, items, discounts and calculated total before
creating an order or redeeming points. Claim coupons, cancel orders or create delivery
addresses only when the user requests that action. Never invent prices or item codes."""
READ_ONLY_TOOLS = frozenset({
    "query-party-store-session", "query-nearby-stores", "query-meal-assistance", "query-party-city",
    "query-store-coupons", "calculate-price", "query-my-prizes", "query-party-store-date",
    "available-coupons", "now-time-info", "delivery-query-addresses", "query-promotions",
    "query-my-coupons", "list-nutrition-foods", "mall-product-detail", "mall-order-list",
    "query-party-store", "delivery-query-stores", "mall-order-detail", "query-meal-detail",
    "query-my-account", "mall-points-products", "query-survey-coupon", "query-lottery-info",
    "query-meals", "campaign-calendar", "query-order", "order-list",
})
slots = anyio.Semaphore(8)
logging.getLogger("httpx").setLevel(logging.WARNING)


config = credentials

spark_provider = SparkProvider(lambda: config()["gateway_token"])


class UpstreamUnavailable(Exception):
    pass


@asynccontextmanager
async def upstream():
    try:
        with anyio.fail_after(95):
            async with slots:
                cfg = config()
                async with streamablehttp_client(
                    "https://mcp.mcd.cn", headers=cfg["upstream_headers"],
                    timeout=20, sse_read_timeout=80,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
    except Exception:
        # Exceptions can include upstream credential-bearing URLs/headers: never log them.
        raise UpstreamUnavailable("Upstream MCP failed or timed out; check server credentials. Do not automatically retry account changes.") from None


async def get_tools():
    async with upstream() as session:
        tools = []
        cursor = None
        for _ in range(100):
            page = await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools
        raise ValueError("Too many upstream tool pages")


async def invoke(name, arguments):
    async with upstream() as session:
        return await session.call_tool(name, arguments)


# 前台只有一个按钮，后台的薯条仍然需要一根一根炸。
one_click = OneClick(lambda: get_tools(), lambda name, arguments: invoke(name, arguments),
                     READ_ONLY_TOOLS, spark_provider.store, lambda: digest(config()["gateway_token"]))


server = Server("mcd-one-click", version="2.0.0", instructions=ONE_CLICK_INSTRUCTIONS)


@server.list_tools()
async def list_tools():
    # 工具数量：1。为了确认工具数量而新增的工具数量：0。
    return [types.Tool.model_validate(ONE_CLICK_TOOL)]


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict):
    if name != ONE_CLICK_TOOL["name"]:
        return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text="Only mcd_one_click is available; refresh the tool list")])
    return await one_click.call(arguments)


security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=sorted(ALLOWED_HOSTS), allowed_origins=sorted(ALLOWED_ORIGINS),
)
sse = SseServerTransport(BASE + "/messages/", security_settings=security, max_request_body_size=262144)
manager = StreamableHTTPSessionManager(
    server, json_response=True, stateless=True, security_settings=security,
    max_request_body_size=262144,
)


async def legacy(scope, receive, send):
    if SERVERLESS:
        return await JSONResponse({"error": "SSE requires a persistent server; use /mcp-mcd/spark"}, 405)(scope, receive, send)
    async with sse.connect_sse(scope, receive, send) as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def modern(scope, receive, send):
    if SERVERLESS:
        response = await spark_http(Request(scope, receive))
        return await response(scope, receive, send)
    await manager.handle_request(scope, receive, send)


async def entry(scope, receive, send):
    if scope["method"] == "GET" and scope["path"] == BASE and b"text/event-stream" not in dict(scope["headers"]).get(b"accept", b""):
        return await RedirectResponse(BASE + "/", 307)(scope, receive, send)
    headers = dict(scope["headers"])
    if scope["method"] == "GET":
        if b"text/event-stream" in headers.get(b"accept", b""):
            await legacy(scope, receive, send)
        else:
            await FileResponse(HERE / "web/index.html")(scope, receive, send)
    else:
        await modern(scope, receive, send)


async def tools_api(request):
    return JSONResponse({"tools": [ONE_CLICK_TOOL]})


async def call_api(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not body["name"] or not isinstance(body.get("arguments", {}), dict):
        return JSONResponse({"error": "Expected {name: string, arguments: object}"}, status_code=400)
    if body["name"] != ONE_CLICK_TOOL["name"]:
        return JSONResponse({"error": "Only mcd_one_click is available; refresh the tool list"}, status_code=400)
    try:
        result = await one_click.call(body.get("arguments", {}))
        return JSONResponse(result.model_dump(by_alias=True, exclude_none=True))
    except UpstreamUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def health(request):
    return JSONResponse({"status": "ok", "service": "mcd-one-click"})


async def spark_http(request):
    """Synchronous MCP JSON-RPC: POST in, complete application/json response out."""
    if request.method in {"GET", "HEAD", "DELETE"}:
        return JSONResponse({"error": "Use POST JSON-RPC; this endpoint has no SSE stream or persistent sessions"}, 405, headers={"Allow": "POST"})

    def error(code, message, rpc_id=None):
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})

    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return error(-32700, "Parse error")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return error(-32600, "Invalid JSON-RPC request")
    rpc_id = body.get("id")
    if "id" in body and (isinstance(rpc_id, bool) or not isinstance(rpc_id, (str, int))):
        return error(-32600, "Invalid request id")
    params = body.get("params", {})
    if not isinstance(params, dict):
        return error(-32602, "params must be an object", rpc_id)
    method = body["method"]
    if "id" not in body:
        # Notifications never execute tools, even when their method resembles an RPC.
        from starlette.responses import Response
        return Response(status_code=202)
    try:
        if method == "initialize":
            supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
            requested = params.get("protocolVersion", "2025-11-25")
            if not isinstance(requested, str):
                return error(-32602, "protocolVersion must be a string", rpc_id)
            result = {"protocolVersion": requested if requested in supported else "2025-11-25",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "mcd-one-click-spark", "version": "2.0.0"},
                      "instructions": ONE_CLICK_INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            if params.get("cursor"):
                return error(-32602, "All tools are returned in one page; omit cursor", rpc_id)
            result = {"tools": [ONE_CLICK_TOOL]}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str) or not params["name"] or not isinstance(params.get("arguments", {}), dict):
                return error(-32602, "Expected name and arguments object", rpc_id)
            if params["name"] != ONE_CLICK_TOOL["name"]:
                return error(-32601, "This endpoint exposes only mcd_one_click; refresh the tool list", rpc_id)
            called = await one_click.call(params.get("arguments", {}))
            result = called.model_dump(by_alias=True, exclude_none=True)
        elif method in {"resources/list", "resources/templates/list", "prompts/list"}:
            result = {{"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]: []}
        else:
            return error(-32601, "Method not found", rpc_id)
    except UpstreamUnavailable as exc:
        return error(-32603, str(exc), rpc_id)
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


async def asset(request):
    name = request.path_params["name"]
    if name not in {"app.js", "style.css", "site-config.js", "openapi.json"}:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if name == "openapi.json":
        spec = json.loads((HERE / "web" / name).read_text())
        spec["servers"] = [{"url": ORIGIN + BASE}]
        return JSONResponse(spec)
    return FileResponse(HERE / "web" / name)


@asynccontextmanager
async def lifespan(app):
    config()
    if SERVERLESS:
        yield
    else:
        async with manager.run():
            yield


class ASGIEndpoint:
    def __init__(self, func):
        self.func = func

    async def __call__(self, scope, receive, send):
        await self.func(scope, receive, send)


async def home(request):
    return RedirectResponse(BASE, 307)


inner = Starlette(routes=auth_routes(spark_provider) + [
    Route("/", home),
    Route(BASE, ASGIEndpoint(entry), methods=["GET", "POST", "DELETE"]),
    Route(BASE + "/", ASGIEndpoint(entry), methods=["GET", "POST", "DELETE"]),
    Route(BASE + "/sse", ASGIEndpoint(legacy), methods=["GET"]),
    Route(BASE + "/mcp", ASGIEndpoint(modern), methods=["GET", "POST", "DELETE"]),
    Route(BASE + "/spark", spark_http, methods=["GET", "HEAD", "POST", "DELETE"]),
    Mount(BASE + "/messages/", app=sse.handle_post_message),
    Route(BASE + "/api/tools", tools_api, methods=["GET"]),
    Route(BASE + "/api/call", call_api, methods=["POST"]),
    Route(BASE + "/health", health, methods=["GET"]),
    Route(BASE + "/{name}", asset, methods=["GET"]),
], lifespan=lifespan)


class Guard:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        request = Request(scope, receive)
        headers = {"Cache-Control": "no-store"}
        if request.headers.get("host") not in ALLOWED_HOSTS:
            return await JSONResponse({"error": "Invalid host"}, 421)(scope, receive, send)
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            scope.setdefault("state", {})["mcd_reject"] = "origin"
            return await JSONResponse({"error": "Invalid origin"}, 403)(scope, receive, send)
        oauth_public = request.url.path.startswith(BASE + "/oauth/") or request.url.path in {
            PRM_PATH, "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server/mcp-mcd/oauth", "/.well-known/oauth-authorization-server",
        }
        public = request.url.path == "/" or oauth_public or (request.method in {"GET", "HEAD"} and (
            request.url.path in {BASE + "/app.js", BASE + "/style.css", BASE + "/site-config.js", BASE + "/openapi.json", BASE + "/health"}
            or (request.url.path in {BASE, BASE + "/"} and "text/event-stream" not in request.headers.get("accept", ""))
        ))
        if not public:
            token = request.headers.get("authorization", "")
            expected = "Bearer " + config()["gateway_token"]
            valid = secrets.compare_digest(token.encode(), expected.encode())
            scope.setdefault("state", {})["mcd_auth"] = "owner" if valid else "none"
            if not valid and token.startswith("Bearer ") and request.url.path == BASE + "/spark":
                valid = await spark_provider.load_access_token(token[7:]) is not None
                if valid:
                    scope["state"]["mcd_auth"] = "oauth"
            if not valid:
                scope["state"]["mcd_auth"] = "rejected" if token else "missing"
                headers["WWW-Authenticate"] = f'Bearer realm="mcd-mcp", resource_metadata="{ORIGIN}{PRM_PATH}", scope="mcd"'
                return await JSONResponse({"error": "Valid gateway bearer token required"}, 401, headers=headers)(scope, receive, send)
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 262144:
                    return await JSONResponse({"error": "Request too large"}, 413)(scope, receive, send)
            original_receive = receive
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = buffered_receive

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)

        await inner(scope, receive, secured_send)


class SafeAudit:
    """Protocol diagnostics only: never log query strings, credentials or tool data."""
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.logger = logging.getLogger("mcd.audit")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            self.logger.addHandler(logging.StreamHandler())
        self.logger.propagate = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.wrapped(scope, receive, send)
        path = scope.get("path", "")
        # Audit only the fixed OAuth/MCP routes, with no user-controlled URI segments.
        allowed_paths = {BASE + "/spark", BASE + "/oauth/authorize", BASE + "/oauth/consent",
                         BASE + "/oauth/token", BASE + "/oauth/register", BASE + "/oauth/revoke"}
        if path not in allowed_paths:
            return await self.wrapped(scope, receive, send)
        started = time.monotonic()
        status = 499
        body = bytearray()
        response_body = bytearray()
        headers = dict(scope.get("headers", []))

        async def read():
            message = await receive()
            if message["type"] == "http.request" and len(body) <= 262144:
                body.extend(message.get("body", b""))
            return message

        async def write(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and len(response_body) < 2048:
                response_body.extend(message.get("body", b"")[:2048-len(response_body)])
            await send(message)

        try:
            await self.wrapped(scope, read, write)
        finally:
            rpc = None
            error_code = None
            if path == BASE + "/spark" and body:
                try:
                    payload = json.loads(body)
                    method = payload.get("method") if isinstance(payload, dict) else None
                    known = {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call",
                             "resources/list", "resources/templates/list", "prompts/list"}
                    rpc = method if method in known else "other"
                except (ValueError, TypeError):
                    rpc = "invalid-json"
                try:
                    payload = json.loads(response_body)
                    error = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(error, dict) and isinstance(error.get("code"), int):
                        error_code = error["code"]
                except ValueError:
                    pass
            origin = headers.get(b"origin", b"").decode("ascii", "replace")
            origin_kind = "none" if not origin else "own" if origin in ALLOWED_ORIGINS else "gemini" if origin == "https://gemini.google.com" else "other"
            agent = headers.get(b"user-agent", b"").lower()
            agent_kind = "google" if b"google" in agent else "openauth" if b"openauth" in agent else "browser" if b"mozilla" in agent else "other"
            self.logger.info(json.dumps({"event": "mcd_protocol", "path": path,
                "method": scope["method"], "status": status, "rpc": rpc, "rpc_error": error_code,
                "origin": origin_kind, "agent": agent_kind,
                "auth": scope.get("state", {}).get("mcd_auth"), "reject": scope.get("state", {}).get("mcd_reject"),
                "ms": round((time.monotonic()-started)*1000)}, ensure_ascii=True))


class BoundedRequests:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await Guard()(scope, receive, send)
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            # Leave time for a controlled reply before the platform's 300s deadline.
            with anyio.fail_after(280 if SERVERLESS else 900):
                await Guard()(scope, receive, tracked_send)
        except (StorageUnavailable, TimeoutError):
            if started:
                raise
            await JSONResponse({"error": "State service unavailable or request timed out. Do not automatically retry account changes."}, 503)(scope, receive, send)


# CORS only enables the configured Pages/site origin, never arbitrary origins.
app = CORSMiddleware(SafeAudit(BoundedRequests()), allow_origins=sorted(ALLOWED_ORIGINS),
                     allow_methods=["GET", "POST", "DELETE"],
                     allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version"],
                     expose_headers=["WWW-Authenticate"], allow_credentials=False)
