"""Persistent OAuth provider for Spark, using the official MCP SDK's protocol handlers."""
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, create_auth_routes, cors_middleware
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
import anyio
from settings import ORIGIN, SERVERLESS
from storage import RedisStore, StorageUnavailable

from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

PREFIX = "/mcp-mcd/oauth"
ISSUER = ORIGIN + PREFIX
RESOURCE = ORIGIN + "/mcp-mcd/spark"
PRM_PATH = "/.well-known/oauth-protected-resource/mcp-mcd/spark"
ASM_PATH = "/.well-known/oauth-authorization-server/mcp-mcd/oauth"
STATE = Path(os.environ.get("MCD_OAUTH_DB", "./.state/oauth.sqlite3"))
SCOPE = "mcd"


class Access(AccessToken):
    family: str
    owner_stamp: str


class Refresh(RefreshToken):
    family: str
    owner_stamp: str


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self):
        has_url = bool(os.getenv("UPSTASH_REDIS_REST_URL"))
        has_token = bool(os.getenv("UPSTASH_REDIS_REST_TOKEN"))
        if has_url != has_token:
            raise RuntimeError("Configure both Upstash REST variables")
        if SERVERLESS and not has_url:
            raise RuntimeError("Serverless deployments require persistent Redis REST storage")
        self.remote = RedisStore() if has_url else None

    async def aget(self, kind, key, consume=False):
        return await anyio.to_thread.run_sync(lambda: self.get(kind, key, consume))

    async def aput(self, kind, key, data, expires):
        return await anyio.to_thread.run_sync(lambda: self.put(kind, key, data, expires))

    @contextmanager
    def connect(self):
        STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(STATE, timeout=10)
        STATE.chmod(0o600)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, data TEXT, expires REAL, PRIMARY KEY(kind,key))")
            db.execute("DELETE FROM records WHERE expires < ?", (time.time(),))
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, kind, key, data, expires):
        if self.remote:
            return self.remote.put(kind, key, data, expires)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (kind, digest(key), json.dumps(data), expires))

    def get(self, kind, key, consume=False):
        if self.remote:
            return self.remote.get(kind, key, consume)
        with self.connect() as db:
            if consume:
                row = db.execute("DELETE FROM records WHERE kind=? AND key=? RETURNING data", (kind, digest(key))).fetchone()
            else:
                row = db.execute("SELECT data FROM records WHERE kind=? AND key=?", (kind, digest(key))).fetchone()
        return json.loads(row[0]) if row else None

    async def revoked(self, family):
        return await self.aget("revoked", family) is not None


