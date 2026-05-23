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


# ─────────── httpx Client read-timeout cap (60s) ───────────
# Stalled LLM streaming responses can wedge worker threads indefinitely on
# half-dead TCP sockets. Cap every httpx.Client's read timeout at 60s so a
# silent stream raises ReadTimeout in bounded time. Applied BEFORE importing
# anything that constructs httpx clients.
import httpx

_READ_CAP = 60.0
_CONNECT_CAP = 15.0
_WRITE_CAP = 15.0
_POOL_CAP = 5.0


def _capped_timeout(orig):
    if orig is None:
        return httpx.Timeout(connect=_CONNECT_CAP, read=_READ_CAP,
                             write=_WRITE_CAP, pool=_POOL_CAP)
    if isinstance(orig, (int, float)):
        v = float(orig)
        return httpx.Timeout(connect=min(v, _CONNECT_CAP), read=min(v, _READ_CAP),
                             write=min(v, _WRITE_CAP), pool=min(v, _POOL_CAP))
    if isinstance(orig, httpx.Timeout):
        def cap(value, lim):
            return lim if value is None else min(value, lim)
        return httpx.Timeout(connect=cap(orig.connect, _CONNECT_CAP),
                             read=cap(orig.read, _READ_CAP),
                             write=cap(orig.write, _WRITE_CAP),
                             pool=cap(orig.pool, _POOL_CAP))
    return orig


_orig_httpx_client_init = httpx.Client.__init__
def _httpx_client_init_capped(self, *args, **kwargs):
    kwargs["timeout"] = _capped_timeout(kwargs.get("timeout"))
    return _orig_httpx_client_init(self, *args, **kwargs)
httpx.Client.__init__ = _httpx_client_init_capped

_orig_httpx_async_client_init = httpx.AsyncClient.__init__
def _httpx_async_client_init_capped(self, *args, **kwargs):
    kwargs["timeout"] = _capped_timeout(kwargs.get("timeout"))
    return _orig_httpx_async_client_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _httpx_async_client_init_capped


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

PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

SIGNING_KEY = hashlib.sha256(b"vibe-trading-oauth/v1\x00" + EXPECTED_TOKEN.encode()).digest()
AUTH_CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 86400

# Feishu integration
LARK_APP_ID = os.environ.get("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip()
FEISHU_DEFAULT_PRESET = os.environ.get("FEISHU_DEFAULT_PRESET", "investment_committee").strip()
# Link-share permission for every docx the bot creates. Default tenant_readable
# so group members can open the link without applying for permission.
# Valid: tenant_readable / tenant_editable / anyone_readable / anyone_editable / closed
FEISHU_DOC_SHARE_ENTITY = os.environ.get("FEISHU_DOC_SHARE_ENTITY",
                                         "tenant_readable").strip().lower()
FEISHU_ENABLED = bool(LARK_APP_ID and LARK_APP_SECRET)

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
    "/feishu/events",  # Feishu signs the request itself; no Bearer needed
})


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
    try:
        body = await request.json() if (await request.body()) else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return JSONResponse({
        "client_id": "mcp-" + secrets.token_urlsafe(12),
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris", []),
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


async def authorize_get(request):
    params = dict(request.query_params)
    required = ["response_type", "client_id", "redirect_uri",
                "code_challenge", "code_challenge_method"]
    missing = [k for k in required if not params.get(k)]
    if missing:
        return HTMLResponse(f"missing: {', '.join(missing)}", status_code=400)
    if params["response_type"] != "code":
        return HTMLResponse("unsupported_response_type", status_code=400)
    if params["code_challenge_method"] != "S256":
        return HTMLResponse("need S256", status_code=400)
    return HTMLResponse(_render_login(params))


async def authorize_post(request):
    form = await request.form()
    secret = str(form.get("secret", ""))
    params = {k: str(v) for k, v in form.items() if k != "secret"}
    for k in ("response_type", "client_id", "redirect_uri",
              "code_challenge", "code_challenge_method"):
        if not params.get(k):
            return HTMLResponse(f"missing: {k}", status_code=400)
    if not secrets.compare_digest(secret, EXPECTED_TOKEN):
        return HTMLResponse(_render_login(params, "Invalid token."), status_code=401)
    now = int(time.time())
    code = _jwt_encode({
        "typ": "code", "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "code_challenge": params["code_challenge"],
        "code_challenge_method": params["code_challenge_method"],
        "scope": params.get("scope", "mcp"),
        "iat": now, "exp": now + AUTH_CODE_TTL,
    })
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
        p = _jwt_decode(code)
        if not p or p.get("typ") != "code":
            return _oauth_error("invalid_grant", "invalid or expired code")
        if p.get("client_id") != client_id:
            return _oauth_error("invalid_grant", "client_id mismatch")
        if p.get("redirect_uri") != redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri mismatch")
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if not hmac.compare_digest(expected, str(p.get("code_challenge", ""))):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        return _issue_tokens(client_id=client_id, scope=str(p.get("scope", "mcp")))
    if grant == "refresh_token":
        rt = str(form.get("refresh_token", ""))
        if not rt:
            return _oauth_error("invalid_request", "missing refresh_token")
        p = _jwt_decode(rt)
        if not p or p.get("typ") != "refresh":
            return _oauth_error("invalid_grant", "invalid or expired refresh token")
        return _issue_tokens(client_id=str(p.get("client_id", "")),
                             scope=str(p.get("scope", "mcp")))
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
    runs_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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
        "NOTION_API_KEY", "NOTION_DATABASE_ID", "NOTION_PARENT_PAGE_ID",
        "LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
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
      gurus_override (optional list of guru skill names)
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
                                if g in _GURU_SKILLS][:GURU_VIEW_MAX]}
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
    runs_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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


