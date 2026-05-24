"""
Vibe-Trading MCP launcher + Feishu bot integration.

Two outward-facing surfaces on a single container:
  1. MCP over SSE (/sse, /messages) — for Claude Desktop / Code / mobile Connector.
     - Static Bearer auth (Desktop/Code via mcp-remote) and OAuth 2.1 + PKCE
       with Dynamic Client Registration (mobile Custom Connector).
  2. Feishu event webhook (/feishu/events) — for Lark/Feishu bot integration.
     - Receives im.message.receive_v1 events.
     - Parses stock symbol from message text.
     - Fires SwarmRuntime.start_run() in background.
     - Polls completion in a separate poller thread; posts final_report back
       to the originating chat via Feishu /im/v1/messages.

Env vars:
  MCP_AUTH_TOKEN              (required) shared secret for MCP Bearer + OAuth login password.
  PORT                        (default 8000)
  PUBLIC_BASE_URL             (optional) used for OAuth issuer.
  LANGCHAIN_PROVIDER          (default openai)  LLM provider for swarm worker.
  LANGCHAIN_MODEL_NAME        model name for provider.
  LANGCHAIN_TEMPERATURE       (default 0.0)
  TIMEOUT_SECONDS             (default 60) per-LLM-call timeout.
  MAX_RETRIES                 (default 1)
  DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL  (provider=deepseek path)
  OPENAI_API_KEY  / OPENAI_BASE_URL     (provider=openai or fallback)

  LARK_APP_ID                 Feishu app id (cli_xxx).
  LARK_APP_SECRET             Feishu app secret.
  FEISHU_VERIFICATION_TOKEN   verification token from event-config page (optional but
                              strongly recommended; if unset, all POSTs to
                              /feishu/events accepted).
  FEISHU_DEFAULT_PRESET       (default investment_committee)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time
from typing import Any
from urllib.parse import urlencode


import httpx

# 历史背景:之前对 httpx.Client / AsyncClient 做过全局 monkey patch,把所有
# client 的 read timeout 强制 cap 到 60s,目的是防 LLM 流式调用半死 socket
# 拖死 worker 线程。
# 副作用:_deepseek_json_call 明确想用 read=90 给 DeepSeek-v4-pro reasoning
# model 留够时间,被 cap 成 60s 后偶发 ReadTimeout(见 CHANGELOG 0.2.x)。
#
# 决定:移除全局 monkey patch,改为「每个 httpx.Client(...) 调用点显式声明
# timeout」(已 audit:7 处调用全都带 timeout 参数)。
# 同时在 lifespan startup 加 self-test 断言一个 read=90 的 AsyncClient
# 真实拿到的就是 90,不是被某个 import 副作用悄悄改的。


# Now safe to import mcp_server (which creates FastMCP + lazy ChatLLM clients)
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

import mcp_server


# ─────────── config ───────────
EXPECTED_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
if not EXPECTED_TOKEN:
    print("FATAL: MCP_AUTH_TOKEN env var is required", file=sys.stderr)
    sys.exit(2)

# 与 MCP_AUTH_TOKEN 解耦的管理员凭据,专用于 /_debug/* 端点。
# 不设值时所有 /_debug/* 路由不注册(生产默认安全行为)。
ADMIN_AUTH_TOKEN = os.environ.get("ADMIN_AUTH_TOKEN", "").strip()
ENABLE_DEBUG_ENDPOINTS = (os.environ.get("ENABLE_DEBUG_ENDPOINTS", "").strip().lower()
                          in ("1", "true", "yes", "on"))
DEBUG_ENDPOINTS_ACTIVE = ENABLE_DEBUG_ENDPOINTS and bool(ADMIN_AUTH_TOKEN)
if ENABLE_DEBUG_ENDPOINTS and not ADMIN_AUTH_TOKEN:
    print("WARN: ENABLE_DEBUG_ENDPOINTS=true 但 ADMIN_AUTH_TOKEN 未设置,"
          "/_debug/* 路由不会注册(防止裸跑泄露)", file=sys.stderr)

PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

SIGNING_KEY = hashlib.sha256(b"vibe-trading-oauth/v1\x00" + EXPECTED_TOKEN.encode()).digest()
AUTH_CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 86400

# ─────────── 持久化状态目录(必须放在引用 STATE_DIR 之前) ───────────
# Railway 容器 ephemeral,默认每次 deploy 擦盘。设置 STATE_DIR 指向 Railway Volume
# 挂载路径(如 /app/data),把以下状态搬到 Volume,deploy 后保留:
#   - swarm runs (.swarm/runs/{run_id}/...) — 报告 + feishu_meta + events
#   - oauth_clients.json — OAuth DCR 注册表
# 不设 STATE_DIR 时退化到老行为(写到 site-packages 下,deploy 清零)。
STATE_DIR = os.environ.get("STATE_DIR", "").strip().rstrip("/")
if STATE_DIR:
    import pathlib as _pathlib
    _state_path = _pathlib.Path(STATE_DIR)
    try:
        _state_path.mkdir(parents=True, exist_ok=True)
        (_state_path / ".swarm" / "runs").mkdir(parents=True, exist_ok=True)
        # 覆盖 mcp_server.AGENT_DIR — swarm runtime 内部 `AGENT_DIR/.swarm/runs`
        # 等所有路径推导随之改变,旧 run + 新 run 都落到 Volume。
        mcp_server.AGENT_DIR = _state_path
        print(f"[boot] STATE_DIR active: {STATE_DIR} "
              f"(swarm runs → {STATE_DIR}/.swarm/runs)", flush=True)
    except Exception as _e:
        print(f"[boot] WARN: STATE_DIR={STATE_DIR} setup failed: {_e}; "
              f"falling back to ephemeral site-packages",
              file=sys.stderr, flush=True)
        STATE_DIR = ""

# OAuth clients 持久化路径:若 STATE_DIR 设置则落 Volume,否则 fallback /tmp
_OAUTH_CLIENTS_PATH = (f"{STATE_DIR}/oauth_clients.json" if STATE_DIR
                       else "/tmp/oauth_clients.json")
_oauth_clients: dict[str, dict] = {}
_oauth_clients_lock = threading.Lock()

# OAuth 一次性 authorization code 服务端存储。code 是不可猜测的 random,
# 不再用 self-signed JWT(JWT 可被重放,只能靠过期窗口拦,弱于真一次性 code)。
_oauth_codes: dict[str, dict] = {}
_oauth_codes_lock = threading.Lock()

# Feishu integration
LARK_APP_ID = os.environ.get("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip()
# 可选:飞书事件加密 key (AES,启用「加密事件」后才需要)。
# 与 FEISHU_VERIFICATION_TOKEN 任一即可作为身份证据。
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "").strip()
# webhook 安全限制
FEISHU_WEBHOOK_MAX_BYTES = int(os.environ.get("FEISHU_WEBHOOK_MAX_BYTES", "65536"))
FEISHU_WEBHOOK_RATE_LIMIT = int(os.environ.get("FEISHU_WEBHOOK_RATE_LIMIT", "30"))
FEISHU_DEFAULT_PRESET = os.environ.get("FEISHU_DEFAULT_PRESET", "investment_committee").strip()
# Link-share permission for every docx the bot creates. Default tenant_readable
# so group members can open the link without applying for permission.
# Valid: tenant_readable / tenant_editable / anyone_readable / anyone_editable / closed
FEISHU_DOC_SHARE_ENTITY = os.environ.get("FEISHU_DOC_SHARE_ENTITY",
                                         "tenant_readable").strip().lower()
# 用户云盘文件夹 token —— 若设置,bot 创建的每个 docx 都落在这个文件夹下,
# 文档自动继承文件夹的「共享权限」(由文件夹所有者在飞书 UI 配)。
# 这条路绕开「需要 drive:drive 才能改链接共享」的难题。
# 前提:文件夹所有者必须在飞书把这个文件夹「分享」给 bot 并给「可编辑」权限。
FEISHU_DRIVE_FOLDER_TOKEN = os.environ.get("FEISHU_DRIVE_FOLDER_TOKEN", "").strip()

# Feishu webhook 强制要求 verification token 或 encrypt key,否则 /feishu/events
# 不注册路由(防止任意公网 POST 触发 swarm 跑分析)。
_FEISHU_HAS_SECRET = bool(FEISHU_VERIFICATION_TOKEN or FEISHU_ENCRYPT_KEY)
FEISHU_ENABLED = bool(LARK_APP_ID and LARK_APP_SECRET and _FEISHU_HAS_SECRET)
if LARK_APP_ID and LARK_APP_SECRET and not _FEISHU_HAS_SECRET:
    print("WARN: LARK_APP_ID/SECRET 已设置但 FEISHU_VERIFICATION_TOKEN 和 "
          "FEISHU_ENCRYPT_KEY 都未设置 — /feishu/events 不会注册(防裸跑)。"
          "建议:在飞书开放平台「事件订阅」页拷贝 Verification Token 到 env。",
          file=sys.stderr)

# Link-share entity allowlist — debug 端点改文档权限时只能选这几个值。
FEISHU_LINK_SHARE_ENTITIES = frozenset({
    "tenant_readable", "tenant_editable",
    "anyone_readable", "anyone_editable",
    "closed",
})

# (STATE_DIR 已在文件前部声明,避免向前引用)

# Notion integration (optional). Set EITHER:
#   NOTION_DATABASE_ID    → reports become DB rows with structured properties
#   NOTION_PARENT_PAGE_ID → reports become child pages under that page (no DB schema)
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()
NOTION_ENABLED = bool(NOTION_API_KEY and (NOTION_DATABASE_ID or NOTION_PARENT_PAGE_ID))
NOTION_API_VERSION = os.environ.get("NOTION_API_VERSION", "2022-06-28")


# ─────────── HS256 JWT (stdlib only) ───────────
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _jwt_encode(payload: dict[str, Any]) -> str:
    h = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    s = _b64url(hmac.new(SIGNING_KEY, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"

def _jwt_decode(token: str) -> dict[str, Any] | None:
    try:
        h, p, s = token.split(".")
        exp = hmac.new(SIGNING_KEY, f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(s), exp):
            return None
        payload = json.loads(_b64url_decode(p))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ─────────── OAuth client registry + auth code store ───────────
from urllib.parse import urlparse as _urlparse


def _is_valid_redirect_uri(uri: str) -> bool:
    """https 强制 + localhost/127.0.0.1/::1 例外 (本地调试 client 常用)。
    禁止 http://(非 loopback)、file://、自定义 scheme(MCP 移动 connector
    会用自定义 scheme,但目前我们的部署只接 web client,真有自定义 scheme
    需求再放开)。"""
    if not uri or not isinstance(uri, str):
        return False
    try:
        u = _urlparse(uri)
    except Exception:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        host = (u.hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1")
    return False


def _load_oauth_clients():
    """启动时从 /tmp 恢复已注册 client。文件丢了也无所谓 — 客户端走一次
    DCR 就重新注册。"""
    if not os.path.exists(_OAUTH_CLIENTS_PATH):
        return
    try:
        with open(_OAUTH_CLIENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _oauth_clients_lock:
                _oauth_clients.update(data)
            print(f"[oauth] restored {len(data)} clients from disk", flush=True)
    except Exception as e:
        print(f"[oauth] restore clients err: {e}", flush=True)


def _save_oauth_clients():
    try:
        with _oauth_clients_lock:
            snapshot = dict(_oauth_clients)
        with open(_OAUTH_CLIENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except Exception as e:
        print(f"[oauth] save clients err: {e}", flush=True)


_load_oauth_clients()


def _register_oauth_client(redirect_uris: list[str]) -> str:
    """生成 client_id,把它和 redirect_uris allowlist 写入 registry。"""
    cid = "mcp-" + secrets.token_urlsafe(12)
    with _oauth_clients_lock:
        _oauth_clients[cid] = {
            "redirect_uris": list(redirect_uris),
            "issued_at": int(time.time()),
        }
    _save_oauth_clients()
    return cid


def _lookup_client(client_id: str) -> dict | None:
    with _oauth_clients_lock:
        return _oauth_clients.get(client_id)


def _client_allows_redirect(client_id: str, redirect_uri: str) -> bool:
    """精确匹配,不做 prefix / 模式匹配,杜绝 open redirect。"""
    c = _lookup_client(client_id)
    if not c:
        return False
    return redirect_uri in (c.get("redirect_uris") or [])


def _save_oauth_code(client_id: str, redirect_uri: str, code_challenge: str,
                    scope: str) -> str:
    """生成不可猜测的 opaque code,服务端存映射。token 兑换时 pop & delete。"""
    code = secrets.token_urlsafe(32)
    with _oauth_codes_lock:
        _oauth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": scope,
            "expires_at": int(time.time()) + AUTH_CODE_TTL,
        }
        # 顺便扫一遍删除过期 code 防止内存累积
        now = int(time.time())
        for k in [k for k, v in _oauth_codes.items() if v["expires_at"] < now]:
            del _oauth_codes[k]
    return code


def _pop_oauth_code(code: str) -> dict | None:
    """pop & delete — 一次性使用。"""
    with _oauth_codes_lock:
        entry = _oauth_codes.pop(code, None)
    if not entry:
        return None
    if entry["expires_at"] < int(time.time()):
        return None
    return entry


# ─────────── base URL helpers ───────────
def _base_from_request(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return f"{request.url.scheme}://{request.headers.get('host', '')}"

def _base_from_scope(scope: Scope) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    headers = {k.decode("ascii"): v.decode("ascii") for k, v in scope.get("headers", [])}
    proto = headers.get("x-forwarded-proto") or scope.get("scheme", "https")
    host = headers.get("host", "")
    return f"{proto}://{host}"


# ─────────── PUBLIC_PATHS for auth middleware ───────────
PUBLIC_PATHS = frozenset({
    "/",
    "/healthz",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/register",
    "/authorize",
    "/token",
})
# Feishu webhook 路径只有 FEISHU_ENABLED 时才公开(同时启动校验了 token),
# 否则不放进 PUBLIC_PATHS — 这样即便误注册了路由也会走 Bearer 检查。
if FEISHU_ENABLED:
    PUBLIC_PATHS = PUBLIC_PATHS | {"/feishu/events"}


# ─────────── auth middleware (Bearer static OR JWT) ───────────
class AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        auth = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v
                break

        if not auth.lower().startswith(b"bearer "):
            await self._reject(scope, receive, send, "missing_bearer")
            return

        token = auth[7:].decode("ascii", errors="ignore").strip()
        path = scope.get("path", "")
        # /_debug/* 路径要求 ADMIN_AUTH_TOKEN(独立于 MCP_AUTH_TOKEN)。
        # 这样泄露 MCP 用户 token 不会顺带打开运维通道。
        if path.startswith("/_debug/"):
            if not ADMIN_AUTH_TOKEN:
                await self._reject(scope, receive, send, "debug_disabled")
                return
            if secrets.compare_digest(token, ADMIN_AUTH_TOKEN):
                await self.app(scope, receive, send)
                return
            await self._reject(scope, receive, send, "admin_token_required")
            return

        if secrets.compare_digest(token, EXPECTED_TOKEN):
            await self.app(scope, receive, send)
            return
        payload = _jwt_decode(token)
        if payload and payload.get("typ") == "access":
            await self.app(scope, receive, send)
            return
        await self._reject(scope, receive, send, "invalid_token")

    async def _reject(self, scope, receive, send, error):
        base = _base_from_scope(scope)
        challenge = (
            f'Bearer realm="vibe-trading", '
            f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
        )
        response = JSONResponse({"error": error}, status_code=401,
                               headers={"WWW-Authenticate": challenge})
        await response(scope, receive, send)


# ─────────── public info endpoints ───────────
async def root(_):
    return JSONResponse({
        "service": "vibe-trading-mcp",
        "transport": "sse",
        "sse_endpoint": "/sse",
        "auth": ["static_bearer", "oauth2.1_pkce"],
        "feishu_webhook": "/feishu/events" if FEISHU_ENABLED else None,
    })


async def healthz(_):
    return PlainTextResponse("ok")


# ─────────── OAuth metadata ───────────
async def oauth_authorization_server(request):
    base = _base_from_request(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


async def oauth_protected_resource(request):
    base = _base_from_request(request)
    return JSONResponse({
        "resource": f"{base}/sse",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


# ─────────── Dynamic Client Registration ───────────
async def register(request):
    """RFC 7591 DCR. 之前实现只是回显 redirect_uris,**没有服务端保存** —
    导致 /authorize 阶段 client_id 可被伪造、redirect_uri 可被任意改成
    钓鱼地址。修复:把 client_id 和 redirect_uris allowlist 真正存到 registry,
    /authorize + /token 严格 lookup 校验。"""
    try:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri",
             "error_description": "redirect_uris must be a non-empty list"},
            status_code=400)
    invalid = [u for u in uris if not _is_valid_redirect_uri(u)]
    if invalid:
        return JSONResponse(
            {"error": "invalid_redirect_uri",
             "error_description": (
                 f"only https or http://localhost(:port) allowed; got: {invalid}")},
            status_code=400)
    cid = _register_oauth_client(uris)
    return JSONResponse({
        "client_id": cid,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    }, status_code=201)


# ─────────── /authorize ───────────
LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>vibe-trading: authorize</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 420px;
         margin: 8vh auto; padding: 24px; background: #111; color: #eee; }
  h1 { font-size: 1.2em; margin-bottom: 4px; }
  .sub { opacity: .7; font-size: .9em; margin-bottom: 20px; }
  .client { font-family: ui-monospace, monospace; font-size: .85em;
            background: #1a1a1a; padding: 6px 10px; border-radius: 4px;
            margin-bottom: 20px; word-break: break-all; }
  input, button { width: 100%; padding: 12px; margin: 6px 0; font-size: 1em;
                  border: 1px solid #333; background: #1a1a1a; color: #eee;
                  border-radius: 6px; box-sizing: border-box; }
  button { cursor: pointer; background: #2563eb; border-color: #2563eb;
           font-weight: 600; }
  button:hover { background: #1d4ed8; }
  .err { color: #f87171; font-size: .9em; margin-top: 8px; }
</style></head><body>
<h1>Authorize vibe-trading</h1>
<div class="sub">A client is requesting access. Enter your shared secret to grant it.</div>
<div class="client">client_id: __CLIENT_ID__</div>
<form method="post">
__HIDDEN__
  <input type="password" name="secret" placeholder="MCP access token" autofocus required>
  <button type="submit">Authorize</button>
__ERROR__
</form>
</body></html>
"""


def _render_login(params, error=""):
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k, quote=True)}" '
        f'value="{html.escape(v, quote=True)}">'
        for k, v in params.items()
    )
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return (LOGIN_PAGE.replace("__HIDDEN__", hidden)
            .replace("__ERROR__", err)
            .replace("__CLIENT_ID__", html.escape(params.get("client_id", "(unknown)"))))


def _check_authorize_params(params: dict) -> tuple[bool, str]:
    """共享校验:必填字段 + response_type + code_challenge_method + client_id
    已注册 + redirect_uri 在 client 的 allowlist 里。"""
    for k in ("response_type", "client_id", "redirect_uri",
              "code_challenge", "code_challenge_method"):
        if not params.get(k):
            return False, f"missing: {k}"
    if params["response_type"] != "code":
        return False, "unsupported_response_type"
    if params["code_challenge_method"] != "S256":
        return False, "need S256"
    if not _lookup_client(params["client_id"]):
        return False, "unknown client_id"
    if not _client_allows_redirect(params["client_id"], params["redirect_uri"]):
        return False, "redirect_uri not in client allowlist"
    return True, ""


async def authorize_get(request):
    params = dict(request.query_params)
    ok, err = _check_authorize_params(params)
    if not ok:
        return HTMLResponse(err, status_code=400)
    return HTMLResponse(_render_login(params))


async def authorize_post(request):
    form = await request.form()
    secret = str(form.get("secret", ""))
    params = {k: str(v) for k, v in form.items() if k != "secret"}
    ok, err = _check_authorize_params(params)
    if not ok:
        return HTMLResponse(err, status_code=400)
    if not secrets.compare_digest(secret, EXPECTED_TOKEN):
        return HTMLResponse(_render_login(params, "Invalid token."), status_code=401)
    # 服务端存一次性 opaque code(替代之前的自签 JWT — JWT 可被重放且无服务端 revoke)
    code = _save_oauth_code(
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        code_challenge=params["code_challenge"],
        scope=params.get("scope", "mcp"),
    )
    qs = {"code": code}
    if params.get("state"):
        qs["state"] = params["state"]
    redirect = params["redirect_uri"]
    sep = "&" if "?" in redirect else "?"
    return RedirectResponse(f"{redirect}{sep}{urlencode(qs)}", status_code=302)


# ─────────── /token ───────────
def _oauth_error(error, description="", status=400):
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status)