class SparkProvider:
    def __init__(self, owner_token):
        self.owner_token = owner_token
        self.store = Store()

    async def get_client(self, client_id):
        item = await self.store.aget("client", client_id)
        return OAuthClientInformationFull.model_validate(item) if item else None

    async def register_client(self, client_info):
        # Google currently registers several HTTPS callback URIs. No outbound fetches.
        if len(client_info.redirect_uris) > 20 or any(urlsplit(str(u)).scheme != "https" for u in client_info.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "Only HTTPS redirect URIs are supported")
        await self.store.aput("client", client_info.client_id, client_info.model_dump(mode="json"), time.time() + 365 * 86400)

    async def authorize(self, client, params):
        if params.resource not in (None, RESOURCE):
            raise AuthorizeError("invalid_request", "Unknown resource")
        params.resource = RESOURCE
        ticket = secrets.token_urlsafe(32)
        await self.store.aput("pending", ticket, {"client_id": client.client_id, "params": params.model_dump(mode="json")}, time.time() + 600)
        return ISSUER + "/consent?" + urlencode({"ticket": ticket})

    async def load_authorization_code(self, client, authorization_code):
        item = await self.store.aget("code", authorization_code)
        if not item or item["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate({**item, "code": authorization_code})

    async def issue(self, client_id, scopes, family=None):
        family = family or secrets.token_urlsafe(24)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": RESOURCE,
                  "family": family, "owner_stamp": digest(self.owner_token()), "subject": "owner"}
        await self.store.aput("access", access, {**common, "token": "", "expires_at": now + 3600}, now + 3600)
        await self.store.aput("refresh", refresh, {**common, "token": "", "expires_at": now + 30 * 86400}, now + 30 * 86400)
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=3600, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        # Consume atomically: parallel exchanges cannot spend the same code twice.
        if not await self.store.aget("code", authorization_code.code, consume=True):
            raise TokenError("invalid_grant", "Code already used or expired")
        return await self.issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        item = await self.store.aget("refresh", refresh_token)
        if not item or item["client_id"] != client.client_id:
            return None
        if item["owner_stamp"] != digest(self.owner_token()) or await self.store.revoked(item["family"]):
            return None
        return Refresh.model_validate({**item, "token": refresh_token})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if not await self.store.aget("refresh", refresh_token.token, consume=True):
            raise TokenError("invalid_grant", "Refresh token already used or expired")
        return await self.issue(client.client_id, scopes, refresh_token.family)

    async def load_access_token(self, token):
        item = await self.store.aget("access", token)
        if not item or item["resource"] != RESOURCE or item["owner_stamp"] != digest(self.owner_token()):
            return None
        if await self.store.revoked(item["family"]):
            return None
        return Access.model_validate({**item, "token": token})

    async def revoke_token(self, token):
        await self.store.aput("revoked", token.family, {}, time.time() + 31 * 86400)

    async def consent(self, request):
        form = await request.form() if request.method == "POST" else {}
        ticket = str(form.get("ticket") or request.query_params.get("ticket", ""))
        pending = await self.store.aget("pending", ticket)
        if not pending:
            return HTMLResponse("Authorization request expired. Return to Gemini and connect again.", 400)
        client = await self.get_client(pending["client_id"])
        params = AuthorizationParams.model_validate(pending["params"])
        if not client:
            return HTMLResponse("Unknown client", 400)
        error = ""
        if request.method == "POST":
            csrf = str(form.get("csrf", ""))
            expected = request.cookies.get("mcd_oauth_csrf", "")
            if not csrf or not secrets.compare_digest(csrf, expected):
                return HTMLResponse("Invalid consent session. Reload the authorization page.", 403)
            if form.get("decision") == "deny":
                await self.store.aget("pending", ticket, consume=True)
                return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state), 303)
            supplied = str(form.get("owner_token", ""))
            if secrets.compare_digest(supplied.encode(), self.owner_token().encode()):
                if not await self.store.aget("pending", ticket, consume=True):
                    return HTMLResponse("Authorization request already used", 400)
                code = secrets.token_urlsafe(32)
                value = AuthorizationCode(
                    code="", scopes=params.scopes or [SCOPE], expires_at=time.time()+300,
                    client_id=client.client_id, code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=RESOURCE, subject="owner",
                )
                await self.store.aput("code", code, value.model_dump(mode="json"), value.expires_at)
                response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 303)
                response.delete_cookie("mcd_oauth_csrf", path=PREFIX)
                return response
            error = '<p role="alert">Token 不正确，请重试。</p>'
        csrf = secrets.token_urlsafe(24)
        esc = lambda value: html.escape(str(value), quote=True)
        response = HTMLResponse(f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>授权 Gemini · MCD One Click</title>
<link rel="stylesheet" href="/mcp-mcd/style.css"></head><body><main><p class="eyebrow">MCD One Click · ACCOUNT LINKING</p>
<h1>连接麦当劳 MCP</h1><section><h2>授权 {esc(client.client_name or 'Registered MCP client')}</h2>
<p>此应用可查询账号、优惠、门店、菜单，并按你的指示执行下单、领券和积分兑换等操作。</p>
<p>下单或兑换前，仍须在 Gemini 中确认商品、门店与费用。授权后有效期内无需每次输入 token。</p>
<p>回调地址：<code>{esc(params.redirect_uri)}</code></p>{error}
<form method="post" action="{PREFIX}/consent"><input type="hidden" name="ticket" value="{esc(ticket)}">
<input type="hidden" name="csrf" value="{esc(csrf)}"><label for="owner_token">网关访问 token</label>
<input id="owner_token" name="owner_token" type="password" autocomplete="off" required>
<p class="muted">输入部署时设置的 MCD_GATEWAY_TOKEN。此表单只提交给 {esc(ORIGIN)}，麦当劳官方 token 无需填写。</p>
<div class="row"><button name="decision" value="approve">授权并返回 Gemini</button>
<button name="decision" value="deny" formnovalidate>拒绝</button></div></form></section></main></body></html>''')
        response.set_cookie("mcd_oauth_csrf", csrf, max_age=600, secure=True, httponly=True, samesite="lax", path=PREFIX)
        # Keep the Origin on same-origin form POSTs. Chrome turns it into "null"
        # under no-referrer; same-origin also hides the ticket on Google's callback.
        response.headers["Referrer-Policy"] = "same-origin"
        callback = urlsplit(str(params.redirect_uri))
        callback_origin = callback.scheme + "://" + callback.netloc
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'none'; style-src 'self'; base-uri 'none'; "
            "object-src 'none'; frame-ancestors 'none'; form-action 'self' " + callback_origin
        )
        return response


def auth_routes(provider):
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE])
    revocation = RevocationOptions(enabled=True)
    routes = create_auth_routes(provider, AnyHttpUrl(ISSUER), AnyHttpUrl(ORIGIN + "/mcp-mcd"), registration, revocation)
    metadata = build_metadata(AnyHttpUrl(ISSUER), AnyHttpUrl(ORIGIN + "/mcp-mcd"), registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]

    class ResourceTokenHandler(TokenHandler):
        async def handle(self, request):
            form = await request.form()
            resources = form.getlist("resource") + request.query_params.getlist("resource")
            if any(value != RESOURCE for value in resources):
                return JSONResponse({"error": "invalid_target", "error_description": "Unknown resource"}, 400)
            return await super().handle(request)

    async def revoke(request):
        # SDK 1.30's revocation request model requires client_secret even for public
        # PKCE clients. Authenticate with its verifier, then implement RFC 7009 here.
        try:
            client = await ClientAuthenticator(provider).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "invalid_client"}, 401)
        form = await request.form()
        raw = form.get("token")
        if not isinstance(raw, str) or not raw:
            return JSONResponse({"error": "invalid_request"}, 400)
        item = await provider.load_access_token(raw) or await provider.load_refresh_token(client, raw)
        if item and item.client_id == client.client_id:
            await provider.revoke_token(item)
        return Response(status_code=200)

    for route in routes:
        if route.path == "/token":
            route.app = cors_middleware(ResourceTokenHandler(provider, ClientAuthenticator(provider)).handle, ["POST", "OPTIONS"])
        elif route.path == "/revoke":
            route.app = cors_middleware(revoke, ["POST", "OPTIONS"])

    async def asm(request):
        return JSONResponse(metadata.model_dump(mode="json", exclude_none=True))

    async def prm(request):
        return JSONResponse({"resource": RESOURCE, "resource_name": "MCD One Click McDonald's China",
            "authorization_servers": [ISSUER], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_documentation": ORIGIN + "/mcp-mcd"})

    return [
        Route(PRM_PATH, prm), Route("/.well-known/oauth-protected-resource", prm),
        Route(ASM_PATH, asm), Route("/.well-known/oauth-authorization-server", asm),
        Route(PREFIX + "/consent", provider.consent, methods=["GET", "POST"]),
        Mount(PREFIX, Starlette(routes=routes)),
    ]