def _parse_explicit_preset(text: str) -> tuple[str | None, str]:
    """Return (preset_name or None, cleaned_text). Strips the preset directive
    from text so downstream extraction isn't polluted."""
    m = _PRESET_OVERRIDE_RE.search(text)
    if not m:
        return None, text
    preset = m.group(1).strip()
    cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
    return preset, cleaned


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
    """
    if not FEISHU_USE_LLM_ROUTER:
        return None
    api_key = (os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "").strip()
    base_url = (os.environ.get("DEEPSEEK_BASE_URL")
                or os.environ.get("OPENROUTER_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LANGCHAIN_MODEL_NAME", "deepseek-v4-pro").strip()
    if not api_key:
        print("[feishu] LLM router skipped (no api key)", flush=True)
        return None

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": LLM_ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 500,  # generous to allow reasoning_content for v4-pro
        "temperature": 0,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=25, write=10, pool=5),
        ) as c:
            r = await c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
            if r.status_code != 200:
                print(f"[feishu] LLM router HTTP {r.status_code}: {r.text[:200]}", flush=True)
                return None
            d = r.json()
            if "choices" not in d:
                print(f"[feishu] LLM router unexpected response: {d}", flush=True)
                return None
            msg = d["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content:
                # Reasoning models may put nothing in content. Try reasoning_content as last resort.
                content = msg.get("reasoning_content") or ""
                # Extract JSON from inside reasoning
                m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content, re.DOTALL)
                if not m:
                    return None
                content = m.group(0)
    except Exception as e:
        print(f"[feishu] LLM router exception: {type(e).__name__}: {e}", flush=True)
        return None

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        print(f"[feishu] LLM router JSON parse failed: {content[:200]}", flush=True)
        return None
    if not isinstance(parsed, dict):
        return None
    action = parsed.get("action")
    if action not in KNOWN_ACTIONS:
        return None
    if action == "run_swarm":
        preset = parsed.get("preset")
        if preset not in KNOWN_PRESETS:
            print(f"[feishu] LLM router returned unknown preset: {preset}", flush=True)
            return None
        if not parsed.get("target"):
            return None
    if action == "status" and not parsed.get("run_id"):
        return None
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
    return (pathlib.Path(mcp_server.__file__).resolve().parent /
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
    runs_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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
  "tldr": "200-350 字的中文综述,流畅自然",
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

    api_key, base_url, model = _get_llm_creds()
    if not api_key:
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
        "- 如果报告不是 A 股短线场景 (美股/港股/加密/期货/宏观/纯长线)，返回空 selected: []\n"
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

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=15, pool=5),
        ) as c:
            r = await c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 1500,
                    "temperature": 0.3,
                },
            )
            if r.status_code != 200:
                print(f"[guru/route] HTTP {r.status_code} run={run_id}: "
                      f"{r.text[:200]}", flush=True)
                return []
            d = r.json()
            msg = d["choices"][0]["message"]
            content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            m = re.search(r"\{[\s\S]*\}", content)
            if not m:
                print(f"[guru/route] no JSON in response run={run_id}", flush=True)
                return []
            parsed = json.loads(m.group(0))
            selected_raw = parsed.get("selected") or []
            # Whitelist + dedupe + cap.
            valid: list[str] = []
            for name in selected_raw:
                if isinstance(name, str) and name in _GURU_SKILLS and name not in valid:
                    valid.append(name)
                if len(valid) >= GURU_VIEW_MAX:
                    break
            print(f"[guru/route] run={run_id} selected={valid} "
                  f"reason={parsed.get('reason','')[:120]}", flush=True)
            return valid
    except Exception as e:
        print(f"[guru/route] exception run={run_id}: {type(e).__name__}: {e}",
              flush=True)
        return []


async def _generate_single_guru_view(guru: str, full_report: str, summary: dict,
                                      run_id: str) -> str | None:
    """Generate one guru's view using their full SKILL.md as voice."""
    skill_text = _GURU_SKILLS.get(guru)
    if not skill_text:
        return None
    display_name, school = GURU_META.get(guru, (guru, "未知派别"))

    api_key, base_url, model = _get_llm_creds()
    if not api_key:
        return None

    system_prompt = (
        skill_text.strip()
        + "\n\n----\n"
        + f"你现在是 A 股游资『{display_name}』本人(派别: {school})。"
        + "读下面这份个股分析报告，严格按你的判断框架给 3-5 句锐评。要点:\n"
        + "1. 主线判断 / 个股定位 / 节奏阶段 / 操作建议 / 风险提示\n"
        + "2. 操作建议要符合你这派的特色 (模式派→首板战法，控回撤派→4 点底线，进攻派→敢上重仓，等)\n\n"
        + "硬规则:\n"
        + "- 全中文，口语化游资风格 (直接、不绕)\n"
        + "- 不复述报告原文，只给『你会怎么看』\n"
        + "- 不要 markdown 列表或 JSON，直接输出 3-5 行短句\n"
        + "- 总长度 180-400 字"
    )
    headline = summary.get("headline") or summary.get("title") or ""
    badge = summary.get("badge") or ""
    user_msg = (
        f"主报告结论: {badge} — {headline}\n\n"
        f"--- 完整报告(截前 8000 字) ---\n{(full_report or '')[:8000]}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=15, pool=5),
        ) as c:
            r = await c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 1200,
                    "temperature": 0.4,
                },
            )
            if r.status_code != 200:
                print(f"[guru/{guru}] HTTP {r.status_code} run={run_id}: "
                      f"{r.text[:200]}", flush=True)
                return None
            d = r.json()
            msg = d["choices"][0]["message"]
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            if not text or text.startswith("不适用"):
                return None
            return text
    except Exception as e:
        print(f"[guru/{guru}] exception run={run_id}: {type(e).__name__}: {e}",
              flush=True)
        return None


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
        if isinstance(view, str) and view:
            display, school = GURU_META.get(guru, (guru, "未知派别"))
            views.append({"guru": guru, "display_name": display,
                          "school": school, "text": view})
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

    api_key = (os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "").strip()
    base_url = (os.environ.get("DEEPSEEK_BASE_URL")
                or os.environ.get("OPENROUTER_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LANGCHAIN_MODEL_NAME", "deepseek-v4-pro").strip()
    if not api_key:
        return None

    # Build context: run metadata + full_report. Cap to ~12K chars input.
    user_msg = (
        f"preset: {getattr(run, 'preset_name', 'investment_committee')}\n"
        f"user_vars: {json.dumps(getattr(run, 'user_vars', {}) or {}, ensure_ascii=False)}\n"
        f"tokens: in={getattr(run, 'total_input_tokens', 0)} "
        f"out={getattr(run, 'total_output_tokens', 0)}\n\n"
        f"--- 原始报告 ---\n{full_report[:12000]}"
    )

    last_err: str = ""
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=60, write=15, pool=5),
            ) as c:
                r = await c.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        "response_format": {"type": "json_object"},
                        "max_tokens": 4000,
                        # Slightly bump temperature on retries to break determinism.
                        "temperature": 0.1 + 0.1 * (attempt - 1),
                    },
                )
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    print(f"[summarizer] attempt {attempt} {last_err}", flush=True)
                    continue
                d = r.json()
                msg = d["choices"][0]["message"]
                content = msg.get("content") or msg.get("reasoning_content") or ""
                if not content:
                    last_err = "empty content+reasoning"
                    print(f"[summarizer] attempt {attempt}: {last_err}", flush=True)
                    continue
                m = re.search(r"\{[\s\S]*\}", content)
                if not m:
                    last_err = "no JSON object found in response"
                    print(f"[summarizer] attempt {attempt}: {last_err}", flush=True)
                    continue
                try:
                    parsed = json.loads(m.group(0))
                    # Ensure template field is set even if LLM missed it
                    parsed.setdefault("template", template)
                    return parsed
                except json.JSONDecodeError as je:
                    last_err = f"JSONDecodeError: {je}"
                    print(f"[summarizer] attempt {attempt}: {last_err}", flush=True)
                    continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[summarizer] attempt {attempt} exception: {last_err}", flush=True)
            continue
    print(f"[summarizer] all 3 attempts failed (last: {last_err})", flush=True)
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
    """Render a structured summary dict into a Feishu Interactive Card.

    Schema is template-agnostic — driven by `kv_fields`, `sections`,
    `actions_or_catalysts` arrays so we don't hardcode per-template labels.
    """
    title = summary.get("title") or "swarm 分析报告"
    badge = summary.get("badge") or "中性"
    color = (_BADGE_COLOR_MAP.get(summary.get("badge_color") or "")
             or _DECISION_DEFAULT_COLOR.get(badge, "blue"))

    # Top kv fields (decision/price/horizon... or stance/timeframe... depending on template)
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
    headline = summary.get("headline") or summary.get("decision_summary")  # back-compat
    if headline:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📌 **{headline}**"},
        })
    if top_fields:
        elements.append({"tag": "div", "fields": top_fields})
    elements.append({"tag": "hr"})

    tldr = summary.get("tldr")
    if tldr:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**📝 综述**\n{tldr}"},
        })
        elements.append({"tag": "hr"})

    # Three bullet sections — labels from the template (multi/macro/research differ)
    for sec in (summary.get("sections") or [])[:3]:
        if not isinstance(sec, dict):
            continue
        label = str(sec.get("label", "")).strip() or "·"
        items = sec.get("items") or []
        body = "\n".join(f"• {x}" for x in items[:6]) if items else "_(未提及)_"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{label}**\n{body}"},
        })

    elements.append({"tag": "hr"})
    metrics = summary.get("key_metrics") or {}
    if isinstance(metrics, dict) and metrics:
        elements.append(_kv_block("📊 关键指标", metrics))
    aoc = summary.get("actions_or_catalysts") or {}
    if isinstance(aoc, dict) and aoc.get("items"):
        elements.append(_bullet_block(aoc.get("label", "📋 后续"),
                                      aoc.get("items") or []))

    # 游资观点 (multi-guru) — LLM 自选 1-2 位互补游资,仅 stock_decision preset.
    views = summary.get("youzi_views") or []
    if views:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**🐊 游资观点 · LLM 自选 {len(views)} 位**"},
        })
        for v in views:
            if not isinstance(v, dict) or not v.get("text"):
                continue
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**{v.get('display_name','游资')} · {v.get('school','')}**\n{v['text']}"},
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
    """Create an empty docx in the bot's own drive. Returns (document_id, url)."""
    try:
        token = _feishu_get_tenant_token()
        with httpx.Client(timeout=15) as c:
            r = c.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"title": title[:200]},
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
    swarm_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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