def _issue_tokens(client_id, scope):
    now = int(time.time())
    access = _jwt_encode({"typ": "access", "sub": "user", "client_id": client_id,
                          "scope": scope, "iat": now, "exp": now + ACCESS_TOKEN_TTL})
    refresh = _jwt_encode({"typ": "refresh", "sub": "user", "client_id": client_id,
                           "scope": scope, "iat": now, "exp": now + REFRESH_TOKEN_TTL})
    return JSONResponse({"access_token": access, "token_type": "Bearer",
                         "expires_in": ACCESS_TOKEN_TTL, "refresh_token": refresh,
                         "scope": scope})


async def token_endpoint(request):
    form = await request.form()
    grant = str(form.get("grant_type", ""))
    if grant == "authorization_code":
        code = str(form.get("code", ""))
        verifier = str(form.get("code_verifier", ""))
        client_id = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        if not (code and verifier and client_id and redirect_uri):
            return _oauth_error("invalid_request", "missing params")
        # 服务端一次性 code:pop + delete,任何后续重放都失败。
        entry = _pop_oauth_code(code)
        if not entry:
            return _oauth_error("invalid_grant", "invalid or expired code")
        if entry["client_id"] != client_id:
            return _oauth_error("invalid_grant", "client_id mismatch")
        if entry["redirect_uri"] != redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri mismatch")
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if not hmac.compare_digest(expected, entry["code_challenge"]):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        return _issue_tokens(client_id=client_id, scope=entry["scope"])
    if grant == "refresh_token":
        rt = str(form.get("refresh_token", ""))
        if not rt:
            return _oauth_error("invalid_request", "missing refresh_token")
        p = _jwt_decode(rt)
        if not p or p.get("typ") != "refresh":
            return _oauth_error("invalid_grant", "invalid or expired refresh token")
        rt_client_id = str(p.get("client_id", ""))
        # client 仍然要在 registry 里(注册被吊销 / 服务端文件丢失场景拦截)
        if not _lookup_client(rt_client_id):
            return _oauth_error("invalid_grant", "client no longer registered")
        # 如果请求带 client_id,必须和 refresh token 里的一致(防 token 串用)
        req_client_id = str(form.get("client_id", "")).strip()
        if req_client_id and req_client_id != rt_client_id:
            return _oauth_error("invalid_grant", "client_id mismatch")
        scope = str(p.get("scope", "mcp"))
        return _issue_tokens(client_id=rt_client_id, scope=scope)
    return _oauth_error("unsupported_grant_type", grant)


# ─────────── debug endpoints (auth-gated) ───────────
async def debug_threads(_):
    import traceback
    frames = sys._current_frames()
    out = []
    for t in threading.enumerate():
        ident = t.ident
        fr = frames.get(ident)
        out.append({
            "name": t.name, "ident": ident, "daemon": t.daemon, "alive": t.is_alive(),
            "stack": traceback.format_stack(fr) if fr else [],
        })
    return JSONResponse({"thread_count": len(out), "threads": out})


async def debug_swarm_state(_):
    import pathlib
    runs_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    if not runs_dir.exists():
        return JSONResponse({"error": "runs_dir missing", "path": str(runs_dir)})
    out = {"runs_dir": str(runs_dir), "runs": []}
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        files = []
        for p in run_dir.rglob("*"):
            if p.is_file():
                try:
                    size = p.stat().st_size
                except Exception:
                    size = -1
                files.append({"path": str(p.relative_to(run_dir)), "size": size})
        events_file = run_dir / "events.jsonl"
        recent_events = []
        if events_file.exists():
            try:
                recent_events = events_file.read_text().splitlines()[-20:]
            except Exception as e:
                recent_events = [f"(read error: {e})"]
        out["runs"].append({"id": run_dir.name, "files": files, "recent_events": recent_events})
    return JSONResponse(out)


async def debug_env(_):
    """Dump presence of relevant env vars (values redacted) to debug deploy issues."""
    keys = [
        "MCP_AUTH_TOKEN", "LARK_APP_ID", "LARK_APP_SECRET",
        "FEISHU_VERIFICATION_TOKEN", "FEISHU_DEFAULT_PRESET",
        "FEISHU_DOC_SHARE_ENTITY", "FEISHU_DRIVE_FOLDER_TOKEN",
        "FEISHU_USE_LLM_ROUTER",
        "NOTION_API_KEY", "NOTION_DATABASE_ID", "NOTION_PARENT_PAGE_ID",
        "LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
        "GURU_VIEW_MODE", "GURU_VIEW_MAX",
    ]
    out = {}
    for k in keys:
        v = os.environ.get(k, "")
        if not v:
            out[k] = None
        elif "KEY" in k or "TOKEN" in k or "SECRET" in k:
            out[k] = f"{v[:6]}...REDACTED (len={len(v)})"
        else:
            out[k] = v
    out["_module_constants"] = {
        "NOTION_ENABLED": NOTION_ENABLED,
        "FEISHU_ENABLED": FEISHU_ENABLED,
    }
    return JSONResponse(out)


