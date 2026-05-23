"""Test harness for mcp_launcher.

mcp_launcher imports container-only deps (httpx, uvicorn, starlette) and the
upstream `mcp_server` package, none of which are installed in CI. To exercise
the pure helper functions against the real source, we install minimal stub
modules into sys.modules *before* importing mcp_launcher, and set the required
MCP_AUTH_TOKEN env var. The stubs only need to satisfy import-time access; the
helpers under test do no network I/O.
"""
import os
import pathlib
import sys
import types

os.environ.setdefault("MCP_AUTH_TOKEN", "test-secret-token-1234567890")

# mcp_launcher.py lives at the repo root; make it importable from tests/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _install(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_stubs() -> None:
    # ── httpx: mcp_launcher monkeypatches Client/AsyncClient __init__ and uses Timeout ──
    if "httpx" not in sys.modules:
        class _Timeout:
            def __init__(self, *a, connect=None, read=None, write=None, pool=None, **k):
                self.connect, self.read, self.write, self.pool = connect, read, write, pool

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _AsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        _install("httpx", Client=_Client, AsyncClient=_AsyncClient, Timeout=_Timeout)

    # ── uvicorn: only uvicorn.run, invoked under __main__ (never at import) ──
    sys.modules.setdefault("uvicorn", _install("uvicorn", run=lambda *a, **k: None))

    # ── starlette: classes referenced at import + module-level app assembly ──
    class _Passthrough:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k

    _install("starlette")
    _install("starlette.applications", Starlette=_Passthrough)
    _install("starlette.middleware", Middleware=_Passthrough)
    _install("starlette.requests", Request=_Passthrough)
    _install("starlette.responses",
             HTMLResponse=_Passthrough, JSONResponse=_Passthrough,
             PlainTextResponse=_Passthrough, RedirectResponse=_Passthrough)
    _install("starlette.routing", Mount=_Passthrough, Route=_Passthrough)
    _install("starlette.types",
             ASGIApp=object, Receive=object, Scope=object, Send=object)

    # ── mcp_server: .mcp.tool() decorator + .http_app(); plus __file__/AGENT_DIR ──
    mcp_obj = types.SimpleNamespace(
        tool=lambda *a, **k: (lambda fn: fn),
        http_app=lambda *a, **k: types.SimpleNamespace(lifespan=lambda app: None),
    )
    ms = _install("mcp_server", mcp=mcp_obj, AGENT_DIR=pathlib.Path("/tmp"))
    ms.__file__ = "/tmp/mcp_server.py"


_install_stubs()