# Webhook
async def feishu_events(request: Request):
    """Feishu event webhook. Handles:
       - URL verification (type=url_verification, returns the challenge)
       - im.message.receive_v1 (schema 2.0)
    """
    if not FEISHU_ENABLED:
        return JSONResponse({"error": "feishu integration not configured (set LARK_APP_ID/SECRET)"}, status_code=503)

    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Legacy URL verification (schema v1): {"type": "url_verification", "token": "...", "challenge": "..."}
    if body.get("type") == "url_verification":
        if FEISHU_VERIFICATION_TOKEN and body.get("token") != FEISHU_VERIFICATION_TOKEN:
            return JSONResponse({"error": "bad token"}, status_code=403)
        return JSONResponse({"challenge": body.get("challenge")})

    # Schema v2: {"schema":"2.0","header":{...,"event_type":"..."}, "event":{...}}
    if body.get("schema") == "2.0":
        header = body.get("header") or {}
        if FEISHU_VERIFICATION_TOKEN and header.get("token") != FEISHU_VERIFICATION_TOKEN:
            return JSONResponse({"error": "bad token"}, status_code=403)
        # Dedup by event_id — Feishu retries deliver the same event_id, so a
        # second call here is a duplicate we must drop before firing the handler.
        event_id = header.get("event_id", "")
        if _is_duplicate_feishu_event(event_id):
            print(f"[feishu] dedup: dropping duplicate event_id={event_id}", flush=True)
            return JSONResponse({"code": 0})
        event_type = header.get("event_type", "")
        if event_type == "im.message.receive_v1":
            asyncio.create_task(_feishu_handle_message(body))
        # Always 200 quickly so Feishu doesn't retry. Real work happens async.
        return JSONResponse({"code": 0})

    return JSONResponse({"code": 0})