async def debug_list_feishu_chats(_):
    """List groups + p2p chats the bot is currently a member of, so an operator
    can grab a `chat_id` for one-off republish operations."""
    if not FEISHU_ENABLED:
        return JSONResponse({"error": "feishu disabled"}, status_code=400)
    try:
        token = _feishu_get_tenant_token()
    except Exception as e:
        return JSONResponse({"error": f"tenant_access_token: {e}"}, status_code=500)
    items: list[dict] = []
    page_token = ""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            for _i in range(5):  # cap 5 pages
                params = {"page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                r = await c.get(
                    "https://open.feishu.cn/open-apis/im/v1/chats",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                d = r.json()
                if d.get("code") != 0:
                    return JSONResponse({"error": d.get("msg"), "code": d.get("code")},
                                        status_code=500)
                items.extend(d.get("data", {}).get("items", []) or [])
                page_token = d.get("data", {}).get("page_token") or ""
                if not d.get("data", {}).get("has_more"):
                    break
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    out = [{"chat_id": it.get("chat_id"), "name": it.get("name"),
            "chat_mode": it.get("chat_mode"), "tenant_key": it.get("tenant_key")}
           for it in items]
    return JSONResponse({"count": len(out), "chats": out})


async def debug_fix_historic_doc_share(request):
    """Bulk-set link-share=tenant_readable on every docx the bot owns,
    retroactively fixing docs created before FEISHU_DOC_SHARE_ENTITY was added.

    POST query params:
      entity (default tenant_readable, MUST be in FEISHU_LINK_SHARE_ENTITIES)
      dry_run (default false): if 'true', only list counts, don't patch
      page_cap (default 20, max 40): max pages of 50 files each
    """
    if not FEISHU_ENABLED:
        return JSONResponse({"error": "feishu disabled"}, status_code=400)
    entity = (request.query_params.get("entity") or "tenant_readable").strip()
    if entity not in FEISHU_LINK_SHARE_ENTITIES:
        return JSONResponse(
            {"error": "invalid entity",
             "valid": sorted(FEISHU_LINK_SHARE_ENTITIES)},
            status_code=400)
    dry_run = (request.query_params.get("dry_run") or "").lower() in ("1", "true", "yes")
    page_cap = max(1, min(40, int(request.query_params.get("page_cap") or "20")))

    try:
        token = _feishu_get_tenant_token()
    except Exception as e:
        return JSONResponse({"error": f"tenant_token: {e}"}, status_code=500)

    all_files: list[dict] = []
    page_token = ""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            for _ in range(page_cap):
                params = {"page_size": 50, "order_by": "EditedTime", "direction": "DESC"}
                if page_token:
                    params["page_token"] = page_token
                r = await c.get(
                    "https://open.feishu.cn/open-apis/drive/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                d = r.json()
                if d.get("code") != 0:
                    return JSONResponse(
                        {"error": d.get("msg"), "code": d.get("code"), "stage": "list"},
                        status_code=500)
                all_files.extend(d.get("data", {}).get("files", []) or [])
                page_token = d.get("data", {}).get("next_page_token") or ""
                if not d.get("data", {}).get("has_more"):
                    break
    except Exception as e:
        return JSONResponse({"error": f"list: {type(e).__name__}: {e}"}, status_code=500)

    docxs = [f for f in all_files if f.get("type") == "docx"]

    if dry_run:
        return JSONResponse({
            "total_files": len(all_files),
            "docx_count": len(docxs),
            "dry_run": True,
            "sample": [{"token": f.get("token"), "name": f.get("name")} for f in docxs[:5]],
        })

    ok = 0
    failures: list[str] = []
    for f in docxs:
        tk = f.get("token", "")
        if not tk:
            continue
        if await asyncio.to_thread(_feishu_set_doc_link_share, tk, entity):
            ok += 1
        else:
            failures.append(f.get("name") or tk)
    return JSONResponse({
        "total_files": len(all_files),
        "docx_count": len(docxs),
        "ok": ok,
        "fail": len(failures),
        "entity": entity,
        "first_failures": failures[:5],
    })


async def debug_republish(request):
    """Trigger _publish_terminal_run for a finished run that wasn't auto-published
    (e.g., run was started via MCP run_swarm tool with no Feishu chat context,
    or the disk was wiped by a redeploy before publish could happen).

    POST JSON body: {
      run_id (required), chat_id (required),
      final_report (required: raw markdown report text),
      preset (default investment_committee),
      target (optional),
      chat_type (default chat_id),
      gurus_override (optional list of guru skill names),
      skip_feishu_card (optional bool, default false — skip sending IM card to
        chat. Useful when补 docx/notion 但不想再发卡片到群里)
    }

    Builds a synthetic Run object so we don't depend on disk state being intact.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "POST JSON body required"}, status_code=400)
    run_id = (body.get("run_id") or "").strip()
    chat_id = (body.get("chat_id") or "").strip()
    final_report = body.get("final_report") or ""
    preset = (body.get("preset") or "investment_committee").strip()
    target = (body.get("target") or "").strip()
    chat_type = (body.get("chat_type") or "chat_id").strip() or "chat_id"
    gurus_override = body.get("gurus_override") or []
    skip_feishu_card = bool(body.get("skip_feishu_card", False))
    if not run_id or not chat_id or not final_report:
        return JSONResponse(
            {"error": "run_id, chat_id, final_report all required"},
            status_code=400)

    from types import SimpleNamespace
    fake_run = SimpleNamespace(
        id=run_id,
        status=SimpleNamespace(value="completed"),
        final_report=final_report,
        preset_name=preset,
        user_vars={"target": target} if target else {},
        total_input_tokens=0,
        total_output_tokens=0,
        tasks=[],
    )
    info = {"receive_id": chat_id, "receive_id_type": chat_type,
            "sender_open_id": "", "chat_type": "", "target": target,
            "preset": preset,
            "gurus_override": [g for g in gurus_override
                                if g in _GURU_SKILLS][:GURU_VIEW_MAX],
            "skip_feishu_card": skip_feishu_card}
    try:
        await _publish_terminal_run(fake_run, info)
    except Exception as e:
        return JSONResponse({"error": f"publish: {type(e).__name__}: {e}"},
                            status_code=500)
    return JSONResponse({"ok": True, "run_id": run_id, "chat_id": chat_id})


async def debug_purge_run(request):
    import ctypes, pathlib, shutil
    run_id = request.query_params.get("run_id", "").strip()
    if not run_id:
        return JSONResponse({"error": "run_id required"}, status_code=400)
    out = {"run_id": run_id, "actions": []}
    target_name = f"swarm-{run_id}"
    for t in threading.enumerate():
        if t.name == target_name and t.is_alive() and t.ident:
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(t.ident), ctypes.py_object(SystemExit))
            out["actions"].append(f"async_exc({t.name}) → {res}")
            if res > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(t.ident), 0)
                out["actions"].append("rolled back")
    runs_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    target = runs_dir / run_id
    if target.exists():
        try:
            shutil.rmtree(target)
            out["actions"].append(f"rmtree ok")
        except Exception as e:
            out["actions"].append(f"rmtree failed: {e}")
    else:
        out["actions"].append("dir not found")
    return JSONResponse(out)


# ─────────── extra MCP tool: non-blocking swarm start ───────────
@mcp_server.mcp.tool()
def start_swarm_async(preset_name: str, variables: dict[str, str]) -> str:
    """Start a swarm run and return the run_id immediately (non-blocking).

    Unlike `run_swarm` which blocks for up to 30 minutes polling for completion,
    this tool kicks off the run in a background thread and returns within ~1s.
    Use `get_swarm_status(run_id)` to poll progress, then `get_run_result(run_id)`
    to fetch the final report once status is 'completed' or 'failed'.

    Args:
        preset_name: Swarm preset (use list_swarm_presets to discover).
        variables: Required variables for the preset.

    Returns:
        JSON string with run_id, preset, status="started", and a usage tip.
    """
    from src.swarm.runtime import SwarmRuntime
    from src.swarm.store import SwarmStore
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)
    runtime = SwarmRuntime(store=store)
    try:
        run = runtime.start_run(preset_name, variables)
    except FileNotFoundError as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"status": "error", "error": f"DAG validation failed: {exc}"},
                          ensure_ascii=False)
    return json.dumps({
        "status": "started", "run_id": run.id, "preset": preset_name,
        "tip": ("Run is executing in background. "
                "Use get_swarm_status(run_id) to poll, "
                "get_run_result(run_id) to fetch final report when complete."),
    }, ensure_ascii=False)


@mcp_server.mcp.tool()
def run_a_share_industry_factor_research(
    lookback_days: int = 260,
    test_days: int = 22,
    horizon_days: int = 5,
    top_k: int = 5,
    board_limit: int = 80,
    report_days: int = 7,
    panel_csv: str = "",
    output_format: str = "markdown",
) -> str:
    """Run A-share industry factor analysis, LightGBM prediction, and backtest.

    The workflow fetches Eastmoney industry board data, generates
    QuantsPlaybook-inspired volume-price/timing factors, trains a LightGBM
    regressor when available, backtests recent industry rotation, pulls recent
    Eastmoney research reports, and returns integrated industry recommendations.

    Args:
        lookback_days: Trading-day history used for factor generation.
        test_days: Recent trading days used as out-of-sample backtest window.
        horizon_days: Forward return horizon for model label.
        top_k: Number of industries selected in each rotation basket.
        board_limit: Max Eastmoney industry boards to fetch.
        report_days: Recent research report lookback days.
        panel_csv: Optional local CSV path with date/code/name/open/high/low/close/volume/amount columns.
        output_format: "markdown" for a human report, "json" for raw details.

    Returns:
        Markdown report or JSON string with model, backtest, and recommendation
        details.
    """
    try:
        from factor_analysis import run_industry_factor_research

        result = run_industry_factor_research(
            lookback_days=lookback_days,
            test_days=test_days,
            horizon_days=horizon_days,
            top_k=top_k,
            board_limit=board_limit,
            report_days=report_days,
            panel_csv=panel_csv or None,
        )
    except ImportError as exc:
        return json.dumps(
            {
                "status": "error",
                "error": f"Missing factor-analysis dependency: {exc}",
                "tip": "Install pandas, numpy, scikit-learn, and lightgbm in the runtime.",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
    if output_format.lower() == "json":
        return json.dumps(result, ensure_ascii=False, default=str)
    return result["report_markdown"]


@mcp_server.mcp.tool()
def validate_a_share_february_factor_model(
    train_month: str = "2026-02",
    validate_start: str = "2026-03-01",
    validate_end: str = "2026-05-24",
    top_k: int = 5,
    output_format: str = "markdown",
) -> str:
    """Train the new sector-factor model on February 2026 and validate March-May.

    This is a no-lookahead validation: only February rows whose forward labels
    finish before February month-end are used for training. March-May returns
    are used only for out-of-sample validation.
    """
    try:
        from factor_analysis import run_february_model_validation

        result = run_february_model_validation(
            train_month=train_month,
            validate_start=validate_start,
            validate_end=validate_end,
            top_k=top_k,
        )
    except Exception as exc:
        return json.dumps(
            {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
    if output_format.lower() == "json":
        return json.dumps(result, ensure_ascii=False, default=str)
    return result["report_markdown"]


@mcp_server.mcp.tool()
def run_sequoia_x_scan(
    days: int = 5,
    max_symbols: int = 300,
    datalen: int = 180,
    min_amount: float = 100_000_000,
    rps_threshold: float = 90.0,
    top_per_strategy: int = 10,
    pause_seconds: float = 0.03,
    end_date: str = "",
    include_st: bool = False,
    output_format: str = "markdown",
) -> str:
    """Run Sequoia-X 6-strategy A-share daily scan over recent trading days.

    Distilled from sngyai/Sequoia-X: MaVolume, TurtleTrade, HighTightFlag,
    LimitUpShakeout, UptrendLimitDown, RpsBreakout. Uses Eastmoney/Sina active
    universe and Sina daily kline (amount ≈ close * volume proxy).

    Args:
        days: Recent trading days to evaluate (default 5).
        max_symbols: Max universe size (default 300 active by amount).
        datalen: Per-stock kline history bars (default 180).
        min_amount: TurtleTrade amount filter (default 1e8 yuan).
        rps_threshold: RpsBreakout percentile threshold (default 90).
        top_per_strategy: Top candidates per strategy per day (default 10).
        pause_seconds: Per-stock fetch delay to avoid rate limits.
        end_date: Optional YYYY-MM-DD cutoff for evaluation.
        include_st: Whether to include ST stocks (default false).
        output_format: "markdown" (human) or "json" (raw dict).

    Returns:
        Markdown report (compact Chinese) or JSON string.
    """
    try:
        from sequoia_x import run_weekly_scan, SequoiaScanError
        result = run_weekly_scan(
            days=days, max_symbols=max_symbols, datalen=datalen,
            min_amount=min_amount, rps_threshold=rps_threshold,
            top_per_strategy=top_per_strategy, pause_seconds=pause_seconds,
            end_date=end_date or None, include_st=include_st,
        )
    except SequoiaScanError as exc:
        return json.dumps(
            {"status": "error", "error_type": "SequoiaScanError",
             "error": str(exc)}, ensure_ascii=False)
    except ImportError as exc:
        return json.dumps(
            {"status": "error", "error_type": "ImportError",
             "error": f"Missing sequoia_x dependency: {exc}"},
            ensure_ascii=False)
    except Exception as exc:
        return json.dumps(
            {"status": "error", "error_type": type(exc).__name__,
             "error": str(exc)}, ensure_ascii=False)
    if output_format.lower() == "json":
        return json.dumps(result, ensure_ascii=False, default=str)
    return result["report_markdown"]


# ─────────── Feishu integration ───────────

# Tenant token cache (per-process, thread-safe)
_feishu_token_cache = {"token": None, "expires_at": 0}
_feishu_token_lock = threading.Lock()


def _feishu_get_tenant_token() -> str:
    now = time.time()
    with _feishu_token_lock:
        if _feishu_token_cache["token"] and _feishu_token_cache["expires_at"] > now + 60:
            return _feishu_token_cache["token"]
        with httpx.Client(timeout=10) as c:
            r = c.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET},
            )
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(f"feishu token fetch failed: {d}")
            _feishu_token_cache["token"] = d["tenant_access_token"]
            _feishu_token_cache["expires_at"] = now + int(d.get("expire", 7200))
            return d["tenant_access_token"]


def _feishu_send_text(receive_id: str, receive_id_type: str, text: str) -> dict:
    """Send a plain-text message to a Feishu chat. receive_id_type: chat_id|open_id|user_id|email|union_id."""
    token = _feishu_get_tenant_token()
    body = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    with httpx.Client(timeout=15) as c:
        r = c.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        d = r.json()
        if d.get("code") != 0:
            print(f"[feishu] send failed: {d}", file=sys.stderr, flush=True)
        return d


def _feishu_send_long(receive_id: str, receive_id_type: str, text: str, chunk_size: int = 4500) -> None:
    """Split long text into chunks (Feishu /im/v1/messages caps content at ~30KB but
    UX is better in smaller chunks)."""
    if len(text) <= chunk_size:
        _feishu_send_text(receive_id, receive_id_type, text)
        return
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > chunk_size:
            if buf:
                parts.append(buf)
                buf = ""
        if len(para) > chunk_size:
            # paragraph itself too big — hard-split
            for i in range(0, len(para), chunk_size):
                parts.append(para[i:i + chunk_size])
        else:
            buf = (buf + "\n\n" + para).strip() if buf else para
    if buf:
        parts.append(buf)
    for i, part in enumerate(parts):
        prefix = f"({i + 1}/{len(parts)}) " if len(parts) > 1 else ""
        _feishu_send_text(receive_id, receive_id_type, prefix + part)


# Asset extractor — explicit ticker formats only.
# Named-entity resolution (苹果 → AAPL, 茅台 → 600519.SH, etc.) is delegated
# entirely to the LLM router. The regex below is a strict fallback used only
# when the LLM is unavailable — covers patterns that have no ambiguity:
#   - 6-digit CN A-share codes
#   - HK codes ending in ".HK"
#   - Crypto symbols like BTC-USD
#   - Uppercase US tickers (with common-word blacklist)
# A user typing a Chinese company name with the LLM down will get an
# explicit "didn't understand" reply — fail-loud is better than guessing wrong.

_CN_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_HK_RE = re.compile(r"\b(\d{1,5})\s*[.\.]?\s*HK\b", re.IGNORECASE)
_CRYPTO_RE = re.compile(
    r"\b(BTC|ETH|SOL|BNB|XRP|DOGE|ADA|AVAX|DOT|MATIC|LINK|ATOM|LTC|TRX|SHIB|NEAR|TON)"
    r"(?:[-/]?USD[T]?)?\b",
    re.IGNORECASE,
)
_US_TICKER_RE = re.compile(r"\b([A-Z]{2,5}(?:\.[A-Z])?)\b")  # supports BRK.B

# Words that LOOK like US tickers but aren't (filter for regex fallback only)
_US_COMMON = frozenset({
    "OK","HI","NO","YES","SO","TO","BY","AT","ON","IN","OF","FOR",
    "THE","AND","OR","BUT","IF","AS","BE","DO","GO","WE","YOU","HE","SHE","IT",
    "US","CN","HK","JP","KR","EU","UK","DE","FR","UTC","GMT","MCP","ETF",
    "AI","API","CEO","CFO","CTO","COO","CRO","IPO","DCF","NAV","AUM",
    "PE","PB","PS","EPS","ROE","ROA","WACC","ESG","TA","FA",
    "BUY","HOLD","SELL","LONG","SHORT","CALL","PUT","LIMIT","STOP",
    "Q1","Q2","Q3","Q4","H1","H2","YTD","YOY","QOQ","CAGR","NPV","IRR",
    "MA","MACD","RSI","ADX","KDJ","BOLL","BIAS","VWAP","ATR",
})


def _strip_mentions(text: str) -> str:
    """Strip Feishu @-mention placeholders.

    Group chat messages may include `<at user_id="ou_xxx">name</at>` markup or
    `@_user_1`-style placeholders. Both are noise for our entity extractor.
    """
    # 1. XML-style at-mentions
    text = re.sub(r"<at[^>]*?>.*?</at>", " ", text, flags=re.DOTALL)
    # 2. Open-tag-only variant (rare)
    text = re.sub(r"<at[^>]*?/>", " ", text)
    # 3. @placeholder or @name space
    text = re.sub(r"@\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_target(text: str) -> tuple[str | None, str | None]:
    """Regex-only fallback. Returns (target, market) or (None, None).

    Only recognizes explicit, unambiguous ticker formats:
      - 6-digit CN A-share         → 600519 → 600519.SH
      - HK code with .HK suffix    → 1810.HK
      - Crypto symbol              → BTC → BTC-USD
      - Uppercase US ticker         → SOXL / AAPL / BRK.B

    Named-entity resolution (苹果 / 茅台 / 小米 / ...) is handled by the LLM
    router. If LLM is unavailable and the user types a name, this returns
    None and the caller surfaces a help message.
    """
    # 1. CN A-share
    m = _CN_RE.search(text)
    if m:
        code = m.group(1)
        suffix = ".SH" if code.startswith(("6", "9")) else ".SZ"
        return f"{code}{suffix}", "CN"

    # 2. HK with explicit .HK or "HK" suffix
    m = _HK_RE.search(text)
    if m:
        code = m.group(1).lstrip("0") or "0"
        return f"{int(code)}.HK", "HK"

    # 3. Crypto explicit token
    m = _CRYPTO_RE.search(text)
    if m:
        return f"{m.group(1).upper()}-USD", "CRYPTO"

    # 4. US uppercase ticker — blacklist filter, prefer longest
    candidates = [c for c in _US_TICKER_RE.findall(text)
                  if c not in _US_COMMON and len(c) >= 2]
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0], "US"

    return None, None


# ─── Intent classification: text → swarm preset ───
# Patterns are evaluated in order; first match wins. Place more specific terms first.
_INTENT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:技术[面分]|技术分析|技术图形|形态|波浪|harmonic|technical|TA(?:\b|$)|ichimoku|smc)", re.I), "technical_analysis_panel"),
    (re.compile(r"(?:财报|业绩|earnings|季报|年报|中报|EPS|营收)", re.I), "earnings_research_desk"),
    (re.compile(r"(?:基本面|基础研究|fundamental|估值)", re.I), "fundamental_research_team"),
    (re.compile(r"(?:风险|风控|风险审查|尾部风险|CRO|risk)", re.I), "risk_committee"),
    (re.compile(r"(?:量化|quant|策略回测|因子|backtest)", re.I), "quant_strategy_desk"),
    (re.compile(r"(?:商品|大宗|期货|铜|铁矿|原油|黄金现货|gold|copper|crude)", re.I), "commodity_research_team"),
    (re.compile(r"(?:宏观|macro|利率|汇率|FX|央行|通胀|CPI|PPI)", re.I), "macro_strategy_forum"),
    (re.compile(r"(?:板块|行业|轮动|sector\s*rotation)", re.I), "sector_rotation_team"),
    (re.compile(r"(?:期权|衍生品|option|derivative|gamma|vol\s*surface)", re.I), "derivatives_strategy_desk"),
    (re.compile(r"(?:配对|pairs?|相对价值|relative\s*value)", re.I), "pairs_research_lab"),
    (re.compile(r"(?:事件驱动|催化剂|catalyst|event[-\s]*driven|并购|分拆)", re.I), "event_driven_task_force"),
    (re.compile(r"(?:情绪|sentiment|舆情)", re.I), "sentiment_intelligence_team"),
    (re.compile(r"(?:基金|fund\s*selection|ETF\s*选)", re.I), "fund_selection_panel"),
    (re.compile(r"(?:信用|信用债|credit|高收益|HY\b)", re.I), "credit_research_team"),
    (re.compile(r"(?:因子研究|factor\s*research)", re.I), "factor_research_committee"),
    (re.compile(r"(?:全球配置|global\s*allocation|大类资产)", re.I), "global_allocation_committee"),
    (re.compile(r"(?:加密|crypto|区块链|链上)", re.I), "crypto_research_lab"),
    (re.compile(r"(?:机器学习|ML\s*量化|deep\s*learning|神经网络)", re.I), "ml_quant_lab"),
    (re.compile(r"(?:投委会|投资委员会|完整分析|全面分析|综合分析|investment\s*committee)", re.I), "investment_committee"),
]


def _classify_preset(text: str, default: str) -> str:
    """Pick the swarm preset based on intent keywords. Falls back to default."""
    for pattern, preset in _INTENT_RULES:
        if pattern.search(text):
            return preset
    return default


# Explicit preset override: "preset:technical_analysis_panel SOXL" or "/preset xxx"
_PRESET_OVERRIDE_RE = re.compile(r"(?:preset[:=]\s*|/preset\s+)([a-z_]+)", re.I)
_FACTOR_RESEARCH_RE = re.compile(
    r"(?:(?:行业|板块).*(?:因子|量化|lightgbm|机器学习|回测|轮动|预测)"
    r"|(?:因子|量化|lightgbm|机器学习|回测|轮动|预测).*(?:行业|板块))",
    re.I,
)


def _parse_explicit_preset(text: str) -> tuple[str | None, str]:
    """Return (preset_name or None, cleaned_text). Strips the preset directive
    from text so downstream extraction isn't polluted."""
    m = _PRESET_OVERRIDE_RE.search(text)
    if not m:
        return None, text
    preset = m.group(1).strip()
    cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
    return preset, cleaned


def _is_factor_research_request(text: str) -> bool:
    return bool(_FACTOR_RESEARCH_RE.search(text or ""))


# Sequoia-X A 股选股扫描 — 6 策略日线
_SEQUOIA_RE = re.compile(
    r"sequoia[-\s]?x|sequoia\b|红杉(?:策略|选股|x|扫描)?|海龟突破|"
    r"RPS\s*突破|涨停洗盘|高(?:位)?(?:窄|紧)幅?旗形",
    re.I,
)


def _is_sequoia_scan_request(text: str) -> bool:
    return bool(_SEQUOIA_RE.search(text or ""))


# ─────────── LLM-powered intent router (primary path) ───────────
# Routes natural-language Feishu messages into structured actions. Falls back
# to regex if LLM is unreachable / returns invalid output.

FEISHU_USE_LLM_ROUTER = os.environ.get("FEISHU_USE_LLM_ROUTER", "true").lower() in ("1", "true", "yes", "on")

KNOWN_PRESETS = frozenset({
    "investment_committee", "technical_analysis_panel", "earnings_research_desk",
    "fundamental_research_team", "risk_committee", "quant_strategy_desk",
    "macro_strategy_forum", "macro_rates_fx_desk", "commodity_research_team",
    "crypto_research_lab", "crypto_trading_desk", "derivatives_strategy_desk",
    "sector_rotation_team", "pairs_research_lab", "event_driven_task_force",
    "sentiment_intelligence_team", "fund_selection_panel", "credit_research_team",
    "factor_research_committee", "global_allocation_committee", "ml_quant_lab",
    "global_equities_desk", "geopolitical_war_room", "portfolio_review_board",
    "social_alpha_team", "statistical_arbitrage_desk", "equity_research_team",
    "convertible_bond_team",
})
KNOWN_ACTIONS = frozenset({"run_swarm", "list_runs", "status", "cancel_run",
                           "help", "presets", "clarify", "reject"})

LLM_ROUTER_SYSTEM_PROMPT = """你是一个交易研究 Bot 的命令路由器。用户在飞书发自然语言消息,你需要判断意图,并仅返回**严格 JSON**(不要任何额外文字、Markdown、解释)。

## 支持的 action(8 种)

- `run_swarm`: 跑新分析。字段 `preset`、`target`、`market`、可选 `gurus`(数组,1-2 个 A 股游资 skill 名,见下表)
- `list_runs`: 列历史 run。可选字段 `status_filter` (`completed`/`failed`/`running`/`cancelled`),可选 `limit`(默认 10)
- `status`: 获取某 run 的报告或当前进度。字段 `run_id`(支持特殊值 `"latest"` = 最近一次 completed 的 run)
- `cancel_run`: 杀掉一个卡死/不想要的 run。字段 `run_id`(也支持 `"latest"`)
- `help`: 显示用法
- `presets`: 列出所有可用 preset
- `clarify`: 你不确定意图,返回追问。字段 `message`
- `reject`: 超出能力范围,礼貌拒绝。字段 `message`

## 28 个 swarm preset(按意图选)

- `investment_committee` 完整投委会(bull/bear/risk/PM)— 默认首选,综合分析
- `technical_analysis_panel` 纯技术面(K线/形态/MACD/RSI/波浪/SMC/Ichimoku)
- `earnings_research_desk` 财报研究(季报/年报/EPS/营收)
- `fundamental_research_team` 基本面深度(估值/ROE/护城河)
- `risk_committee` 风险审查(VaR/尾部风险/CRO视角)
- `quant_strategy_desk` 量化策略 + 回测
- `macro_strategy_forum` 宏观策略(利率/通胀/美联储)
- `macro_rates_fx_desk` 利率汇率
- `commodity_research_team` 大宗商品(铜/铁矿/原油/黄金)
- `crypto_research_lab` 加密(链上 + 基本面)
- `crypto_trading_desk` 加密交易策略
- `derivatives_strategy_desk` 期权/衍生品(gamma/vol surface)
- `sector_rotation_team` 板块轮动
- `pairs_research_lab` 配对交易
- `event_driven_task_force` 事件驱动(并购/分拆/催化)
- `sentiment_intelligence_team` 情绪/舆情
- `fund_selection_panel` 基金/ETF 筛选
- `credit_research_team` 信用债/高收益
- `factor_research_committee` 因子研究
- `global_allocation_committee` 全球大类配置
- `ml_quant_lab` ML 量化(神经网络/深度学习)
- `geopolitical_war_room` 地缘政治
- `portfolio_review_board` 组合审议
- `statistical_arbitrage_desk` 统计套利
- `social_alpha_team` 社交 alpha
- `equity_research_team` 股票研究
- `global_equities_desk` 全球股票
- `convertible_bond_team` 可转债

## 10 位 A 股游资 skill(可选,只用于 stock_decision 类 preset)

用户可以指定 1-2 位游资来给报告下方观点。识别用户消息里的游资名/派别后,在输出 JSON 里加 `gurus` 字段(skill 名数组,最多 2 个)。**只对 A 股相关分析有效**,美股/港股/加密/macro 不要带 gurus 字段。

| skill 名 | 中文/别名 | 派别 |
|---|---|---|
| `xiao-eyu` | 小鳄鱼 | 理解力派(通用) |
| `bei-jing-chao-jia` | 北京炒家 | 模式派(首板战法) |
| `chen-xiao-qun` | 陈小群、群神 | 龙头信仰派(主升浪锁仓) |
| `jiu-er-ke-bi` | 92 科比、科比 | 情绪周期派(高低切) |
| `nie-pan-chong-sheng` | 涅盘重升、升大 | 资金流派(强势形态低吸) |
| `yi-shun-liu-guang` | 一瞬流光、光神 | 高位接力派(锁 2 板) |
| `xiang-cheng-cai-lian-lu` | 采莲路、川哥 | 控回撤派(4 点底线) |
| `xiao-rui-rui` | 小睿睿、睿神、小睿睿8 | 进攻派(敢上重仓) |
| `hua-dong-da-dao-dan` | 华东大导弹、大导弹 | 低频狙击派(空仓为主) |
| `gui-yin` | 归因 | 资讯派(逻辑驱动低吸) |

例子:

输入: `用陈小群视角看下 茅台`
输出: `{"action":"run_swarm","preset":"investment_committee","target":"600519.SH","market":"CN","gurus":["chen-xiao-qun"]}`

输入: `分析 002594,用北京炒家和小鳄鱼的玩法`
输出: `{"action":"run_swarm","preset":"investment_committee","target":"002594.SZ","market":"CN","gurus":["bei-jing-chao-jia","xiao-eyu"]}`

输入: `小睿睿会怎么看 中际旭创`
输出: `{"action":"run_swarm","preset":"investment_committee","target":"300308.SZ","market":"CN","gurus":["xiao-rui-rui"]}`

输入: `控回撤派看 隆基`
输出: `{"action":"run_swarm","preset":"investment_committee","target":"601012.SH","market":"CN","gurus":["xiang-cheng-cai-lian-lu"]}`

输入: `用龙头信仰派+情绪周期派分析 比亚迪`
输出: `{"action":"run_swarm","preset":"investment_committee","target":"002594.SZ","market":"CN","gurus":["chen-xiao-qun","jiu-er-ke-bi"]}`

注意:用户没指定游资时**不要**加 gurus 字段——会由系统自动 LLM 路由选 1-2 位。

## target 标准格式

- US 美股: `AAPL`, `NVDA`, `SOXL`, `BRK.B`(全大写)
- HK 港股: `1810.HK`, `700.HK`, `981.HK`(数字.HK)
- CN A 股: `600519.SH`(沪市,代码 6/9 开头), `000333.SZ`(深市)
- Crypto: `BTC-USD`, `ETH-USD`, `SOL-USD`
- 商品: `copper`, `gold`, `crude`(英文名)

## market 取值

`US` | `HK` | `CN` | `CRYPTO` | `GLOBAL`

## 名称 → ticker(运用你的世界知识 + 财经常识自行推断,不限于下面列举)

常见对照参考(非完整列表,你需要扩展):
- 中国 A 股:茅台→600519.SH, 五粮液→000858.SZ, 宁王/宁德时代→300750.SZ, 招行→600036.SH, 隆基→601012.SH, 德业→605117.SH...
- 港股:腾讯→700.HK, 小米→1810.HK, 美团→3690.HK, 中芯国际→981.HK, 比亚迪→1211.HK...
- 中概 ADR:阿里→BABA, 京东→JD, 拼多多→PDD, 蔚来→NIO, 理想→LI, 小鹏→XPEV, 台积电→TSM, 网易→NTES, B站→BILI...
- US:苹果→AAPL, 微软→MSFT, 英伟达→NVDA, 特斯拉→TSLA, Meta→META, 谷歌→GOOGL, 亚马逊→AMZN, 高通→QCOM, 博通→AVGO, 高盛→GS...
- ETF/指数:标普→SPY, 纳指→QQQ, 罗素→IWM, 道指→DIA, VIX→VIX, 黄金→GLD, 原油→USO...
- Crypto:比特币→BTC-USD, 以太坊→ETH-USD, 索拉纳→SOL-USD, 狗狗→DOGE-USD...

碰到没列出的中文公司名(如"招商蛇口"、"洛阳钼业"、"东方甄选"),根据你的训练知识返回正确 ticker。不确定时返回 `clarify` 让用户澄清。

## 例子

输入: `分析苹果`
输出: `{"action":"run_swarm","preset":"investment_committee","target":"AAPL","market":"US"}`

输入: `帮我看下英伟达最近技术面怎么样`
输出: `{"action":"run_swarm","preset":"technical_analysis_panel","target":"NVDA","market":"US"}`

输入: `茅台最新季报数据`
输出: `{"action":"run_swarm","preset":"earnings_research_desk","target":"600519.SH","market":"CN"}`

输入: `做个小米的风险评估`
输出: `{"action":"run_swarm","preset":"risk_committee","target":"1810.HK","market":"HK"}`

输入: `BTC 链上活跃度`
输出: `{"action":"run_swarm","preset":"crypto_research_lab","target":"BTC-USD","market":"CRYPTO"}`

输入: `半导体板块怎么样`
输出: `{"action":"run_swarm","preset":"sector_rotation_team","target":"半导体","market":"GLOBAL"}`

输入: `最近跑过哪些分析`
输出: `{"action":"list_runs"}`

输入: `只看 completed 的`
输出: `{"action":"list_runs","status_filter":"completed"}`

输入: `失败的 run 有哪些`
输出: `{"action":"list_runs","status_filter":"failed"}`

输入: `最近 5 个`
输出: `{"action":"list_runs","limit":5}`

输入: `当前在跑的`
输出: `{"action":"list_runs","status_filter":"running"}`

输入: `查一下 swarm-20260506-171102-016a0768`
输出: `{"action":"status","run_id":"swarm-20260506-171102-016a0768"}`

输入: `把最新的报告发我`
输出: `{"action":"status","run_id":"latest"}`

输入: `刚跑完的那个`
输出: `{"action":"status","run_id":"latest"}`

输入: `取消 swarm-20260506-171102-016a0768`
输出: `{"action":"cancel_run","run_id":"swarm-20260506-171102-016a0768"}`

输入: `把当前在跑的干掉`
输出: `{"action":"cancel_run","run_id":"latest"}`

输入: `怎么用`
输出: `{"action":"help"}`

输入: `有哪些 preset`
输出: `{"action":"presets"}`

输入: `阿巴阿巴`
输出: `{"action":"clarify","message":"没看懂,能具体说想分析什么资产吗?比如 '分析 SOXL'"}`

输入: `帮我做菜`
输出: `{"action":"reject","message":"我只能做金融分析,做菜帮不了你"}`

只输出 JSON,不要任何多余字符。"""


async def _llm_route(text: str) -> dict | None:
    """Call DeepSeek to route a Feishu message into a structured action.

    Returns the parsed dict on success, or None on any failure
    (network error, invalid JSON, unknown preset/action) — caller should
    fall back to regex-based routing.

    NOTE: 用 _deepseek_json_call 收口,与 summarizer / guru route / guru voice
    共享截断检测 / 错误日志 / 90s read timeout。max_tokens 升到 1500 给推理留量。
    """
    if not FEISHU_USE_LLM_ROUTER:
        print(f"[feishu/route] LLM router disabled by env", flush=True)
        return None
    print(f"[feishu/route] input: text={text[:120]!r}", flush=True)
    parsed, _err = await _deepseek_json_call(
        system=LLM_ROUTER_SYSTEM_PROMPT, user=text, max_tokens=1500,
        temperature=0, label="feishu/route", run_id="",
    )
    if not parsed or not isinstance(parsed, dict):
        print(f"[feishu/route] LLM returned None or non-dict", flush=True)
        return None
    action = parsed.get("action")
    if action not in KNOWN_ACTIONS:
        print(f"[feishu/route] reject: unknown action={action!r} "
              f"(valid: {sorted(KNOWN_ACTIONS)})", flush=True)
        return None
    if action == "run_swarm":
        preset = parsed.get("preset")
        if preset not in KNOWN_PRESETS:
            print(f"[feishu/route] reject: unknown preset={preset!r}", flush=True)
            return None
        if not parsed.get("target"):
            print(f"[feishu/route] reject: action=run_swarm but no target "
                  f"in parsed={parsed!r}", flush=True)
            return None
    if action == "status" and not parsed.get("run_id"):
        print(f"[feishu/route] reject: action=status but no run_id", flush=True)
        return None
    print(f"[feishu/route] ok: action={action} preset={parsed.get('preset')} "
          f"target={parsed.get('target')} gurus={parsed.get('gurus')}", flush=True)
    return parsed


# Pending runs the bot needs to follow up on
_feishu_pending: dict[str, dict] = {}
_feishu_pending_lock = threading.Lock()

# Event-id dedup: Feishu retries events that don't receive 200 within ~3s.
# Even though we return 200 quickly, the LLM router runs async; under load it
# can still take long enough that a single user message triggers multiple
# handler invocations. Track recent event_ids to drop duplicates.
_seen_event_ids: dict[str, float] = {}
_seen_event_lock = threading.Lock()
_EVENT_DEDUP_TTL_SEC = 3600


# Feishu metadata persistence — written to disk so the bot can resume publishing
# after container restart (in-memory _feishu_pending dict alone is lost on restart).
def _feishu_meta_path(run_id: str):
    import pathlib
    return (mcp_server.AGENT_DIR /
            ".swarm" / "runs" / run_id / "feishu_meta.json")


def _write_feishu_meta(run_id: str, chat_id: str, receive_id_type: str,
                        sender_open_id: str, chat_type: str = "",
                        target: str = "", preset: str = "",
                        gurus_override: list[str] | None = None) -> None:
    """Persist routing info for this run so we can publish back to the right chat
    even after a container restart."""
    p = _feishu_meta_path(run_id)
    if not p.parent.exists():
        return  # run dir doesn't exist yet
    try:
        p.write_text(json.dumps({
            "receive_id": chat_id,
            "receive_id_type": receive_id_type or "chat_id",
            "sender_open_id": sender_open_id or "",
            "chat_type": chat_type or "",  # 'p2p' or 'group'
            "target": target or "",
            "preset": preset or "",
            "gurus_override": gurus_override or [],
            "created_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[feishu] write meta {run_id} err: {e}", flush=True)


def _load_feishu_meta(run_id: str) -> dict | None:
    p = _feishu_meta_path(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[feishu] load meta {run_id} err: {e}", flush=True)
        return None


def _restore_feishu_pending_from_disk() -> int:
    """Scan disk for running swarm runs that have feishu_meta, restore them to
    the in-memory pending dict. Called once at startup."""
    import pathlib
    from src.swarm.store import SwarmStore
    from src.swarm.models import RunStatus
    runs_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    if not runs_dir.exists():
        return 0
    store = SwarmStore(base_dir=runs_dir)
    restored = 0
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        rid = run_dir.name
        meta = _load_feishu_meta(rid)
        if not meta:
            continue
        try:
            run = store.load_run(rid)
        except Exception:
            continue
        if run is None:
            continue
        # Restore both running and recently-terminal runs that haven't been
        # published yet (i.e., still on disk → we may need to push their result).
        # The poll loop will then drain terminal ones.
        if run.status in (RunStatus.running, RunStatus.completed,
                          RunStatus.failed, RunStatus.cancelled):
            with _feishu_pending_lock:
                if rid not in _feishu_pending:
                    _feishu_pending[rid] = meta
                    restored += 1
    return restored


def _is_duplicate_feishu_event(event_id: str) -> bool:
    """Return True if this event_id was seen recently. Side effect: records the
    event_id with current timestamp (so subsequent calls within TTL return True).
    Empty event_id → not deduped (treated as unique)."""
    if not event_id:
        return False
    now = time.time()
    with _seen_event_lock:
        # Periodic GC
        if len(_seen_event_ids) > 2000:
            cutoff = now - _EVENT_DEDUP_TTL_SEC
            for k in list(_seen_event_ids.keys()):
                if _seen_event_ids[k] < cutoff:
                    _seen_event_ids.pop(k, None)
        if event_id in _seen_event_ids:
            return True
        _seen_event_ids[event_id] = now
        return False


# ─────────── LLM-based structured summarizer (DeepSeek) ───────────

# Preset → template family. Summarizer prompt + renderers branch on this so
# each preset shows fields that actually make sense for its output type.
PRESET_TEMPLATE: dict[str, str] = {
    # ── Stock-decision: 个股决策(决策 + 目标价 + 多空 + 风险)──
    "investment_committee": "stock_decision",
    "technical_analysis_panel": "stock_decision",
    "earnings_research_desk": "stock_decision",
    "fundamental_research_team": "stock_decision",
    "risk_committee": "stock_decision",
    "derivatives_strategy_desk": "stock_decision",
    "credit_research_team": "stock_decision",
    "crypto_research_lab": "stock_decision",
    "crypto_trading_desk": "stock_decision",
    "commodity_research_team": "stock_decision",
    "equity_research_team": "stock_decision",
    "global_equities_desk": "stock_decision",
    # ── Macro / theme: 宏观/板块/事件(立场 + 机会 + 风险)──
    "macro_strategy_forum": "macro_theme",
    "macro_rates_fx_desk": "macro_theme",
    "sector_rotation_team": "macro_theme",
    "event_driven_task_force": "macro_theme",
    "sentiment_intelligence_team": "macro_theme",
    "fund_selection_panel": "macro_theme",
    "geopolitical_war_room": "macro_theme",
    "portfolio_review_board": "macro_theme",
    "social_alpha_team": "macro_theme",
    "quant_strategy_desk": "macro_theme",
    "statistical_arbitrage_desk": "macro_theme",
    # ── Research / allocation: 因子/模型/配对/资产配置 ──
    "pairs_research_lab": "research_alloc",
    "factor_research_committee": "research_alloc",
    "global_allocation_committee": "research_alloc",
    "ml_quant_lab": "research_alloc",
    "convertible_bond_team": "research_alloc",
}


_TEMPLATE_INSTRUCTIONS = {
    "stock_decision": """==== 模板:个股决策(stock_decision)====
适用:investment_committee / technical_analysis_panel / earnings_research_desk / 等。

badge 必选其一:买入 / 卖出 / 持有 / 条件性多头 / 条件性空头 / 回避 / 关注 / 中性
badge_color 映射:买入/条件性多头/关注→green;卖出/条件性空头→red;持有/中性→blue;回避→grey

kv_fields 顺序填这些(没找到填 "(未提及)",但 label 必须保留):
  [{"label":"决策","value":"<同 badge>"},
   {"label":"现价","value":"¥150.63"},
   {"label":"目标价","value":"¥320-400"},
   {"label":"止损","value":"¥250"},
   {"label":"仓位","value":"2% NAV"},
   {"label":"持有期","value":"3-6 个月"},
   {"label":"信心","value":"高|中|低"}]

sections 必须正好 3 个,顺序:
  [{"label":"🐂 多头论据","items":[3-6 条多头论点,每项 ≤50 字]},
   {"label":"🐻 空头论据","items":[3-6 条空头论点]},
   {"label":"⚠️ 核心风险","items":[3-6 条风险]}]

key_metrics:出现过的财务/技术指标 dict(PE/ROE/RSI/MACD/...)
actions_or_catalysts:{"label":"🎯 催化剂","items":["Q3 财报","..."]}""",

    "macro_theme": """==== 模板:宏观/板块/主题(macro_theme)====
适用:macro_strategy_forum / sector_rotation_team / event_driven / sentiment / 等。

badge 必选其一:看多 / 看空 / 中性 / 超配 / 低配 / 关注 / 回避
badge_color 映射:看多/超配/关注→green;看空/低配→red;中性→blue;回避→grey

kv_fields 顺序填(灵活,4-6 条):
  [{"label":"立场","value":"<同 badge>"},
   {"label":"时间维度","value":"3-6 个月"},
   {"label":"信心","value":"高|中|低"},
   {"label":"涉及板块/区域","value":"..."}]

sections 必须正好 3 个,顺序:
  [{"label":"💡 核心观点","items":[3-6 条]},
   {"label":"🚀 机会","items":[3-6 条]},
   {"label":"⚠️ 风险","items":[3-6 条]}]

key_metrics:涉及的宏观/板块指标(GDP / CPI / 利率 / 油价 / 行业 PE / ...)
actions_or_catalysts:{"label":"🎯 催化剂","items":["FOMC 会议","..."]}""",

    "research_alloc": """==== 模板:研究/配置(research_alloc)====
适用:pairs_research_lab / factor_research_committee / global_allocation / ml_quant_lab / 等。

badge 必选其一:推荐 / 谨慎 / 中性 / 待验证 / 待优化 / 不建议
badge_color 映射:推荐→green;不建议/谨慎→red;中性/待验证→blue;待优化→orange

kv_fields 顺序填(4-6 条):
  [{"label":"结论","value":"<同 badge>"},
   {"label":"方法","value":"<回测/统计/ML 模型/配对/...>"},
   {"label":"信心","value":"高|中|低"},
   {"label":"适用范围","value":"..."}]

sections 必须正好 3 个,顺序:
  [{"label":"🔍 主要发现","items":[3-6 条]},
   {"label":"🛠 方法/参数","items":[3-6 条]},
   {"label":"⚠️ 注意事项","items":[3-6 条]}]

key_metrics:关键统计量(夏普 / 胜率 / 最大回撤 / IC / IR / 相关性 / ...)
actions_or_catalysts:{"label":"📋 建议行动","items":["纳入因子库","..."]}""",
}


def _build_summarizer_prompt(template: str) -> str:
    tpl_addon = _TEMPLATE_INSTRUCTIONS.get(template,
                                            _TEMPLATE_INSTRUCTIONS["stock_decision"])
    return f"""你是金融报告结构化助手。给你一份 swarm 多 agent 协作的最终输出(英文或中英混合 markdown),抽取并翻译为**中文结构化 JSON**。

只输出 JSON,不要解释、不要 markdown 围栏。

通用 schema(所有 template 共享):

{{
  "template": "{template}",
  "title": "<中文标的/主题名> (<原 ticker 或 主题英文>) — <preset 中文名>",
  "badge": "<按下面模板说明挑一个>",
  "badge_color": "green|red|blue|grey|orange",
  "headline": "1-2 句最核心结论(15-40 字)",
  "tldr": "200-350 字的中文综述,流畅自然 — 用于飞书 docx 和 Notion 等长内容",
  "short_tldr": "80-120 字的精炼综述 — 用于飞书互动卡片(必须比 tldr 更紧凑,只留最核心的判断和理由)",
  "kv_fields": [{{"label":"...","value":"..."}}, ...],
  "sections": [{{"label":"...","items":["...", "..."]}}, ...],
  "key_metrics": {{"指标名":"值", ...}},
  "actions_or_catalysts": {{"label":"...","items":["...", "..."]}}
}}

{tpl_addon}

通用要求:
- 全中文(ticker / 数字单位 / 英文专有名词保留)
- 不要 hallucinate,原文没说的就写 "(未提及)" 或省略数组项
- 数字保留原始货币符号
- title 必须包含原文 ticker 或主题英文名
"""


# ─────────── 游资观点 (multi-guru) addendum ───────────
# 10 位游资 voice，每次分析 LLM 路由选 1-2 个互补的派别生成观点。
# 模式由 GURU_VIEW_MODE 控制：auto（LLM 路由）/ fixed:name1,name2 / off。

GURU_LIST = [
    "xiao-eyu", "bei-jing-chao-jia", "chen-xiao-qun", "jiu-er-ke-bi",
    "nie-pan-chong-sheng", "yi-shun-liu-guang", "xiang-cheng-cai-lian-lu",
    "xiao-rui-rui", "hua-dong-da-dao-dan", "gui-yin",
]

# (中文名, 派别) — 卡片/文档/Notion 标题用。
GURU_META: dict[str, tuple[str, str]] = {
    "xiao-eyu":                ("小鳄鱼", "理解力派"),
    "bei-jing-chao-jia":       ("北京炒家", "模式派"),
    "chen-xiao-qun":           ("陈小群", "龙头信仰派"),
    "jiu-er-ke-bi":            ("92 科比", "情绪周期派"),
    "nie-pan-chong-sheng":     ("涅盘重升", "资金流派"),
    "yi-shun-liu-guang":       ("一瞬流光", "高位接力派"),
    "xiang-cheng-cai-lian-lu": ("采莲路", "控回撤派"),
    "xiao-rui-rui":            ("小睿睿", "进攻派"),
    "hua-dong-da-dao-dan":     ("华东大导弹", "低频狙击派"),
    "gui-yin":                 ("归因", "资讯派"),
}

GURU_VIEW_MODE = os.environ.get("GURU_VIEW_MODE", "auto").strip().lower()
GURU_VIEW_MAX = max(1, min(3, int(os.environ.get("GURU_VIEW_MAX", "2") or "2")))


def _extract_guru_profile(skill_md: str) -> str:
    """Extract the frontmatter description as the routing profile (~300 chars)."""
    m = re.search(r"^---\n(.+?)\n---", skill_md, re.DOTALL)
    if not m:
        return skill_md[:300]
    fm = m.group(1)
    # Greedy match of description value until the next top-level YAML key or end.
    desc_m = re.search(r"description:\s*(.+?)(?=\n[a-zA-Z_]+:\s|\Z)", fm, re.DOTALL)
    return (desc_m.group(1).strip() if desc_m else "")[:600]


def _load_all_guru_skills() -> tuple[dict[str, str], dict[str, str]]:
    """Returns (profiles_for_routing, full_skill_md_for_voicing)."""
    profiles: dict[str, str] = {}
    full: dict[str, str] = {}
    try:
        from src.agent.skills import SkillsLoader
        skills_dir = str(SkillsLoader().skills_dir)
    except Exception as e:
        print(f"[guru] SkillsLoader unavailable: {e}", flush=True)
        return profiles, full
    for name in GURU_LIST:
        path = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(path):
            print(f"[guru] missing: {name}", flush=True)
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            full[name] = text
            profiles[name] = _extract_guru_profile(text)
        except Exception as e:
            print(f"[guru] read {name} failed: {e}", flush=True)
    return profiles, full


_GURU_PROFILES, _GURU_SKILLS = _load_all_guru_skills()
print(f"[guru] loaded {len(_GURU_SKILLS)}/{len(GURU_LIST)} gurus "
      f"(mode={GURU_VIEW_MODE}, max={GURU_VIEW_MAX})", flush=True)


def _get_llm_creds() -> tuple[str, str, str]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "").strip()
    base_url = (os.environ.get("DEEPSEEK_BASE_URL")
                or os.environ.get("OPENROUTER_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LANGCHAIN_MODEL_NAME", "deepseek-v4-pro").strip()
    return api_key, base_url, model


async def _deepseek_json_call(*, system: str, user: str, max_tokens: int,
                               temperature: float = 0.1,
                               label: str = "deepseek",
                               run_id: str = "") -> tuple[dict | None, str]:
    """Single call to DeepSeek expecting a JSON object response.

    Returns (parsed_dict, error_str). On success, error_str is "".

    Centralized handling for the reasoning-model failure modes that bit us:
      - finish_reason=length → truncated → JSON parse will always fail,
        return early with clear label (don't waste retries on truncation)
      - Empty content → reasoning model spent budget on CoT,
        DO NOT fall back to reasoning_content (that's free-form thought)
      - JSON parse failures → log content snippet for diagnosis
      - Network/HTTP/timeout → label-tagged log line

    Read timeout 90s — DeepSeek v4-pro reasoning + long output (4-6K tokens)
    can take 30-70s in practice. Tight 60s gave us flaky ReadTimeouts.

    Callers handle retry strategy (temperature bump, etc.).
    """
    api_key, base_url, model = _get_llm_creds()
    if not api_key:
        return None, "no api key"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=90, write=15, pool=5),
        ) as c:
            r = await c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            if r.status_code != 200:
                err = f"HTTP {r.status_code}: {r.text[:200]}"
                print(f"[{label}] {err} run={run_id}", flush=True)
                return None, err
            d = r.json()
            choice = d["choices"][0]
            msg = choice.get("message") or {}
            finish_reason = choice.get("finish_reason") or ""
            content = (msg.get("content") or "").strip()
            if finish_reason == "length":
                reasoning_len = len(msg.get("reasoning_content") or "")
                err = (f"truncated (finish=length, content_len={len(content)}, "
                       f"reasoning_len={reasoning_len}, max_tokens={max_tokens})")
                print(f"[{label}] {err} run={run_id}", flush=True)
                return None, err
            if not content:
                reasoning_len = len(msg.get("reasoning_content") or "")
                err = (f"empty content (reasoning_len={reasoning_len}, "
                       f"finish={finish_reason})")
                print(f"[{label}] {err} run={run_id}", flush=True)
                return None, err
            m = re.search(r"\{[\s\S]*\}", content)
            if not m:
                err = f"no JSON object (content[:200]={content[:200]!r})"
                print(f"[{label}] {err} run={run_id}", flush=True)
                return None, err
            try:
                return json.loads(m.group(0)), ""
            except json.JSONDecodeError as je:
                err = (f"JSONDecodeError: {je} (content_len={len(content)}, "
                       f"finish={finish_reason}, snippet={content[:200]!r})")
                print(f"[{label}] {err} run={run_id}", flush=True)
                return None, err
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[{label}] exception {err} run={run_id}", flush=True)
        return None, err


async def _route_gurus(summary: dict, full_report: str, run_id: str) -> list[str]:
    """LLM picks 1-2 most relevant gurus. Whitelist-validated.

    Returns [] when not applicable (non-A股短线场景) OR on routing failure
    (caller decides whether to fallback).
    """
    # Fixed mode bypasses LLM.
    if GURU_VIEW_MODE.startswith("fixed:"):
        spec = GURU_VIEW_MODE[len("fixed:"):].strip()
        return [g.strip() for g in spec.split(",")
                if g.strip() in _GURU_PROFILES][:GURU_VIEW_MAX]

    if not _GURU_PROFILES:
        return []

    profile_blocks = "\n\n".join(
        f"### {name} ({GURU_META[name][0]} · {GURU_META[name][1]})\n{prof}"
        for name, prof in _GURU_PROFILES.items()
    )
    system = (
        "你是 A 股短线游资视角分发器。下面是 10 位游资的简短画像。"
        f"读用户给的个股分析报告，从这 10 位里选 1-{GURU_VIEW_MAX} 位**最相关的互补**游资。\n\n"
        "硬规则：\n"
        f"- 选 1 位还是多位看报告内容：格局明确就 1 位即可，复杂(主线+龙头/首板+控回撤)再选 2 位，最多 {GURU_VIEW_MAX} 位\n"
        "- 选多位时必须是**不同派别**，互补视角，不要两个同派\n"
        "- 范围:**所有 A 股股票分析都在范围内** — 基本面/财报/技术面/价值/估值 都让相应风格的游资从他们的视角(主线归属 / 资金面 / 情绪节奏 / 龙头格局)给观点,即使报告本身是长线/基本面取向\n"
        "- 仅当报告**完全不涉及 A 股股票** (美股/港股/加密/期货/纯宏观/纯利率/纯汇率)，才返回空 selected: []\n"
        "- 只返回 JSON，不要其他文字：\n"
        '  {"selected": ["name1", "name2"], "reason": "为啥选他们 + 互补点"}\n'
        f"- name 严格只能是这 10 个之一：{', '.join(GURU_LIST)}\n\n"
        + profile_blocks
    )
    headline = summary.get("headline") or summary.get("title") or ""
    badge = summary.get("badge") or ""
    user_msg = (
        f"主结论: {badge} — {headline}\n\n"
        f"--- 报告片段(截前 4000 字) ---\n{(full_report or '')[:4000]}"
    )

    # max_tokens 3000: 路由输出 JSON 小但 v4-pro 推理本身吃 token
    parsed, _err = await _deepseek_json_call(
        system=system, user=user_msg, max_tokens=3000, temperature=0.3,
        label="guru/route", run_id=run_id,
    )
    if not parsed:
        return []
    selected_raw = parsed.get("selected") or []
    valid: list[str] = []
    for name in selected_raw:
        if isinstance(name, str) and name in _GURU_SKILLS and name not in valid:
            valid.append(name)
        if len(valid) >= GURU_VIEW_MAX:
            break
    print(f"[guru/route] run={run_id} selected={valid} "
          f"reason={parsed.get('reason','')[:120]}", flush=True)
    return valid


async def _generate_single_guru_view(guru: str, full_report: str, summary: dict,
                                      run_id: str) -> dict | None:
    """Generate one guru's view as a JSON object with takeaway + full_view.

    Returns {"takeaway": "30-60 字一句话", "full_view": "180-400 字完整锐评"}
    or None on failure / 不适用. Card uses takeaway, docx/Notion use full_view.
    """
    skill_text = _GURU_SKILLS.get(guru)
    if not skill_text:
        return None
    display_name, school = GURU_META.get(guru, (guru, "未知派别"))

    system_prompt = (
        skill_text.strip()
        + "\n\n----\n"
        + f"你现在是 A 股游资『{display_name}』本人(派别: {school})。"
        + "读下面这份个股分析报告，严格按你的判断框架给观点。\n\n"
        + "**必须返回严格 JSON,不要 markdown 围栏,不要解释:**\n"
        + '{\n'
        + '  "takeaway": "30-60 字一句话核心观点 — 直接说该买/该卖/该等 + 最关键的一个理由,口语化",\n'
        + '  "full_view": "180-400 字完整锐评(3-5 行短句),覆盖主线判断 / 个股定位 / 节奏阶段 / 操作建议 / 风险提示"\n'
        + '}\n\n'
        + "硬规则:\n"
        + "- 全中文,口语化游资风格 (直接、不绕)\n"
        + "- 操作建议要符合你这派的特色 (模式派→首板战法,控回撤派→4 点底线,进攻派→敢上重仓,等)\n"
        + "- 不复述报告原文,只给『你会怎么看』\n"
        + "- takeaway 是给一眼看的;full_view 才完整展开\n"
        + "- 不适用时 takeaway 和 full_view 都返回空字符串"
    )
    headline = summary.get("headline") or summary.get("title") or ""
    badge = summary.get("badge") or ""
    user_msg = (
        f"主报告结论: {badge} — {headline}\n\n"
        f"--- 完整报告(截前 8000 字) ---\n{(full_report or '')[:8000]}"
    )

    # max_tokens 3000: SKILL.md 在 system 里占大头,推理+输出双份
    parsed, _err = await _deepseek_json_call(
        system=system_prompt, user=user_msg, max_tokens=3000, temperature=0.4,
        label=f"guru/{guru}", run_id=run_id,
    )
    if not parsed:
        return None
    takeaway = (parsed.get("takeaway") or "").strip()
    full_view = (parsed.get("full_view") or "").strip()
    if not full_view and not takeaway:
        return None
    return {"takeaway": takeaway or full_view[:60],
            "full_view": full_view or takeaway}


async def _generate_youzi_views(full_report: str, summary: dict,
                                 preset: str, run_id: str,
                                 gurus_override: list[str] | None = None) -> list[dict]:
    """Pick 1-2 gurus via LLM router, generate each voice in parallel.

    When `gurus_override` is non-empty, skip routing and use those gurus
    (whitelist-validated). Returns list of {"guru","display_name","school","text"}.
    """
    if GURU_VIEW_MODE == "off":
        return []
    if PRESET_TEMPLATE.get(preset, "stock_decision") != "stock_decision":
        return []
    if not full_report or not _GURU_SKILLS:
        return []

    if gurus_override:
        selected = [g for g in gurus_override
                    if g in _GURU_SKILLS][:GURU_VIEW_MAX]
        print(f"[guru] run={run_id} using user override: {selected}", flush=True)
    else:
        selected = await _route_gurus(summary, full_report, run_id)
    if not selected:
        return []

    results = await asyncio.gather(
        *[_generate_single_guru_view(g, full_report, summary, run_id) for g in selected],
        return_exceptions=True,
    )
    views: list[dict] = []
    for guru, view in zip(selected, results):
        if isinstance(view, dict) and (view.get("full_view") or view.get("takeaway")):
            display, school = GURU_META.get(guru, (guru, "未知派别"))
            views.append({
                "guru": guru,
                "display_name": display,
                "school": school,
                "takeaway": view.get("takeaway") or "",
                # `text` 字段保持向后兼容,docx/Notion 渲染会读它当 full_view
                "text": view.get("full_view") or view.get("takeaway") or "",
            })
    print(f"[guru] run={run_id} produced {len(views)} views: "
          f"{[v['guru'] for v in views]}", flush=True)
    return views


async def _summarize_report(run) -> dict | None:
    """Use DeepSeek to extract a structured Chinese summary from a completed run.

    Retries up to 3 times — DeepSeek v4-pro is a reasoning model and the
    `content` field can be empty (all output went to `reasoning_content`)
    intermittently, causing a single attempt to fail JSON parsing. Caller
    falls back only if all 3 attempts fail.
    """
    full_report = (getattr(run, "final_report", None) or "").strip()
    # If aggregator-level final_report is short, also append last task's summary
    if len(full_report) < 500:
        tasks = getattr(run, "tasks", []) or []
        completed = [t for t in tasks if getattr(t.status, "value", "") == "completed" and t.summary]
        if completed:
            full_report += "\n\n" + completed[-1].summary

    if not full_report:
        return None

    preset_name = getattr(run, "preset_name", "investment_committee")
    template = PRESET_TEMPLATE.get(preset_name, "stock_decision")
    system_prompt = _build_summarizer_prompt(template)
    run_id = getattr(run, "id", "")

    user_msg = (
        f"preset: {preset_name}\n"
        f"user_vars: {json.dumps(getattr(run, 'user_vars', {}) or {}, ensure_ascii=False)}\n"
        f"tokens: in={getattr(run, 'total_input_tokens', 0)} "
        f"out={getattr(run, 'total_output_tokens', 0)}\n\n"
        f"--- 原始报告 ---\n{full_report[:12000]}"
    )

    # 3 次重试 — DeepSeek-v4-pro reasoning model 偶发 content 真空 / 截断,
    # 升温重试可以打破确定性的坏模式。每次复用 _deepseek_json_call 的健壮性。
    last_err = ""
    for attempt in range(1, 4):
        parsed, err = await _deepseek_json_call(
            system=system_prompt, user=user_msg, max_tokens=6000,
            temperature=0.1 + 0.1 * (attempt - 1),
            label=f"summarizer/{attempt}", run_id=run_id,
        )
        if parsed:
            parsed.setdefault("template", template)
            return parsed
        last_err = err
    print(f"[summarizer] all 3 attempts failed (last: {last_err}) run={run_id}",
          flush=True)
    return None


# ─────────── Feishu Interactive Card builder ───────────

_BADGE_COLOR_MAP = {
    "green": "green", "red": "red", "blue": "blue", "orange": "orange",
    "grey": "grey", "gray": "grey", "turquoise": "turquoise",
}
# Default color for each badge value across all 3 templates.
_DECISION_DEFAULT_COLOR = {
    # stock_decision
    "买入": "green", "条件性多头": "green", "关注": "green",
    "卖出": "red", "条件性空头": "red",
    "持有": "blue", "中性": "blue", "回避": "grey",
    # macro_theme
    "看多": "green", "超配": "green",
    "看空": "red", "低配": "red",
    # research_alloc
    "推荐": "green", "不建议": "red", "谨慎": "red",
    "待验证": "blue", "待优化": "orange",
}


def _bullet_block(title: str, items: list[str], emoji: str = "") -> dict:
    """Build a Feishu card div element with a title + bullet list."""
    if not items:
        body = "_(未提及)_"
    else:
        body = "\n".join(f"• {x}" for x in items[:6])
    return {
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": f"**{emoji}{title}**\n{body}"},
    }


def _kv_block(title: str, kv: dict[str, str]) -> dict:
    if not kv:
        return {"tag": "div", "text": {"tag": "lark_md",
                                       "content": f"**{title}**\n_(未提及)_"}}
    lines = [f"• {k}: **{v}**" for k, v in kv.items() if v]
    body = "\n".join(lines[:8]) or "_(未提及)_"
    return {"tag": "div", "text": {"tag": "lark_md",
                                   "content": f"**{title}**\n{body}"}}


def _build_feishu_card(summary: dict, run_id: str,
                       notion_url: str | None = None,
                       feishu_doc_url: str | None = None) -> dict:
    """Render a concise interactive card. Details (full tldr / sections /
    key_metrics / catalysts / full guru views) live in the docx and Notion.

    Card content:
      - header (title + decision badge)
      - 📌 headline (one line)
      - 2-4 KV fields (decision / target / horizon / risk)
      - short_tldr (80-120 字精炼综述)
      - 🐊 每位游资一行 takeaway (30-60 字)
      - action buttons (docx + Notion) + run_id note
    """
    title = summary.get("title") or "swarm 分析报告"
    badge = summary.get("badge") or "中性"
    color = (_BADGE_COLOR_MAP.get(summary.get("badge_color") or "")
             or _DECISION_DEFAULT_COLOR.get(badge, "blue"))

    # KV fields — 紧凑展示决策维度。
    top_fields: list[dict] = []
    for kv in (summary.get("kv_fields") or []):
        if not isinstance(kv, dict):
            continue
        label = str(kv.get("label", "")).strip()
        value = str(kv.get("value", "")).strip()
        if not label or not value or value == "(未提及)":
            continue
        top_fields.append({
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"},
        })

    elements: list[dict] = []
    headline = summary.get("headline") or summary.get("decision_summary")
    if headline:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📌 **{headline}**"},
        })
    if top_fields:
        elements.append({"tag": "div", "fields": top_fields[:4]})

    # 精简综述 (短版,完整版在 docx)
    short_tldr = (summary.get("short_tldr") or "").strip()
    if not short_tldr:
        # 回退:若 summarizer 还没出 short_tldr 字段(老 run),把 tldr 截前 140 字
        full = (summary.get("tldr") or "").strip()
        if full:
            short_tldr = full[:140] + ("…" if len(full) > 140 else "")
    if short_tldr:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**📝 综述**\n{short_tldr}"},
        })

    # 游资速看 — 每位一行 takeaway (full_view 留给 docx/Notion)
    views = summary.get("youzi_views") or []
    if views:
        elements.append({"tag": "hr"})
        lines = ["**🐊 游资速看**"]
        for v in views:
            if not isinstance(v, dict):
                continue
            name = v.get("display_name", "游资")
            school = v.get("school", "")
            takeaway = (v.get("takeaway") or "").strip()
            if not takeaway:
                # 老数据没 takeaway,从 text 截 60 字
                takeaway = (v.get("text") or "").strip()[:60]
            if not takeaway:
                continue
            lines.append(f"• **{name}**({school}): {takeaway}")
        if len(lines) > 1:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            })
    else:
        # 用户明确指定了游资但 voice 自判不适用 — 显式告知,别让用户以为系统挂了。
        skipped = summary.get("youzi_skipped_override") or []
        if skipped:
            elements.append({"tag": "hr"})
            names = " / ".join(
                f"{s.get('display_name', '')}({s.get('school', '')})"
                for s in skipped if isinstance(s, dict))
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": (f"**🐊 你指定的游资:{names}**\n"
                                     "判断本标的不在他/她的能力圈/方法论适用范围,"
                                     "本次无观点。换个游资再试(例:『用 92 科比看 ...』)"
                                     "或不指定游资让系统自动选最相关的。")},
            })

    # Footer: full-report links (Feishu doc + Notion) + run id
    actions = []
    if feishu_doc_url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📄 飞书文档"},
            "url": feishu_doc_url,
            "type": "primary",
        })
    if notion_url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🗂 Notion 备份"},
            "url": notion_url,
            "type": "default",
        })
    if actions:
        elements.append({"tag": "action", "actions": actions})

    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": f"run_id: {run_id}",
        }],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title[:120]},
            "template": color,
        },
        "elements": elements,
    }