HELP_TEXT = (
    "👋 vibe-trading bot 用法\n\n"
    "🗣️ **直接说人话**——bot 会用 LLM 理解你的意图,不用记格式。例如:\n"
    "  • 分析苹果 / 看下英伟达 / 茅台怎么样\n"
    "  • 帮我做小米的风险评估\n"
    "  • 英伟达最近技术面\n"
    "  • 茅台季报数据\n"
    "  • BTC 链上活跃度\n"
    "  • 半导体板块如何\n"
    "  • 对比 AAPL 和 MSFT(自动识别为 pairs 配对)\n"
    "  • SPY 期权策略\n\n"
    "📊 **历史报告查询**:\n"
    "  • 最近跑过哪些 / list_runs           列最近 10 个\n"
    "  • 失败的 run / list_runs failed      按状态过滤\n"
    "  • 当前在跑的 / list_runs running     看进行中的\n"
    "  • 最近 5 个 / list_runs 5            限制数量\n"
    "  • 查一下 <run_id>                    拉完整报告\n"
    "  • 把最新报告发我 / status latest     最近一次 completed\n\n"
    "🔧 **运维**:\n"
    "  • 取消 <run_id> / cancel <run_id>     杀掉卡死的 run\n"
    "  • 把当前在跑的干掉 / cancel latest    杀最新一个\n\n"
    "📋 **系统**:\n"
    "  • presets / 有哪些 preset             列出全部 28 个 preset\n"
    "  • help / 怎么用                       这条帮助\n\n"
    "💡 显式指定 preset(高优先级,绕过 LLM):\n"
    "  • preset:technical_analysis_panel SOXL"
)


def _resolve_latest_run_id(filter_status: str | None = "completed") -> str | None:
    """Return the most recent run_id matching filter_status. None if no match."""
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
    try:
        store = SwarmStore(base_dir=swarm_dir)
        runs = store.list_runs() or []
    except Exception:
        return None
    if filter_status:
        runs = [r for r in runs if r.status.value == filter_status]
    if not runs:
        return None
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs[0].id


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
            return
        # Capture sender's open_id so we can share generated docs with them.
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        sender_open_id = sender_id.get("open_id") or ""
        chat_type = msg.get("chat_type") or ""  # 'p2p' (DM) or 'group'
        if msg.get("message_type") != "text":
            _feishu_send_text(chat_id, "chat_id",
                              "目前只支持文本消息。发 help 看用法。")
            return
        try:
            content = json.loads(msg.get("content") or "{}")
        except Exception:
            content = {}
        raw_text = content.get("text", "") or ""
        text = _strip_mentions(raw_text)
        if not text:
            _feishu_send_text(chat_id, "chat_id", HELP_TEXT)
            return

        # ─── routing ───
        # Highest priority: explicit `preset:xxx <args>` override (deterministic,
        # zero-latency, doesn't burn LLM tokens for power users).
        explicit_preset, cleaned_text = _parse_explicit_preset(text)
        if explicit_preset:
            target, market = _extract_target(cleaned_text)
            await _fire_swarm(chat_id, explicit_preset, target, market, cleaned_text,
                              sender_open_id=sender_open_id, chat_type=chat_type)
            return

        # Primary path: LLM router. Handles all 8 actions including system commands.
        llm_result = await _llm_route(text)

        if llm_result is not None:
            action = llm_result.get("action")
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
                )
            elif action == "status":
                run_id = llm_result.get("run_id") or "latest"
                if run_id == "latest":
                    resolved = _resolve_latest_run_id("completed") or _resolve_latest_run_id(None)
                    if not resolved:
                        _feishu_send_text(chat_id, "chat_id", "没有 run 记录。")
                        return
                    run_id = resolved
                await _feishu_handle_status(chat_id, run_id)
            elif action == "cancel_run":
                run_id = llm_result.get("run_id") or "latest"
                if run_id == "latest":
                    resolved = _resolve_latest_run_id("running") or _resolve_latest_run_id(None)
                    if not resolved:
                        _feishu_send_text(chat_id, "chat_id", "没有 run 可以 cancel。")
                        return
                    run_id = resolved
                await _feishu_handle_cancel_run(chat_id, run_id)
            elif action == "help":
                _feishu_send_text(chat_id, "chat_id", HELP_TEXT)
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
        target, market = _extract_target(text)
        if not target:
            _feishu_send_text(
                chat_id, "chat_id",
                "没识别出来,试试 SOXL / 1810.HK / 605117 / BTC,或发 help。",
            )
            return
        fallback_preset = _classify_preset(text, FEISHU_DEFAULT_PRESET)
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
    swarm_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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