def _feishu_send_card(receive_id: str, receive_id_type: str, card: dict) -> dict:
    """Send an interactive card to a Feishu chat."""
    token = _feishu_get_tenant_token()
    body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    with httpx.Client(timeout=20) as c:
        r = c.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        d = r.json()
        if d.get("code") != 0:
            print(f"[feishu] send card failed: {d}", file=sys.stderr, flush=True)
        return d


# ─────────── Feishu Docx (云文档) sync ───────────

def _feishu_create_docx(title: str) -> tuple[str | None, str | None]:
    """Create an empty docx. If FEISHU_DRIVE_FOLDER_TOKEN is set the docx
    lands in that (user-owned) folder so it inherits the folder's share
    settings — bypassing the need for drive:drive scope on the bot."""
    try:
        token = _feishu_get_tenant_token()
        body: dict = {"title": title[:200]}
        if FEISHU_DRIVE_FOLDER_TOKEN:
            body["folder_token"] = FEISHU_DRIVE_FOLDER_TOKEN
        with httpx.Client(timeout=15) as c:
            r = c.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=body,
            )
            d = r.json()
        if d.get("code") != 0:
            print(f"[feishu/docx] create failed: {d}", flush=True)
            return None, None
        doc = d.get("data", {}).get("document") or {}
        doc_id = doc.get("document_id")
        if not doc_id:
            return None, None
        url = f"https://feishu.cn/docx/{doc_id}"
        return doc_id, url
    except Exception as e:
        print(f"[feishu/docx] create exception: {type(e).__name__}: {e}", flush=True)
        return None, None


# ── inline markdown → Feishu text_run elements ──
_INLINE_RE = re.compile(
    r'\*\*(?P<bold>[^*\n]+?)\*\*'
    r'|__(?P<bold2>[^_\n]+?)__'
    r'|(?<![\w*])\*(?P<italic>[^*\n]+?)\*(?!\w)'
    r'|`(?P<code>[^`\n]+?)`'
)


def _parse_inline_md(text: str) -> list[dict]:
    """Parse inline markdown into Feishu text_run elements with styles
    (bold / italic / inline_code). Plain segments use empty style."""
    text = text or ""
    out: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                out.append({"text_run": {"content": plain, "text_element_style": {}}})
        style = {}
        content = ""
        if m.group("bold") is not None or m.group("bold2") is not None:
            content = m.group("bold") or m.group("bold2")
            style = {"bold": True}
        elif m.group("italic") is not None:
            content = m.group("italic")
            style = {"italic": True}
        elif m.group("code") is not None:
            content = m.group("code")
            style = {"inline_code": True}
        out.append({"text_run": {"content": content, "text_element_style": style}})
        pos = m.end()
    if pos < len(text):
        tail = text[pos:]
        if tail:
            out.append({"text_run": {"content": tail, "text_element_style": {}}})
    if not out:
        out.append({"text_run": {"content": text, "text_element_style": {}}})
    # Cap each run at 1900 chars (Feishu API limit per run)
    capped: list[dict] = []
    for el in out:
        content = el["text_run"]["content"]
        if len(content) <= 1900:
            capped.append(el)
        else:
            style = el["text_run"]["text_element_style"]
            for i in range(0, len(content), 1900):
                capped.append({"text_run": {"content": content[i:i + 1900],
                                            "text_element_style": dict(style)}})
    return capped


def _feishu_text_block(content_or_elements, btype: str = "text") -> dict:
    """Build a Feishu docx block. `content_or_elements` is either a str
    (will be parsed for inline markdown) or a pre-built list of text_run dicts."""
    if isinstance(content_or_elements, list):
        elements = content_or_elements
    else:
        elements = _parse_inline_md(content_or_elements)
    # Feishu block_type ints: text=2, heading1=3, heading2=4, heading3=5,
    # bullet=12, ordered=13, code=14, quote=15, todo=17, callout=19, divider=22
    if btype == "heading1":
        return {"block_type": 3, "heading1": {"elements": elements, "style": {}}}
    if btype == "heading2":
        return {"block_type": 4, "heading2": {"elements": elements, "style": {}}}
    if btype == "heading3":
        return {"block_type": 5, "heading3": {"elements": elements, "style": {}}}
    if btype == "bullet":
        return {"block_type": 12, "bullet": {"elements": elements, "style": {}}}
    if btype == "ordered":
        return {"block_type": 13, "ordered": {"elements": elements, "style": {}}}
    if btype == "quote":
        return {"block_type": 15, "quote": {"elements": elements, "style": {}}}
    return {"block_type": 2, "text": {"elements": elements, "style": {}}}


def _flush_table(table_lines: list[str], blocks: list[dict], max_blocks: int) -> None:
    """Convert a markdown table (with leading `|`) into a Feishu heading +
    bullet list. Bullets read 'col1: val1 · col2: val2 · ...' for readability."""
    if not table_lines:
        return
    # Parse rows
    rows: list[list[str]] = []
    for line in table_lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Skip separator rows (e.g., "---|---")
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return
    header = rows[0]
    for row in rows[1:]:
        if len(blocks) >= max_blocks:
            return
        if not any(row):
            continue
        # Build "col: val · col: val" form
        parts: list[str] = []
        for i, val in enumerate(row):
            if not val:
                continue
            col = header[i] if i < len(header) else ""
            if col:
                parts.append(f"**{col}**: {val}")
            else:
                parts.append(val)
        if parts:
            blocks.append(_feishu_text_block(" · ".join(parts), "bullet"))


def _md_to_feishu_blocks(md: str, max_blocks: int = 80) -> list[dict]:
    """Convert markdown into Feishu docx blocks with inline-style preservation.

    Supports:
      - Headings ## ### → heading1/2/3
      - Bullet lines `- / * / +` → bullet (with inline **bold**/`code` parsed)
      - Ordered lists `1.` `2.` → ordered
      - Blockquote `>` → quote
      - Divider `---` → divider
      - Tables `| ... | ... |` → rendered as bullet rows with bold column names
      - Code fence ``` ``` ``` → collapsed to single quote block (Feishu doesn't have plain code block in v1)
      - Paragraphs → text block, with inline markdown parsed
    """
    blocks: list[dict] = []
    para_buf: list[str] = []
    table_buf: list[str] = []
    in_code_fence = False
    code_buf: list[str] = []

    def flush_para():
        nonlocal para_buf
        if not para_buf:
            return
        text = "\n".join(para_buf).strip()
        para_buf = []
        if not text:
            return
        # Within paragraph, single newline → space (cleaner reading)
        text = re.sub(r"\s*\n\s*", " ", text)
        blocks.append(_feishu_text_block(text, "text"))

    def flush_table():
        nonlocal table_buf
        if table_buf:
            _flush_table(table_buf, blocks, max_blocks)
            table_buf = []

    def flush_code():
        nonlocal code_buf, in_code_fence
        if code_buf:
            content = "\n".join(code_buf)
            # Render code as quote (Feishu's plain code block needs language metadata)
            blocks.append(_feishu_text_block(content, "quote"))
            code_buf = []
        in_code_fence = False

    for raw_line in (md or "").splitlines():
        if len(blocks) >= max_blocks:
            break
        line = raw_line.rstrip()

        # Code fence handling
        if line.strip().startswith("```"):
            if in_code_fence:
                flush_code()
            else:
                flush_para(); flush_table()
                in_code_fence = True
            continue
        if in_code_fence:
            code_buf.append(line)
            continue

        # Table accumulation
        if line.startswith("|"):
            flush_para()
            table_buf.append(line)
            continue
        elif table_buf:
            flush_table()

        if not line.strip():
            flush_para()
            continue
        if line.strip() == "---" or re.fullmatch(r"-{3,}|_{3,}|\*{3,}", line.strip() or ""):
            flush_para()
            blocks.append({"block_type": 22, "divider": {}})
            continue
        # Heading
        m = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if m:
            flush_para()
            level = len(m.group(1))
            kind = ["heading1", "heading2", "heading3"][level - 1]
            blocks.append(_feishu_text_block(m.group(2), kind))
            continue
        # Blockquote
        if line.startswith(">"):
            flush_para()
            blocks.append(_feishu_text_block(line.lstrip("> ").rstrip(), "quote"))
            continue
        # Ordered list "1." "2."
        m = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if m:
            flush_para()
            blocks.append(_feishu_text_block(m.group(1), "ordered"))
            continue
        # Bullet
        m = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if m:
            flush_para()
            blocks.append(_feishu_text_block(m.group(1), "bullet"))
            continue
        para_buf.append(line)

    flush_code()
    flush_table()
    flush_para()
    return blocks[:max_blocks]


def _feishu_insert_blocks(doc_id: str, blocks: list[dict]) -> bool:
    """Insert blocks under the doc root, in chunks of 50 (Feishu API limit)."""
    if not blocks:
        return True
    token = _feishu_get_tenant_token()
    idx = 0
    for offset in range(0, len(blocks), 50):
        chunk = blocks[offset:offset + 50]
        try:
            with httpx.Client(timeout=30) as c:
                r = c.post(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/"
                    f"{doc_id}/blocks/{doc_id}/children",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json={"children": chunk, "index": idx},
                )
                d = r.json()
            if d.get("code") != 0:
                print(f"[feishu/docx] insert blocks failed at offset {offset}: {d}", flush=True)
                return False
        except Exception as e:
            print(f"[feishu/docx] insert exception offset {offset}: {e}", flush=True)
            return False
        idx += len(chunk)
    return True


def _feishu_set_doc_link_share(doc_id: str, entity: str = "tenant_readable") -> bool:
    """Set link-share permission on a docx so anyone in the org with the link
    can read it without applying for permission.

    Requires `drive:drive` (or `docs:doc`) app scope. Falls back silently if
    the app version hasn't been published with the required scope active.

    `entity` values:
      - `tenant_readable` 组织内可阅读 (推荐 — 群成员直接看)
      - `tenant_editable` 组织内可编辑
      - `anyone_readable` 公网可阅读 (慎用,内容会被搜索引擎收录)
      - `closed`          关闭分享 (默认 Feishu 行为)
    """
    if not doc_id or entity == "closed":
        return False
    try:
        token = _feishu_get_tenant_token()
        with httpx.Client(timeout=15) as c:
            r = c.patch(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public"
                f"?type=docx",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"link_share_entity": entity},
            )
            d = r.json()
        if d.get("code") != 0:
            print(f"[feishu/docx] set link-share=({entity}) failed: "
                  f"{d.get('code')} {d.get('msg','')[:120]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[feishu/docx] set link-share exception: "
              f"{type(e).__name__}: {e}", flush=True)
        return False