async def _feishu_handle_cancel_run(chat_id: str, run_id: str) -> None:
    """Kill a stuck/unwanted run. Mirrors /_debug/purge-run logic."""
    import ctypes, pathlib, shutil
    from src.swarm.store import SwarmStore
    swarm_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"

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
    swarm_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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
                                    limit: int = 10) -> None:
    from src.swarm.store import SwarmStore
    import pathlib
    swarm_dir = pathlib.Path(mcp_server.__file__).resolve().parent / ".swarm" / "runs"
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

    if limit < 1:
        limit = 10
    runs = runs[:limit]

    if not runs:
        scope = f" (status={status_filter})" if status_filter else ""
        _feishu_send_text(chat_id, "chat_id", f"暂无 run 记录{scope}。")
        return

    header = f"最近 {len(runs)} 个 run" + (f" (status={status_filter})" if status_filter else "") + ":"
    lines = [header]
    for r in runs:
        # tokens may be 0/0 while running — present cleanly
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
async def _lifespan(app):
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
app = Starlette(
    routes=[
        Route("/", root),
        Route("/healthz", healthz),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize_get, methods=["GET"]),
        Route("/authorize", authorize_post, methods=["POST"]),
        Route("/token", token_endpoint, methods=["POST"]),
        Route("/feishu/events", feishu_events, methods=["POST"]),
        Route("/_debug/threads", debug_threads),
        Route("/_debug/swarm-state", debug_swarm_state),
        Route("/_debug/purge-run", debug_purge_run),
        Route("/_debug/env", debug_env),
        Route("/_debug/list-feishu-chats", debug_list_feishu_chats),
        Route("/_debug/republish", debug_republish, methods=["POST"]),
        Mount("/", app=mcp_app),
    ],
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