def _feishu_share_doc_with_user(doc_id: str, open_id: str, perm: str = "full_access") -> bool:
    """Add a user as a member on a docx. Requires `drive:drive` (or
    `docs:permission.member:create`) app permission. Failure is non-fatal —
    user can still view via the link if link-share is enabled separately."""
    if not (doc_id and open_id):
        return False
    try:
        token = _feishu_get_tenant_token()
        with httpx.Client(timeout=15) as c:
            r = c.post(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/members"
                f"?type=docx&need_notification=false",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"member_type": "openid", "member_id": open_id, "perm": perm},
            )
            d = r.json()
        if d.get("code") != 0:
            print(f"[feishu/docx] share with {open_id} failed: {d.get('code')} {d.get('msg','')[:120]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[feishu/docx] share exception: {type(e).__name__}: {e}", flush=True)
        return False


def _feishu_create_doc_from_report(summary: dict, full_report: str,
                                    run_id: str, preset: str,
                                    share_with_open_id: str | None = None) -> str | None:
    """Create a Feishu docx with structured header + full report. Returns the URL.

    If `share_with_open_id` is provided, the doc is shared (full_access) with
    that user so they can read it without applying for permission.
    """
    title = summary.get("title") or f"swarm {run_id}"
    doc_title = f"{title}  {time.strftime('%Y-%m-%d')}"

    doc_id, url = _feishu_create_docx(doc_title)
    if not doc_id:
        return None

    # Build content blocks driven by the uniform schema:
    # metadata header (kv_fields) + headline + tldr + sections + metrics + actions.
    blocks: list[dict] = []
    badge = summary.get("badge") or "未分类"

    # Metadata quote: collect all populated kv_fields + badge + run_id
    meta_lines: list[str] = []
    for kv in (summary.get("kv_fields") or []):
        if not isinstance(kv, dict):
            continue
        label = str(kv.get("label", "")).strip()
        value = str(kv.get("value", "")).strip()
        if not label or not value or value == "(未提及)":
            continue
        meta_lines.append(f"{label}: {value}")
    if not meta_lines:
        meta_lines.append(f"结论: {badge}")
    meta_lines.append(f"run_id: {run_id}")
    blocks.append(_feishu_text_block("\n".join(meta_lines), "quote"))

    headline = summary.get("headline") or summary.get("decision_summary")
    if headline:
        blocks.append(_feishu_text_block(headline, "heading2"))
    if summary.get("tldr"):
        blocks.append(_feishu_text_block("综述", "heading2"))
        blocks.append(_feishu_text_block(summary["tldr"], "text"))

    # Bullet sections (template-agnostic: takes labels from summary["sections"])
    for sec in (summary.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        label = str(sec.get("label", "")).strip() or "·"
        items = sec.get("items") or []
        if not items:
            continue
        blocks.append(_feishu_text_block(label, "heading2"))
        for item in items[:8]:
            blocks.append(_feishu_text_block(item, "bullet"))

    metrics = summary.get("key_metrics") or {}
    if isinstance(metrics, dict) and metrics:
        blocks.append(_feishu_text_block("📊 关键指标", "heading2"))
        for k, v in list(metrics.items())[:12]:
            blocks.append(_feishu_text_block(f"**{k}**: {v}", "bullet"))

    aoc = summary.get("actions_or_catalysts") or {}
    if isinstance(aoc, dict) and aoc.get("items"):
        blocks.append(_feishu_text_block(aoc.get("label", "📋 后续"), "heading2"))
        for item in (aoc.get("items") or [])[:8]:
            blocks.append(_feishu_text_block(item, "bullet"))

    # 游资观点 (multi-guru) — between main summary and raw report.
    views = summary.get("youzi_views") or []
    if views:
        blocks.append({"block_type": 22, "divider": {}})
        blocks.append(_feishu_text_block(
            f"🐊 游资观点 · LLM 自选 {len(views)} 位", "heading2"))
        for v in views:
            if not isinstance(v, dict) or not v.get("text"):
                continue
            title = f"{v.get('display_name','游资')} · {v.get('school','')}"
            blocks.append(_feishu_text_block(title, "heading3"))
            for line in v["text"].split("\n"):
                if line.strip():
                    blocks.append(_feishu_text_block(line.strip(), "text"))
    else:
        skipped = summary.get("youzi_skipped_override") or []
        if skipped:
            names = " / ".join(
                f"{s.get('display_name', '')}({s.get('school', '')})"
                for s in skipped if isinstance(s, dict))
            blocks.append({"block_type": 22, "divider": {}})
            blocks.append(_feishu_text_block(
                f"🐊 你指定的游资:{names}", "heading2"))
            blocks.append(_feishu_text_block(
                "判断本标的不在他/她的能力圈/方法论适用范围,本次无观点。"
                "换个游资再试,或不指定让系统自动选最相关的。", "text"))

    blocks.append({"block_type": 22, "divider": {}})
    blocks.append(_feishu_text_block("完整原始报告", "heading2"))
    blocks.extend(_md_to_feishu_blocks(full_report, max_blocks=60))
    blocks = blocks[:99]

    ok = _feishu_insert_blocks(doc_id, blocks)
    if ok:
        # Default: anyone in the org with the link can read (so group members
        # don't need to apply for permission). Falls back silently if the
        # required scope isn't activated.
        if FEISHU_DOC_SHARE_ENTITY != "closed":
            _feishu_set_doc_link_share(doc_id, entity=FEISHU_DOC_SHARE_ENTITY)
        if share_with_open_id:
            _feishu_share_doc_with_user(doc_id, share_with_open_id, perm="full_access")
    return url if ok else None


# ─────────── Notion sync ───────────

def _notion_markdown_to_blocks(md: str, max_blocks: int = 90) -> list[dict]:
    """Convert markdown text into Notion paragraph/heading blocks.

    Keeps it pragmatic: detects # headings, --- dividers, and groups everything
    else into paragraph blocks (one per non-empty line cluster). Notion has a
    100-block limit per request, so we cap.
    """
    blocks: list[dict] = []
    lines = (md or "").splitlines()
    para_buf: list[str] = []

    def flush_para():
        nonlocal para_buf
        if not para_buf:
            return
        text = "\n".join(para_buf).strip()
        para_buf = []
        if not text:
            return
        # Notion rich_text limit per block is 2000 chars
        for chunk in [text[i:i + 1900] for i in range(0, len(text), 1900)]:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })
            if len(blocks) >= max_blocks:
                return

    for line in lines:
        if len(blocks) >= max_blocks:
            break
        s = line.rstrip()
        if not s:
            flush_para()
            continue
        if s.strip() == "---":
            flush_para()
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        m = re.match(r"^(#{1,3})\s+(.+)$", s)
        if m:
            flush_para()
            level = len(m.group(1))
            heading_type = f"heading_{min(level, 3)}"
            blocks.append({
                "object": "block", "type": heading_type,
                heading_type: {"rich_text": [{"type": "text", "text": {"content": m.group(2)[:1900]}}]},
            })
            continue
        para_buf.append(s)
    flush_para()
    return blocks[:max_blocks]


async def _notion_create_page(summary: dict, full_report: str, run_id: str,
                              preset: str) -> str | None:
    """Create a page in the configured Notion database. Returns the page URL or None."""
    if not NOTION_ENABLED:
        return None

    title = (summary.get("title") or f"swarm {run_id}")[:200]
    decision = summary.get("badge") or summary.get("decision_badge") or "未分类"
    target = ""
    # Try to extract ticker from title "Name (TICKER) — preset" or fallback to user_vars
    m = re.search(r"\(([^)]+)\)", title)
    if m:
        target = m.group(1)

    use_database = bool(NOTION_DATABASE_ID)
    # Properties depend on parent type:
    #   - database parent: structured columns (Title + optional Ticker/Decision/Date/Preset)
    #   - page parent: only "title" is allowed
    if use_database:
        properties = {
            "Title": {"title": [{"text": {"content": title}}]},
        }
        optional_props = {
            "Ticker": {"rich_text": [{"text": {"content": target[:200]}}]} if target else None,
            "Decision": {"select": {"name": decision}},
            "Preset": {"select": {"name": preset}},
            "Date": {"date": {"start": time.strftime("%Y-%m-%d")}},
            "Status": {"select": {"name": "Completed"}},
            "Run ID": {"rich_text": [{"text": {"content": run_id}}]},
        }
    else:
        # Pages under a parent page accept only `title` property (key must literally be "title")
        properties = {
            "title": {"title": [{"text": {"content": title}}]},
        }
        optional_props = {}

    # Build body blocks from the uniform schema (kv_fields / headline / tldr /
    # sections / metrics / actions). Template-agnostic.
    body_blocks: list[dict] = []
    badge = summary.get("badge") or "未分类"

    # Metadata callout (always show — kv_fields + preset + run_id)
    meta_lines = []
    if target:
        meta_lines.append(f"📌 Ticker: {target}")
    meta_lines.append(f"⚖️ 结论: {badge}")
    for kv in (summary.get("kv_fields") or []):
        if not isinstance(kv, dict):
            continue
        label = str(kv.get("label", "")).strip()
        value = str(kv.get("value", "")).strip()
        if label and value and value != "(未提及)" and label != "决策" and label != "立场" and label != "结论":
            meta_lines.append(f"{label}: {value}")
    meta_lines.append(f"🧪 Preset: {preset}")
    meta_lines.append(f"📅 Date: {time.strftime('%Y-%m-%d')}")
    meta_lines.append(f"🔖 Run ID: {run_id}")
    body_blocks.append({
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": "\n".join(meta_lines)}}],
            "icon": {"emoji": "🗂️"},
        },
    })

    headline = summary.get("headline") or summary.get("decision_summary")
    if headline:
        body_blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": headline}}],
                "icon": {"emoji": "📌"},
            },
        })
    if summary.get("tldr"):
        body_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "综述"}}]},
        })
        body_blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text",
                                         "text": {"content": summary["tldr"][:1900]}}]},
        })

    def _bullets_block(heading: str, items: list[str]):
        if not items:
            return []
        out = [{
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": heading}}]},
        }]
        for it in items[:8]:
            out.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text",
                                                       "text": {"content": (it or "")[:1900]}}]},
            })
        return out

    # Template-agnostic bullet sections (each section has its own label per template)
    for sec in (summary.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        label = str(sec.get("label", "")).strip() or "·"
        body_blocks.extend(_bullets_block(label, sec.get("items") or []))

    aoc = summary.get("actions_or_catalysts") or {}
    if isinstance(aoc, dict) and aoc.get("items"):
        body_blocks.extend(_bullets_block(aoc.get("label", "📋 后续"),
                                           aoc.get("items") or []))

    metrics = summary.get("key_metrics") or {}
    if isinstance(metrics, dict) and metrics:
        body_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📊 关键指标"}}]},
        })
        for k, v in list(metrics.items())[:12]:
            body_blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text",
                                                       "text": {"content": f"{k}: {v}"}}]},
            })

    # 游资观点 (multi-guru) — between main summary and raw report.
    views = summary.get("youzi_views") or []
    if views:
        body_blocks.append({"object": "block", "type": "divider", "divider": {}})
        body_blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text",
                                          "text": {"content": f"🐊 游资观点 · LLM 自选 {len(views)} 位"}}]},
        })
        for v in views:
            if not isinstance(v, dict) or not v.get("text"):
                continue
            title = f"{v.get('display_name','游资')} · {v.get('school','')}"
            body_blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text",
                                              "text": {"content": title}}]},
            })
            for line in v["text"].split("\n"):
                line = line.strip()
                if not line:
                    continue
                body_blocks.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text",
                                                  "text": {"content": line[:1900]}}]},
                })
    else:
        skipped = summary.get("youzi_skipped_override") or []
        if skipped:
            names = " / ".join(
                f"{s.get('display_name', '')}({s.get('school', '')})"
                for s in skipped if isinstance(s, dict))
            body_blocks.append({"object": "block", "type": "divider", "divider": {}})
            body_blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text",
                                              "text": {"content": f"🐊 你指定的游资:{names}"}}]},
            })
            body_blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text",
                                              "text": {"content": "判断本标的不在他/她的能力圈/方法论适用范围,本次无观点。换个游资再试,或不指定让系统自动选最相关的。"}}]},
            })

    body_blocks.append({"object": "block", "type": "divider", "divider": {}})
    body_blocks.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "完整原始报告"}}]},
    })
    body_blocks.extend(_notion_markdown_to_blocks(full_report, max_blocks=90))
    body_blocks = body_blocks[:99]  # Notion 100-block per-request cap

    parent = ({"database_id": NOTION_DATABASE_ID} if use_database
              else {"page_id": NOTION_PARENT_PAGE_ID})

    async def _post_with_props(props):
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=15, pool=5)) as c:
            return await c.post(
                "https://api.notion.com/v1/pages",
                headers={
                    "Authorization": f"Bearer {NOTION_API_KEY}",
                    "Notion-Version": NOTION_API_VERSION,
                    "Content-Type": "application/json",
                },
                json={
                    "parent": parent,
                    "properties": props,
                    "children": body_blocks,
                },
            )

    # First attempt with all optional props
    props = dict(properties)
    for k, v in optional_props.items():
        if v is not None:
            props[k] = v
    try:
        r = await _post_with_props(props)
        if r.status_code == 200:
            return r.json().get("url")
        # If property mismatch, retry with only Title
        err_text = r.text
        print(f"[notion] first attempt failed {r.status_code}: {err_text[:300]}", flush=True)
        if "property" in err_text.lower() or r.status_code == 400:
            r2 = await _post_with_props(properties)  # only Title
            if r2.status_code == 200:
                return r2.json().get("url")
            print(f"[notion] retry failed {r2.status_code}: {r2.text[:300]}", flush=True)
        return None
    except Exception as e:
        print(f"[notion] exception: {type(e).__name__}: {e}", flush=True)
        return None


def _feishu_format_report(run) -> str:
    """Build a markdown-ish text summary of a completed swarm run."""
    status = run.status.value
    if status != "completed":
        return f"❌ swarm 终态: {status}\nrun_id: {run.id}\n" + \
               (run.final_report or "(no final_report)")
    lines = []
    fr = (run.final_report or "").strip()
    if fr:
        lines.append(fr)
    # Per-agent summaries below the final report
    tasks = getattr(run, "tasks", []) or []
    completed_tasks = [t for t in tasks if t.status.value == "completed" and t.summary]
    if completed_tasks and not fr:
        lines.append("(no aggregated final_report — per-agent summaries:)\n")
        for t in completed_tasks:
            sm = (t.summary or "").strip()
            if sm:
                lines.append(f"### {t.agent_id}\n\n{sm[:3000]}")
    footer = f"\n\n---\nrun_id: {run.id}  tokens: in={run.total_input_tokens} out={run.total_output_tokens}"
    return "\n".join(lines) + footer


async def _publish_terminal_run(run, info: dict) -> None:
    """When a run reaches terminal state: summarize via DeepSeek, push Feishu
    interactive card, sync to Notion. Each step is best-effort and isolated —
    a failure in one doesn't prevent the others."""
    chat_id = info["receive_id"]
    chat_type = info.get("receive_id_type", "chat_id")
    run_id = run.id
    status = run.status.value
    full_report = (getattr(run, "final_report", None) or "").strip()
    preset = getattr(run, "preset_name", "investment_committee")

    # Non-completed terminal states: short text, no raw report dump.
    if status != "completed" or not full_report:
        try:
            _feishu_send_text(
                chat_id, chat_type,
                f"❌ swarm 终态: {status}\nrun_id: {run_id}\n"
                f"原始报告留在服务端,可用 `status {run_id}` 重试查询。"
            )
        except Exception as e:
            print(f"[publish] send failure-text err {run_id}: {e}", flush=True)
        return

    # 1. Structured summary via DeepSeek
    summary: dict | None = None
    try:
        summary = await _summarize_report(run)
    except Exception as e:
        print(f"[publish] summarize exception {run_id}: {e}", flush=True)
    if not summary:
        # No raw-markdown fallback into chat. Surface a short failure note.
        try:
            _feishu_send_text(
                chat_id, chat_type,
                f"⚠️ swarm 已完成但摘要生成失败,run_id: {run_id}\n"
                f"原始报告已落到服务端 disk,稍后可重新摘要。",
            )
        except Exception as e:
            print(f"[publish] summary-fail send err {run_id}: {e}", flush=True)
        return

    # 1b. 游资观点 (multi-guru) — 用户指定 gurus_override > LLM 路由,仅 stock_decision preset.
    try:
        gurus_override = info.get("gurus_override") or []
        views = await _generate_youzi_views(full_report, summary, preset, run_id,
                                             gurus_override=gurus_override)
        if views:
            summary["youzi_views"] = views
            print(f"[publish] guru views ok {run_id}: "
                  f"{[v['guru'] for v in views]} "
                  f"(override={bool(gurus_override)})", flush=True)
        else:
            # 显式可见 — 之前是 silent skip,bug 时不知道这一环挂了。
            # 三种合法 0 views 场景:1) preset 非 stock_decision 2) 报告非 A 股
            #                    3) router/voice LLM 出问题 (上游 helper 已 log)
            print(f"[publish] guru views EMPTY {run_id} "
                  f"(preset={preset}, override={bool(gurus_override)}) — "
                  f"卡片将不带 游资速看 段", flush=True)
            # 如果用户明确指定了游资但 voice LLM 判定不适用,在卡片/docx/Notion
            # 显式告知 — 不让用户以为系统挂了。
            if gurus_override:
                safe = [g for g in gurus_override if g in _GURU_SKILLS][:GURU_VIEW_MAX]
                summary["youzi_skipped_override"] = [
                    {"guru": g,
                     "display_name": GURU_META.get(g, (g, ""))[0],
                     "school": GURU_META.get(g, ("", ""))[1]}
                    for g in safe
                ]
    except Exception as e:
        print(f"[publish] guru exception {run_id}: {e}", flush=True)

    # 2. Notion sync (independent of Feishu success)
    notion_url: str | None = None
    if NOTION_ENABLED:
        try:
            notion_url = await _notion_create_page(summary, full_report, run_id, preset)
        except Exception as e:
            print(f"[publish] notion exception {run_id}: {e}", flush=True)
        if notion_url:
            print(f"[publish] notion ok {run_id} → {notion_url}", flush=True)

    # 3. Feishu Docx with full report + auto-share with the user who triggered.
    feishu_doc_url: str | None = None
    share_with = info.get("sender_open_id") or ""
    try:
        feishu_doc_url = await asyncio.to_thread(
            _feishu_create_doc_from_report, summary, full_report, run_id, preset,
            share_with,
        )
    except Exception as e:
        print(f"[publish] feishu docx exception {run_id}: {e}", flush=True)
    if feishu_doc_url:
        print(f"[publish] feishu docx ok {run_id} → {feishu_doc_url}", flush=True)

    # 4. Feishu interactive card with both links. If card send fails, send a
    #    short text pointing to the off-chat surfaces (doc + Notion) — no raw
    #    markdown dump in chat.
    if info.get("skip_feishu_card"):
        print(f"[publish] feishu card skipped {run_id} (skip_feishu_card)",
              flush=True)
        return
    try:
        card = _build_feishu_card(summary, run_id, notion_url=notion_url,
                                  feishu_doc_url=feishu_doc_url)
        _feishu_send_card(chat_id, chat_type, card)
        print(f"[publish] feishu card ok {run_id}", flush=True)
    except Exception as e:
        print(f"[publish] feishu card err {run_id}: {e}", flush=True)
        # Don't dump raw markdown. Send a short text with the off-chat links.
        try:
            lines = [f"⚠️ swarm 完成但卡片渲染失败,run_id: {run_id}"]
            if feishu_doc_url:
                lines.append(f"📄 飞书文档: {feishu_doc_url}")
            if notion_url:
                lines.append(f"🗂 Notion: {notion_url}")
            _feishu_send_text(chat_id, chat_type, "\n".join(lines))
        except Exception as e2:
            print(f"[publish] card-fail send err {run_id}: {e2}", flush=True)


def _feishu_poll_loop():
    """Background poller. Runs an asyncio loop in this thread so it can await
    the publish coroutine (which uses async httpx for DeepSeek + Notion)."""
    from src.swarm.models import RunStatus
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print("[feishu] poll loop started", flush=True)
    try:
        while True:
            try:
                with _feishu_pending_lock:
                    snapshot = dict(_feishu_pending)
                if not snapshot:
                    time.sleep(15)
                    continue
                for run_id, info in snapshot.items():
                    try:
                        run = store.load_run(run_id)
                    except Exception as e:
                        print(f"[feishu] load_run({run_id}) err: {e}", flush=True)
                        continue
                    if run is None:
                        continue
                    if run.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled):
                        try:
                            loop.run_until_complete(_publish_terminal_run(run, info))
                        except Exception as e:
                            print(f"[feishu] publish err {run_id}: {e}", flush=True)
                        with _feishu_pending_lock:
                            _feishu_pending.pop(run_id, None)
                time.sleep(15)
            except Exception as e:
                print(f"[feishu] poll loop error: {e}", file=sys.stderr, flush=True)
                time.sleep(30)
    finally:
        loop.close()


# 简单的 per-IP token bucket 防止 webhook 被刷。Feishu 自己 ~3 个 retry,
# 正常单 IP 每秒不该超过个位数请求。
_feishu_webhook_rate: dict[str, list[float]] = {}
_feishu_webhook_rate_lock = threading.Lock()


def _feishu_rate_limit_ok(ip: str) -> bool:
    if FEISHU_WEBHOOK_RATE_LIMIT <= 0:
        return True
    now = time.time()
    window = 60.0
    cutoff = now - window
    with _feishu_webhook_rate_lock:
        bucket = [t for t in _feishu_webhook_rate.get(ip, []) if t > cutoff]
        if len(bucket) >= FEISHU_WEBHOOK_RATE_LIMIT:
            _feishu_webhook_rate[ip] = bucket
            return False
        bucket.append(now)
        _feishu_webhook_rate[ip] = bucket
    return True


def _check_feishu_secret(body: dict, header: dict) -> bool:
    """v1 schema body.token / v2 schema header.token 任一匹配即可。
    必须有 FEISHU_VERIFICATION_TOKEN — 启动时强制要求,这里二次校验。"""
    if not FEISHU_VERIFICATION_TOKEN:
        return False
    incoming = (body.get("token") if body.get("type") == "url_verification"
                else header.get("token")) or ""
    return hmac.compare_digest(str(incoming), FEISHU_VERIFICATION_TOKEN)


# Webhook
async def feishu_events(request: Request):
    """Feishu event webhook.

    安全前置:
    1. FEISHU_ENABLED 隐含「verification token 或 encrypt key 已设」(启动时校验)
    2. 每个 POST 都必须带正确 token,不接受裸调用
    3. body size 上限防 OOM,rate limit 防刷
    """
    if not FEISHU_ENABLED:
        return JSONResponse({"error": "feishu integration not configured"},
                            status_code=503)

    # body size 上限 — 提前从 Content-Length 拦截,防止恶意大包占内存。
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > FEISHU_WEBHOOK_MAX_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    client_ip = request.client.host if request.client else "unknown"
    if not _feishu_rate_limit_ok(client_ip):
        print(f"[feishu] rate-limited {client_ip}", flush=True)
        return JSONResponse({"error": "rate limited"}, status_code=429)

    body_bytes = await request.body()
    if len(body_bytes) > FEISHU_WEBHOOK_MAX_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    header = body.get("header") or {}
    if not _check_feishu_secret(body, header):
        # 不告诉攻击者具体哪个字段错了
        print(f"[feishu] reject bad token from {client_ip}", flush=True)
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    # Legacy URL verification (schema v1)
    if body.get("type") == "url_verification":
        print(f"[feishu/webhook] url_verification from {client_ip}", flush=True)
        return JSONResponse({"challenge": body.get("challenge")})

    # Schema v2
    if body.get("schema") == "2.0":
        event_id = header.get("event_id", "")
        event_type = header.get("event_type", "")
        print(f"[feishu/webhook] event_type={event_type} event_id={event_id} "
              f"src={client_ip}", flush=True)
        if _is_duplicate_feishu_event(event_id):
            print(f"[feishu/webhook] dedup: drop duplicate event_id={event_id}",
                  flush=True)
            return JSONResponse({"code": 0})
        if event_type == "im.message.receive_v1":
            asyncio.create_task(_feishu_handle_message(body))
        else:
            print(f"[feishu/webhook] event_type={event_type!r} not handled "
                  f"(only im.message.receive_v1 dispatched)", flush=True)
        return JSONResponse({"code": 0})

    print(f"[feishu/webhook] unknown body shape, src={client_ip}, "
          f"keys={list(body.keys())[:10]}", flush=True)
    return JSONResponse({"code": 0})


HELP_TEXT = (
    "👋 vibe-trading bot — 直接说人话即可,LLM 会理解意图。"
    "完整说明用富卡片返回,如果你只看到这行纯文本,说明卡片渲染挂了。"
)


_HELP_GITHUB_URL = "https://github.com/shao60533/michael-vibe-trading"


def _send_help(chat_id: str) -> None:
    """发使用说明卡片;若卡片渲染或发送失败,降级到纯文本不至于完全没回应。"""
    try:
        card = _build_help_card()
        _feishu_send_card(chat_id, "chat_id", card)
        print(f"[feishu/help] sent card to {chat_id}", flush=True)
    except Exception as e:
        print(f"[feishu/help] card send failed: {e}, fallback to text",
              flush=True)
        try:
            _feishu_send_text(chat_id, "chat_id",
                f"{HELP_TEXT}\n\n📦 完整说明: {_HELP_GITHUB_URL}")
        except Exception as e2:
            print(f"[feishu/help] text fallback also failed: {e2}", flush=True)


def _build_help_card() -> dict:
    """Render the bot usage guide as a Feishu interactive card.

    内容必须与 README『飞书使用速查』节保持一致 — 它们是同一份内容的两个 surface。
    """
    sections = [
        ("📊 1️⃣ 个股分析",
         "• `分析苹果` / `看下 NVDA` / `茅台怎么样` — 默认综合投委会\n"
         "• `英伟达技术面` / `茅台财报` / `小米风险评估` — 自动切对应 preset\n"
         "• `BTC 链上活跃度` — 加密研究\n"
         "• `分析 002594,用陈小群` — A 股 + 强制指定游资视角\n"
         "• `控回撤派看 隆基` — 派别名映射到对应游资"),
        ("🏢 2️⃣ 行业 / 板块 / 量化",
         "• `半导体板块` / `光模块怎么样` — swarm 板块轮动分析(慢,5-15 分钟)\n"
         "• `跑下行业因子量化分析` — LightGBM 行业轮动预测 + 回测(快,1-3 分钟)\n"
         "• `板块轮动 lightgbm 预测` — 同上"),
        ("🌲 3️⃣ Sequoia-X 选股",
         "• `跑下 Sequoia-X 扫描` / `红杉策略选股` — 6 策略 × 活跃 300 只 × 5 天\n"
         "• `海龟突破` / `RPS 突破` / `涨停洗盘` / `高位窄幅旗形` — 任一关键词都识别\n"
         "约 1-3 分钟"),
        ("📋 4️⃣ 历史 / 运维",
         "• `最近跑过哪些` — 列你自己最近 10 个 run(权限隔离,看不到他人/他群)\n"
         "• `失败的 run` / `当前在跑的` — 按状态过滤\n"
         "• `查一下 latest` / `查一下 swarm-xxx` — 拉报告\n"
         "• `取消 latest` / `取消 swarm-xxx` — 杀掉卡死的\n"
         "• `presets` / `怎么用` — 查 preset 列表 / 这条帮助"),
        ("🐊 5️⃣ 10 位游资速查",
         "**通用**:小鳄鱼(理解力派,默认)\n"
         "**首板/模式**:北京炒家(模式派)\n"
         "**龙头**:陈小群(龙头信仰派) / 一瞬流光(高位接力派)\n"
         "**情绪**:92 科比(情绪周期派)\n"
         "**资金/低吸**:涅盘重升(资金流派) / 归因(资讯派)\n"
         "**进攻**:小睿睿(进攻派)\n"
         "**稳健**:采莲路(控回撤派) / 华东大导弹(低频狙击派)\n"
         "用法:`用 X 看 Y` 或 `X 派分析 Y`"),
        ("📤 6️⃣ 输出形式",
         "每次分析推回三处:\n"
         "**飞书卡片**(精简,30 秒看完核心)+ **飞书文档**(完整,落「投研文件夹」组织内可见)+ **Notion 归档**(跨平台备份)"),
        ("⚠️ 注意事项",
         "• **群权限隔离**:你只能查/取消本群本人的 run,不能跨群\n"
         "• **部署重启**:服务部署会中断进行中的分析,bot 会主动告知「请重发」\n"
         "• **数据时效**:免费接口可能延迟或字段口径差异,以官方为准\n"
         "• **不构成投资建议**:所有输出仅为研究参考"),
    ]
    elements: list[dict] = [
        {"tag": "div",
         "text": {"tag": "lark_md",
                  "content": ("📖 **vibe-trading bot 使用速查**\n"
                              "A 股 / 美港股 / 加密 多市场 AI 投研助手 — 28 个分析师 preset + 10 位游资视角 + 行业因子量化 + Sequoia-X 选股")}},
    ]
    for title, body in sections:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{title}**\n{body}"},
        })
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📦 GitHub 仓库 / README"},
            "url": _HELP_GITHUB_URL,
            "type": "primary",
        }],
    })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "随时发『怎么用』可再次拉出这张卡"}],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "vibe-trading bot · 使用说明"},
            "template": "blue",
        },
        "elements": elements,
    }


def _run_owner(run_id: str) -> tuple[str, str]:
    """Read (chat_id, sender_open_id) ownership for a run from feishu_meta.json.
    Empty strings if meta missing (run wasn't initiated via Feishu — only
    visible to admin)."""
    # 先看 in-memory pending(in-flight)
    with _feishu_pending_lock:
        meta = _feishu_pending.get(run_id)
    if not meta:
        meta = _load_feishu_meta(run_id) or {}
    return (meta.get("receive_id") or ""), (meta.get("sender_open_id") or "")


def _authz_run_for(run_id: str, chat_id: str, sender_open_id: str) -> bool:
    """True if the requester (chat + sender) can access this run.

    Rule: requester sees a run iff the run was triggered from THIS chat
    OR by THIS sender personally. 这样:
      - 群 A 的用户不能查 / 取消 / 重发 群 B 的 run
      - 私聊也不能查别人的 run
    No-meta runs (e.g. MCP-direct fires by admin) are NOT visible to any
    Feishu chat — those must go through /_debug/republish with admin token.
    """
    owner_chat, owner_sender = _run_owner(run_id)
    if not owner_chat and not owner_sender:
        return False
    if chat_id and owner_chat == chat_id:
        return True
    if sender_open_id and owner_sender == sender_open_id:
        return True
    return False


def _resolve_latest_run_id(filter_status: str | None = "completed",
                            chat_id: str = "", sender_open_id: str = "") -> str | None:
    """Return the most recent run_id this chat/sender owns. None if no match.

    Authz: only sees runs originating from THIS chat or THIS sender's open_id."""
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    try:
        store = SwarmStore(base_dir=swarm_dir)
        runs = store.list_runs() or []
    except Exception:
        return None
    if filter_status:
        runs = [r for r in runs if r.status.value == filter_status]
    runs.sort(key=lambda r: r.created_at, reverse=True)
    for r in runs:
        if _authz_run_for(r.id, chat_id, sender_open_id):
            return r.id
    return None


def _build_preset_vars(preset: str, target: str | None, market: str | None,
                      raw_text: str) -> dict[str, str]:
    """Build the variables dict expected by the given preset.

    Most presets accept {target, market}. A few take different keys
    (commodity/horizon, timeframe, goal, etc.); we map our extracted entity
    into the right slot AND also include common defaults so unused keys are
    silently ignored by the preset template.
    """
    t = target or ""
    m = market or "US"
    base = {
        "target": t,
        "market": m,
        # Defaults — preset uses whichever subset it declares
        "commodity": t,
        "horizon": "3M",
        "timeframe": "3M",
        "goal": f"分析 {t}" if t else raw_text[:120],
        "view": "neutral",
        "event_type": "general",
        "fund_type": "etf",
        "target_variable": "return",
    }
    return base


async def _feishu_handle_message(body: dict):
    """Parse an incoming message + dispatch action. Fire-and-forget."""
    try:
        event = body.get("event") or {}
        msg = event.get("message") or {}
        chat_id = msg.get("chat_id")
        if not chat_id:
            print(f"[feishu/msg] skip: missing chat_id (body keys={list(body.keys())})",
                  flush=True)
            return
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        sender_open_id = sender_id.get("open_id") or ""
        chat_type = msg.get("chat_type") or ""  # 'p2p' (DM) or 'group'
        msg_type = msg.get("message_type") or ""
        print(f"[feishu/msg] received chat={chat_id} type={chat_type} "
              f"sender={sender_open_id[:12]}.. msg_type={msg_type}", flush=True)
        if msg_type != "text":
            print(f"[feishu/msg] reject: msg_type={msg_type} (only text supported)",
                  flush=True)
            _feishu_send_text(chat_id, "chat_id",
                              "目前只支持文本消息。发 help 看用法。")
            return
        try:
            content = json.loads(msg.get("content") or "{}")
        except Exception as je:
            print(f"[feishu/msg] content JSON parse failed: {je}", flush=True)
            content = {}
        raw_text = content.get("text", "") or ""
        text = _strip_mentions(raw_text)
        print(f"[feishu/msg] text(stripped)={text[:120]!r}", flush=True)
        if not text:
            print(f"[feishu/msg] empty text after strip_mentions, send help", flush=True)
            _send_help(chat_id)
            return

        # ─── routing ───
        if _is_factor_research_request(text):
            print(f"[feishu/dispatch] direct industry factor research "
                  f"chat={chat_id} sender={sender_open_id[:12]}..", flush=True)
            await _feishu_handle_factor_research(
                chat_id, sender_open_id=sender_open_id, chat_type=chat_type)
            return

        if _is_sequoia_scan_request(text):
            print(f"[feishu/dispatch] direct sequoia-x scan "
                  f"chat={chat_id} sender={sender_open_id[:12]}..", flush=True)
            await _feishu_handle_sequoia_scan(
                chat_id, sender_open_id=sender_open_id, chat_type=chat_type)
            return

        explicit_preset, cleaned_text = _parse_explicit_preset(text)
        if explicit_preset:
            target, market = _extract_target(cleaned_text)
            print(f"[feishu/dispatch] explicit preset={explicit_preset} "
                  f"target={target} market={market}", flush=True)
            await _fire_swarm(chat_id, explicit_preset, target, market, cleaned_text,
                              sender_open_id=sender_open_id, chat_type=chat_type)
            return

        llm_result = await _llm_route(text)

        if llm_result is not None:
            action = llm_result.get("action")
            print(f"[feishu/dispatch] action={action} "
                  f"preset={llm_result.get('preset')} "
                  f"target={llm_result.get('target')} "
                  f"gurus={llm_result.get('gurus')}", flush=True)
            if action == "run_swarm":
                # Optional guru override — only honored when LLM router extracted
                # explicit names; whitelist-filtered downstream in _fire_swarm.
                gurus_raw = llm_result.get("gurus") or []
                gurus = [g for g in gurus_raw if isinstance(g, str)] if isinstance(gurus_raw, list) else []
                await _fire_swarm(
                    chat_id,
                    llm_result.get("preset") or FEISHU_DEFAULT_PRESET,
                    llm_result.get("target"),
                    llm_result.get("market") or "US",
                    text,
                    sender_open_id=sender_open_id,
                    chat_type=chat_type,
                    gurus_override=gurus,
                )
            elif action == "list_runs":
                await _feishu_handle_list_runs(
                    chat_id,
                    status_filter=llm_result.get("status_filter"),
                    limit=int(llm_result.get("limit") or 10),
                    sender_open_id=sender_open_id,
                )
            elif action == "status":
                run_id = llm_result.get("run_id") or "latest"
                if run_id == "latest":
                    resolved = (_resolve_latest_run_id("completed", chat_id, sender_open_id)
                                or _resolve_latest_run_id(None, chat_id, sender_open_id))
                    if not resolved:
                        _feishu_send_text(chat_id, "chat_id", "没有你的 run 记录。")
                        return
                    run_id = resolved
                # authz:run 必须属于这个 chat 或这个 sender
                if not _authz_run_for(run_id, chat_id, sender_open_id):
                    _feishu_send_text(chat_id, "chat_id",
                                      f"⛔ 这个 run 不属于本聊天,无权查看")
                    return
                await _feishu_handle_status(chat_id, run_id)
            elif action == "cancel_run":
                run_id = llm_result.get("run_id") or "latest"
                if run_id == "latest":
                    resolved = (_resolve_latest_run_id("running", chat_id, sender_open_id)
                                or _resolve_latest_run_id(None, chat_id, sender_open_id))
                    if not resolved:
                        _feishu_send_text(chat_id, "chat_id", "没有你的 run 可以 cancel。")
                        return
                    run_id = resolved
                if not _authz_run_for(run_id, chat_id, sender_open_id):
                    _feishu_send_text(chat_id, "chat_id",
                                      f"⛔ 这个 run 不属于本聊天,无权 cancel")
                    return
                await _feishu_handle_cancel_run(chat_id, run_id)
            elif action == "help":
                _send_help(chat_id)
            elif action == "presets":
                await _feishu_handle_list_presets(chat_id)
            elif action == "clarify":
                _feishu_send_text(chat_id, "chat_id",
                                  llm_result.get("message")
                                  or "请明确一下你想分析什么。发 help 看示例。")
            elif action == "reject":
                _feishu_send_text(chat_id, "chat_id",
                                  llm_result.get("message")
                                  or "这个不在我能力范围内。")
            return

        # Fallback when LLM is unavailable: regex ticker + keyword preset classifier.
        print(f"[feishu/dispatch] LLM router returned None, falling back to regex",
              flush=True)
        target, market = _extract_target(text)
        if not target:
            print(f"[feishu/dispatch] regex fallback: no ticker recognized, "
                  f"text={text[:60]!r}", flush=True)
            _feishu_send_text(
                chat_id, "chat_id",
                "没识别出来,试试 SOXL / 1810.HK / 605117 / BTC,或发 help。",
            )
            return
        fallback_preset = _classify_preset(text, FEISHU_DEFAULT_PRESET)
        print(f"[feishu/dispatch] regex fallback fire: preset={fallback_preset} "
              f"target={target} market={market}", flush=True)
        await _fire_swarm(chat_id, fallback_preset, target, market, text,
                          sender_open_id=sender_open_id, chat_type=chat_type)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[feishu] message handler error: {e}", file=sys.stderr, flush=True)


# Preset → 中文显示名(用于 ack 文案,内部仍用英文 key)
_PRESET_ZH = {
    "investment_committee": "投委会",
    "technical_analysis_panel": "技术面",
    "earnings_research_desk": "财报",
    "fundamental_research_team": "基本面",
    "risk_committee": "风险评估",
    "quant_strategy_desk": "量化策略",
    "macro_strategy_forum": "宏观",
    "macro_rates_fx_desk": "利率汇率",
    "commodity_research_team": "大宗商品",
    "crypto_research_lab": "加密研究",
    "crypto_trading_desk": "加密交易",
    "derivatives_strategy_desk": "衍生品",
    "sector_rotation_team": "板块轮动",
    "pairs_research_lab": "配对交易",
    "event_driven_task_force": "事件驱动",
    "sentiment_intelligence_team": "情绪舆情",
    "fund_selection_panel": "基金筛选",
    "credit_research_team": "信用研究",
    "factor_research_committee": "因子研究",
    "global_allocation_committee": "全球配置",
    "ml_quant_lab": "ML 量化",
    "geopolitical_war_room": "地缘政治",
    "portfolio_review_board": "组合审议",
}


async def _fire_swarm(chat_id: str, preset: str, target: str | None,
                      market: str | None, raw_text: str,
                      sender_open_id: str = "",
                      chat_type: str = "",
                      gurus_override: list[str] | None = None) -> None:
    """Start a swarm run, register for poll-back, ack to user."""
    ENTITY_OPTIONAL_PRESETS = {
        "macro_strategy_forum", "macro_rates_fx_desk",
        "sector_rotation_team", "risk_committee",
        "global_allocation_committee", "factor_research_committee",
        "fund_selection_panel", "geopolitical_war_room",
    }
    if not target and preset not in ENTITY_OPTIONAL_PRESETS:
        _feishu_send_text(
            chat_id, "chat_id",
            f"没识别出标的,试试 '分析 SOXL' / '财报 茅台'。",
        )
        return

    if preset not in KNOWN_PRESETS:
        _feishu_send_text(chat_id, "chat_id",
                          f"不认识 preset '{preset}',发 presets 看列表。")
        return

    # In-flight 拦截:同一 chat + 同一 target 已有在跑的 run → 不再启第二个,告知用户。
    if target:
        existing_rid: str | None = None
        with _feishu_pending_lock:
            for rid, meta in _feishu_pending.items():
                if meta.get("receive_id") == chat_id and meta.get("target") == target:
                    existing_rid = rid
                    break
        if existing_rid:
            _feishu_send_text(
                chat_id, "chat_id",
                f"⏳ {target} 已经在跑了({_PRESET_ZH.get(preset, preset)}),完成会自动推回。\n"
                f"run_id: {existing_rid}",
            )
            return

    from src.swarm.runtime import SwarmRuntime
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)
    runtime = SwarmRuntime(store=store)
    variables = _build_preset_vars(preset, target, market, raw_text)
    try:
        run = runtime.start_run(preset, variables)
    except FileNotFoundError as e:
        _feishu_send_text(chat_id, "chat_id",
                          f"preset '{preset}' 不存在。发 presets 看完整列表。\n详细: {e}")
        return
    except ValueError as e:
        _feishu_send_text(chat_id, "chat_id",
                          f"preset '{preset}' 参数校验失败: {e}")
        return
    except Exception as e:
        _feishu_send_text(chat_id, "chat_id", f"启动 swarm 失败: {e}")
        return

    # Whitelist guru override against loaded skills, cap at GURU_VIEW_MAX.
    safe_gurus = [g for g in (gurus_override or [])
                  if g in _GURU_SKILLS][:GURU_VIEW_MAX] if gurus_override else []

    meta_payload = {
        "receive_id": chat_id,
        "receive_id_type": "chat_id",
        "sender_open_id": sender_open_id,
        "chat_type": chat_type,
        "target": target or "",
        "preset": preset,
        "gurus_override": safe_gurus,
    }
    with _feishu_pending_lock:
        _feishu_pending[run.id] = meta_payload
    # Persist to disk so restart can recover routing.
    _write_feishu_meta(run.id, chat_id, "chat_id", sender_open_id, chat_type,
                       target=target or "", preset=preset,
                       gurus_override=safe_gurus)

    preset_zh = _PRESET_ZH.get(preset, preset)
    if target:
        head = f"📊 {target}({market or '?'}) · {preset_zh}"
    else:
        head = f"📊 {preset_zh}"
    guru_line = ""
    if safe_gurus:
        guru_names = " + ".join(GURU_META.get(g, (g, ""))[0] for g in safe_gurus)
        guru_line = f"\n🐊 指定游资: {guru_names}"
    _feishu_send_text(
        chat_id, "chat_id",
        f"{head}{guru_line}\n开始分析,预计 5-15 分钟,完成自动推回。\n"
        f"查进度发:查一下 {run.id}",
    )


async def _feishu_handle_factor_research(chat_id: str,
                                          sender_open_id: str = "",
                                          chat_type: str = "") -> None:
    """Run the local industry factor module, then push through the SAME publish
    pipeline as a swarm run — DeepSeek-summarized structured card + Feishu docx
    + Notion page.

    Treated as a `sector_rotation_team` preset (→ macro_theme template), so:
      - 游资观点 不附加(macro_theme 模板默认不带 youzi)
      - kv_fields / sections / short_tldr 全部 LLM 抽
      - 同一格式:卡片(简洁)+ docx(完整)+ Notion(归档)
    """
    _feishu_send_text(
        chat_id, "chat_id",
        "📊 开始跑 A 股行业因子量化分析(行业行情 + LightGBM 预测 + 回测 + 研报热度)\n"
        "完成后会推回 互动卡片 + 飞书文档 + Notion,约 1-3 分钟。",
    )
    try:
        from factor_analysis import run_industry_factor_research
        result = await asyncio.to_thread(
            run_industry_factor_research,
            lookback_days=260, test_days=22, horizon_days=5,
            top_k=5, board_limit=80, report_days=7,
        )
    except ImportError as exc:
        _feishu_send_text(chat_id, "chat_id",
            f"❌ 因子模块依赖缺失: {exc}\n请确认部署镜像已装 pandas/numpy/sklearn/lightgbm。")
        return
    except Exception as exc:
        _feishu_send_text(chat_id, "chat_id",
            f"❌ 因子分析失败: {type(exc).__name__}: {exc}")
        return

    # 合成一个伪 run_id + Run-like 对象,走标准 publish 流水线。
    # preset=sector_rotation_team → macro_theme 模板 → 适合 industry rotation。
    from types import SimpleNamespace
    run_id = f"factor-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
    full_report = result.get("report_markdown") or ""
    fake_run = SimpleNamespace(
        id=run_id,
        status=SimpleNamespace(value="completed"),
        final_report=full_report,
        preset_name="sector_rotation_team",
        user_vars={"target": "A股行业轮动",
                   "market": "CN",
                   "model": result.get("model", {}).get("name", "")},
        total_input_tokens=0,
        total_output_tokens=0,
        tasks=[],
    )
    info = {"receive_id": chat_id, "receive_id_type": "chat_id",
            "sender_open_id": sender_open_id, "chat_type": chat_type,
            "target": "A股行业轮动", "preset": "sector_rotation_team",
            "gurus_override": [], "skip_feishu_card": False}
    try:
        await _publish_terminal_run(fake_run, info)
    except Exception as exc:
        print(f"[factor] publish err {run_id}: {type(exc).__name__}: {exc}",
              flush=True)
        _feishu_send_text(chat_id, "chat_id",
            f"⚠️ 因子分析跑完了但 publish 失败: {type(exc).__name__}: {exc}\n"
            f"run_id: {run_id}")


# 整体硬超时 (秒)。Sina/Eastmoney 接口偶发卡死会拖死 worker — 5 分钟兜底。
SEQUOIA_HARD_TIMEOUT = int(os.environ.get("SEQUOIA_HARD_TIMEOUT", "300"))


async def _feishu_handle_sequoia_scan(chat_id: str,
                                       sender_open_id: str = "",
                                       chat_type: str = "") -> None:
    """Run the local Sequoia-X scanner, then push through the SAME publish
    pipeline as a swarm run (card + 飞书 docx + Notion).

    并发模型:无 dedup,允许同聊天 / 不同 sender 并发触发。
    - 飞书 retry 已在 webhook 层 event_id 维度去重
    - 同一聊天 2+ 用户 / 2 个不同时间窗的需求都应该被允许
    硬超时由 asyncio.wait_for 各自独立兜底。
    """
    _feishu_send_text(chat_id, "chat_id",
        "🌲 开始跑 Sequoia-X A 股选股扫描\n"
        "(6 策略 × 最近 5 个交易日 × 活跃 300 只,海龟突破 / RPS / 均线放量 / 涨停洗盘 等)\n"
        f"约 1-3 分钟,硬超时 {SEQUOIA_HARD_TIMEOUT}s,完成后推回 卡片 + 飞书文档 + Notion。")
    try:
        from sequoia_x import run_weekly_scan, SequoiaScanError
        result = await asyncio.wait_for(
            asyncio.to_thread(
                run_weekly_scan,
                days=5, max_symbols=300,
            ),
            timeout=SEQUOIA_HARD_TIMEOUT,
        )
    except SequoiaScanError as exc:
        _feishu_send_text(chat_id, "chat_id",
            f"❌ Sequoia-X 扫描中止:{exc}")
        return
    except asyncio.TimeoutError:
        _feishu_send_text(chat_id, "chat_id",
            f"❌ Sequoia-X 扫描超过 {SEQUOIA_HARD_TIMEOUT}s 硬超时 — "
            "可能 sina/eastmoney 接口阻塞,稍后再试。")
        return
    except ImportError as exc:
        _feishu_send_text(chat_id, "chat_id",
            f"❌ sequoia_x 模块或依赖缺失:{exc}")
        return
    except Exception as exc:
        print(f"[sequoia] unexpected err: {type(exc).__name__}: {exc}",
              flush=True)
        _feishu_send_text(chat_id, "chat_id",
            f"❌ Sequoia-X 扫描失败:{type(exc).__name__}: {exc}")
        return

    # publish 走标准管道
    from types import SimpleNamespace
    run_id = f"sequoia-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
    cov = result.get("coverage", {}) or {}
    scope = (f"{cov.get('fetched_symbols', '?')}/"
             f"{cov.get('requested_symbols', '?')}只"
             f" × {len(result.get('dates', []))}天"
             f" ({cov.get('elapsed_seconds', '?')}s)")
    fake_run = SimpleNamespace(
        id=run_id,
        status=SimpleNamespace(value="completed"),
        final_report=result.get("report_markdown") or "(empty report)",
        preset_name="sector_rotation_team",
        user_vars={"target": "A股 Sequoia-X 选股", "market": "CN",
                   "scope": scope,
                   "error_symbols": str(cov.get("error_symbols", 0))},
        total_input_tokens=0, total_output_tokens=0, tasks=[],
    )
    info = {"receive_id": chat_id, "receive_id_type": "chat_id",
            "sender_open_id": sender_open_id, "chat_type": chat_type,
            "target": "A股 Sequoia-X 选股",
            "preset": "sector_rotation_team",
            "gurus_override": [], "skip_feishu_card": False}
    try:
        await _publish_terminal_run(fake_run, info)
    except Exception as exc:
        print(f"[sequoia] publish err {run_id}: "
              f"{type(exc).__name__}: {exc}", flush=True)
        _feishu_send_text(chat_id, "chat_id",
            f"⚠️ Sequoia-X 扫描完成但 publish 失败:"
            f"{type(exc).__name__}: {exc}\nrun_id: {run_id}")


async def _feishu_handle_cancel_run(chat_id: str, run_id: str) -> None:
    """Kill a stuck/unwanted run. Mirrors /_debug/purge-run logic."""
    import ctypes, pathlib, shutil
    from src.swarm.store import SwarmStore
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"

    actions: list[str] = []
    # 1. Try the runtime's built-in cancel path (sets the cancel_event).
    target_name = f"swarm-{run_id}"
    killed_thread = False
    for t in threading.enumerate():
        if t.name == target_name and t.is_alive() and t.ident:
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(t.ident), ctypes.py_object(SystemExit))
            actions.append(f"中止线程({t.name}) → {res}")
            killed_thread = True
            if res > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(t.ident), 0)
                actions.append("回滚")

    # 2. Mark run as cancelled in store (if it exists) so list_runs reflects it.
    store = SwarmStore(base_dir=swarm_dir)
    try:
        run = store.load_run(run_id)
    except Exception:
        run = None

    # 3. Drop from pending tracker.
    with _feishu_pending_lock:
        _feishu_pending.pop(run_id, None)

    # 4. Remove disk artifacts.
    target_dir = swarm_dir / run_id
    if target_dir.exists():
        try:
            shutil.rmtree(target_dir)
            actions.append("清理 disk artifacts")
        except Exception as e:
            actions.append(f"清理失败: {e}")
    elif run is None:
        _feishu_send_text(chat_id, "chat_id", f"找不到 run: {run_id}")
        return

    summary = "\n  • ".join(actions) if actions else "(no-op,run 不在运行也无 disk artifacts)"
    _feishu_send_text(chat_id, "chat_id", f"✅ 已 cancel {run_id}\n  • {summary}")


async def _feishu_handle_list_presets(chat_id: str):
    """List all available swarm presets in the chat."""
    try:
        from src.swarm.presets import list_presets
        presets = list_presets()
    except Exception as e:
        _feishu_send_text(chat_id, "chat_id", f"读取 preset 列表失败: {e}")
        return
    lines = ["📋 可用 swarm preset(共 {}):".format(len(presets))]
    for p in presets:
        name = p.get("name") if isinstance(p, dict) else getattr(p, "name", str(p))
        title = p.get("title") if isinstance(p, dict) else getattr(p, "title", "")
        agents = p.get("agent_count") if isinstance(p, dict) else getattr(p, "agent_count", "?")
        lines.append(f"  {name:35s} agents={agents}  {title or ''}")
    lines.append("\n用法: preset:<name> <target>")
    _feishu_send_text(chat_id, "chat_id", "\n".join(lines))


async def _feishu_handle_status(chat_id: str, run_id: str):
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)
    try:
        run = store.load_run(run_id)
    except Exception as e:
        _feishu_send_text(chat_id, "chat_id", f"读取 run 失败: {e}")
        return
    if run is None:
        _feishu_send_text(chat_id, "chat_id", f"找不到 run: {run_id}")
        return
    status = run.status.value
    if status in ("completed", "failed", "cancelled"):
        # Re-run the same publish chain (summary → docx → notion → card).
        # Raw markdown never goes into chat now.
        await _publish_terminal_run(run, {"receive_id": chat_id, "receive_id_type": "chat_id"})
        return
    # Show task-level state
    counts: dict[str, int] = {}
    for t in (getattr(run, "tasks", []) or []):
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    counts_str = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    _feishu_send_text(
        chat_id, "chat_id",
        f"run: {run_id}\nstatus: {status}\ntasks: {counts_str or '(none)'}\n"
        f"tokens: in={run.total_input_tokens} out={run.total_output_tokens}\n"
        f"preset: {run.preset_name}",
    )


_VALID_RUN_STATUS_FILTERS = frozenset({"completed", "failed", "running", "cancelled", "pending"})


async def _feishu_handle_list_runs(chat_id: str, status_filter: str | None = None,
                                    limit: int = 10,
                                    sender_open_id: str = "") -> None:
    """List runs **owned by this chat or this sender** only.
    群 A 看不到群 B 的 run,私聊看不到他人的 run。"""
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)
    try:
        runs = store.list_runs() or []
    except Exception as e:
        _feishu_send_text(chat_id, "chat_id", f"读取 runs 失败: {e}")
        return

    runs = sorted(runs, key=lambda r: r.created_at, reverse=True)
    if status_filter:
        sf = str(status_filter).strip().lower()
        if sf not in _VALID_RUN_STATUS_FILTERS:
            _feishu_send_text(chat_id, "chat_id",
                              f"status_filter 必须是: {', '.join(sorted(_VALID_RUN_STATUS_FILTERS))}")
            return
        runs = [r for r in runs if r.status.value == sf]

    # 权限过滤:只保留 chat_id 或 sender_open_id 拥有的 run。
    runs = [r for r in runs if _authz_run_for(r.id, chat_id, sender_open_id)]

    if limit < 1:
        limit = 10
    runs = runs[:limit]

    if not runs:
        scope = f" (status={status_filter})" if status_filter else ""
        _feishu_send_text(chat_id, "chat_id", f"暂无你的 run 记录{scope}。")
        return

    header = f"你的最近 {len(runs)} 个 run" + (f" (status={status_filter})" if status_filter else "") + ":"
    lines = [header]
    for r in runs:
        tok = f"{r.total_input_tokens}/{r.total_output_tokens}"
        lines.append(f"  {r.id}  {r.preset_name}  {r.status.value}  tok={tok}")
    lines.append("\n💡 查具体报告: 查一下 <run_id>  或  status latest")
    _feishu_send_text(chat_id, "chat_id", "\n".join(lines))


# ─────────── lifespan: start Feishu poller on app startup ───────────
from contextlib import asynccontextmanager
mcp_app = mcp_server.mcp.http_app(transport="sse")


def _notify_interrupted_runs() -> int:
    """On graceful shutdown (Railway SIGTERM), tell each pending Feishu chat that
    their in-flight run is dead so they don't wait forever. Container restart
    will recover persisted runs that already finished but failed to publish — but
    threads that were mid-LLM are gone and can't resume.

    Returns the number of chats successfully notified.
    """
    notified = 0
    try:
        with _feishu_pending_lock:
            pending = dict(_feishu_pending)
    except Exception as e:
        print(f"[shutdown] snapshot _feishu_pending err: {e}", flush=True)
        return 0

    if not pending:
        print("[shutdown] no pending runs to notify", flush=True)
        return 0

    print(f"[shutdown] notifying {len(pending)} pending chats of interruption",
          flush=True)
    for run_id, meta in pending.items():
        chat_id = meta.get("receive_id") or ""
        chat_type = meta.get("receive_id_type") or "chat_id"
        target = meta.get("target") or "(无标的)"
        preset = meta.get("preset") or ""
        if not chat_id:
            continue
        preset_zh = _PRESET_ZH.get(preset, preset) if preset else "?"
        text = (
            f"⚠️ 服务部署重启,本次分析被中断\n"
            f"目标: {target} · preset: {preset_zh}\n"
            f"run_id: {run_id}\n"
            f"请重新发送原指令(已记录的进度无法恢复)"
        )
        try:
            _feishu_send_text(chat_id, chat_type, text)
            notified += 1
        except Exception as e:
            print(f"[shutdown] notify {run_id} err: {e}", flush=True)
    print(f"[shutdown] notified {notified}/{len(pending)} chats", flush=True)
    return notified


@asynccontextmanager
def _assert_httpx_timeout_not_capped() -> None:
    """启动时自检:_deepseek_json_call 想要的 read=90 没被任何 import 副作用悄悄
    改成更小的值。之前 monkey patch 全局 cap 60s,这里固化为运行时断言。"""
    tc = httpx.Timeout(connect=10, read=90, write=15, pool=5)
    c = httpx.AsyncClient(timeout=tc)
    try:
        rd = c.timeout.read
        if abs(float(rd) - 90.0) > 0.01:
            raise RuntimeError(
                f"FATAL: httpx AsyncClient read timeout is {rd}, expected 90. "
                f"Something is monkey-patching httpx clients.")
    finally:
        # 不真发请求,纯结构 assert,关掉同步释放。
        # AsyncClient.aclose 是协程,这里 best-effort 不 await。
        pass
    print(f"[boot] httpx timeout self-test ok: read={rd}s", flush=True)


async def _lifespan(app):
    # 启动断言
    _assert_httpx_timeout_not_capped()
    # Defer to FastMCP's own lifespan first
    async with mcp_app.lifespan(app):
        if FEISHU_ENABLED:
            # Restore pending dict from disk so previous-container runs still publish.
            try:
                restored = _restore_feishu_pending_from_disk()
                if restored:
                    print(f"[feishu] restored {restored} pending runs from disk", flush=True)
            except Exception as e:
                print(f"[feishu] restore from disk err: {e}", flush=True)
            t = threading.Thread(target=_feishu_poll_loop, daemon=True, name="feishu-poll")
            t.start()
            print(f"[feishu] enabled. app_id={LARK_APP_ID} default_preset={FEISHU_DEFAULT_PRESET}", flush=True)
            if NOTION_ENABLED:
                parent_desc = (f"db={NOTION_DATABASE_ID[:8]}..." if NOTION_DATABASE_ID
                               else f"page={NOTION_PARENT_PAGE_ID[:8]}...")
                print(f"[feishu] notion sync: enabled ({parent_desc})", flush=True)
            else:
                print("[feishu] notion sync: disabled", flush=True)
        else:
            print("[feishu] disabled (LARK_APP_ID/SECRET not set)", flush=True)
        try:
            yield
        finally:
            # Graceful shutdown — Railway SIGTERM lands here via uvicorn lifespan.
            # We have ~30s before SIGKILL; send interruption notices then exit.
            if FEISHU_ENABLED:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(_notify_interrupted_runs),
                        timeout=20,
                    )
                except asyncio.TimeoutError:
                    print("[shutdown] notify timed out at 20s", flush=True)
                except Exception as e:
                    print(f"[shutdown] notify err: {e}", flush=True)


# ─────────── app assembly ───────────
# 基础路由 — 始终注册。
_base_routes = [
    Route("/", root),
    Route("/healthz", healthz),
    Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize_get, methods=["GET"]),
    Route("/authorize", authorize_post, methods=["POST"]),
    Route("/token", token_endpoint, methods=["POST"]),
]

# /feishu/events 仅在配置了 token/encrypt key 时注册(防裸跑)。
if FEISHU_ENABLED:
    _base_routes.append(
        Route("/feishu/events", feishu_events, methods=["POST"]))

# /_debug/* 仅在 ENABLE_DEBUG_ENDPOINTS=true 且 ADMIN_AUTH_TOKEN 设了时注册。
# Read-only: GET; mutating: POST (purge-run / republish / fix-historic-doc-share)
if DEBUG_ENDPOINTS_ACTIVE:
    _base_routes.extend([
        Route("/_debug/threads", debug_threads, methods=["GET"]),
        Route("/_debug/swarm-state", debug_swarm_state, methods=["GET"]),
        Route("/_debug/env", debug_env, methods=["GET"]),
        Route("/_debug/list-feishu-chats", debug_list_feishu_chats, methods=["GET"]),
        Route("/_debug/purge-run", debug_purge_run, methods=["POST"]),
        Route("/_debug/republish", debug_republish, methods=["POST"]),
        Route("/_debug/fix-historic-doc-share", debug_fix_historic_doc_share,
              methods=["POST"]),
    ])
    print(f"[boot] debug endpoints active: /_debug/* (admin token required)",
          flush=True)
else:
    print(f"[boot] debug endpoints DISABLED "
          f"(ENABLE_DEBUG_ENDPOINTS={ENABLE_DEBUG_ENDPOINTS}, "
          f"ADMIN_AUTH_TOKEN set={bool(ADMIN_AUTH_TOKEN)})", flush=True)

_base_routes.append(Mount("/", app=mcp_app))

app = Starlette(
    routes=_base_routes,
    middleware=[Middleware(AuthMiddleware)],
    lifespan=_lifespan,
)


if __name__ == "__main__":
    print(
        f"[vibe-trading-mcp] listening on 0.0.0.0:{PORT}  "
        f"(SSE: /sse, OAuth: /.well-known/oauth-authorization-server, "
        f"Feishu: {'/feishu/events' if FEISHU_ENABLED else 'disabled'})",
        flush=True,
    )
    uvicorn.run(
        app, host="0.0.0.0", port=PORT,
        log_level="info", timeout_keep_alive=120,
        proxy_headers=True, forwarded_allow_ips="*",
    )
