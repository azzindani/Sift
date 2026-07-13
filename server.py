"""FastMCP wrapper. Tool bodies are one line each — all logic lives in engine.py.

Nothing here may touch domain data. If a tool body grows a second line of logic, it
belongs in the engine.

The HTTP surface deliberately mirrors Folio's, so one client config style reaches both:

    POST /mcp             JSON-RPC — tools/list, tools/call, initialize   [Bearer]
    GET  /health          liveness + toolchain                            [public]
    GET  /version         running version                                 [public]
    GET  /tokens/whoami   which named token you are                       [Bearer]
    GET  /clips/{b}/{f}   published clip, thumbnail, manifest, gallery    [Bearer]
    GET  /library/*       browse the library  [Bearer | ?token= | session cookie]
    *    /oauth/*         OAuth 2.0 + PKCE for claude.ai's connector      [public]
    GET  /.well-known/oauth-*   RFC 8414 / RFC 9728 discovery             [public]

The OAuth surface is not a second auth system — it is a bridge to the same one. The
key pasted at /oauth/authorize is the same tokens.json entry a raw Bearer uses, and
both resolve to the same named principal. One key, every platform. See shared/oauth.py.

stdout is the MCP channel on stdio transport, so a single stray ``print`` corrupts the
protocol stream. Logging is pinned to stderr here, once, for the whole process.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from functools import partial
from typing import Any

import anyio
from fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.types import ASGIApp, Receive, Scope, Send

import engine
from _clip_helpers import projects_dir, serve_dir
from shared import browse, oauth
from shared.auth import (
    OPEN_PRINCIPAL,
    AuthConfigError,
    authorize,
    describe_auth,
    load_tokens,
    rate_limiter_from_env,
)
from shared.file_utils import PathError, resolve_path
from shared.oauth import OAUTH_PATH_PREFIXES
from shared.platform_utils import check_toolchain

VERSION = "0.1.0"

logging.basicConfig(
    stream=sys.stderr,  # never stdout: it is the MCP channel
    level=os.environ.get("SIFT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sift.server")

mcp = FastMCP(
    name="sift",
    version=VERSION,
    instructions=(
        "Turns long-form video into short vertical clips. You are the intelligence: read the "
        "transcript in overlapping chunks, judge what is clip-worthy, label it, and orchestrate. "
        "The server only executes.\n\n"
        "Pipeline: fetch_source -> read_transcript_chunk (all chunks) -> add_candidates -> "
        "plan_clips -> render_clip (async) -> get_job (poll) -> publish_outputs.\n\n"
        "Work is organised into projects (a file-backed library). Pass project='name' to "
        "fetch_source; browse with list_library().\n\n"
        "Labels: quote joke story argument hot_take reaction. Read skills/<label>.md before "
        "choosing boundaries for that label. Every clip must pass the cold-open test: its first "
        "sentence must make sense with zero prior context."
    ),
)

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
NETWORK = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
IDEMPOTENT_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


async def offload(fn: Any, *args: Any) -> dict:
    """Run a blocking engine call on a worker thread. Never on the event loop.

    FastMCP invokes a *sync* tool function directly inside the event loop
    (``FunctionTool.run`` → ``type_adapter.validate_python``, with no thread offload). Every
    tool here does blocking I/O — subprocesses, file reads, SQLite — and ``fetch_source``
    can sit in a yt-dlp probe for fifteen seconds. Run that on the loop and the whole
    server freezes: health checks stall, in-flight SSE streams stall, and every other
    client blocks behind one caller. On a shared endpoint that is not a slow tool, it is an
    outage.

    So every tool body is async and hands the work to a thread. The loop stays free to
    serve everyone else while one caller's ffmpeg grinds.
    """
    return await anyio.to_thread.run_sync(partial(fn, *args))


# --------------------------------------------------------------------------
# Tier 1 — read / inspect
# --------------------------------------------------------------------------


@mcp.tool(annotations=NETWORK)
async def fetch_source(
    url: str, max_height: int = 720, cookies_path: str = "", project: str = "default"
) -> dict:
    """Fetch video + transcript by URL into a project. Returns metadata."""
    return await offload(engine.fetch_source, url, max_height, cookies_path, project)


@mcp.tool(annotations=READ_ONLY)
async def read_transcript_chunk(source_id: str, index: int) -> dict:
    """Read one overlapping transcript window. Bounded, with timing."""
    return await offload(engine.read_transcript_chunk, source_id, index)


@mcp.tool(annotations=READ_ONLY)
async def sample_frames(source_id: str, start: float, end: float, fps: float = 1.0) -> dict:
    """Sample capped, downscaled frames in a span for vision review."""
    return await offload(engine.sample_frames, source_id, start, end, fps)


@mcp.tool(annotations=READ_ONLY)
async def get_job(job_id: str) -> dict:
    """Read render job status, progress, and output path."""
    return await offload(engine.get_job, job_id)


@mcp.tool(annotations=READ_ONLY)
async def list_library(project: str = "") -> dict:
    """List projects, or one project's sources, clips, and exports."""
    return await offload(engine.list_library, project)


# --------------------------------------------------------------------------
# Tier 2 — structured
# --------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def add_candidates(source_id: str, candidates: list[dict]) -> dict:
    """Persist agent-selected clip candidates. Dedups overlaps."""
    return await offload(engine.add_candidates, source_id, candidates)


@mcp.tool(annotations=WRITE)
async def plan_clips(source_id: str, mode: str = "auto") -> dict:
    """Group candidates into clips: auto by_label by_topic montage supercut."""
    return await offload(engine.plan_clips, source_id, mode)


# --------------------------------------------------------------------------
# Tier 3 — render / export
# --------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def render_clip(clip_id: str, reframe: str = "speaker", captions: bool = True) -> dict:
    """Enqueue render job: trim, reframe, caption. reframe: speaker center stacked"""
    return await offload(engine.render_clip, clip_id, reframe, captions)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def publish_outputs(job_ids: list[str], ttl_hours: int = 168) -> dict:
    """Move clips to served dir. Returns links + verifiable summary."""
    return await offload(engine.publish_outputs, job_ids, ttl_hours)


# --------------------------------------------------------------------------
# HTTP surface — auth, rate limit, static delivery
# --------------------------------------------------------------------------

PUBLIC_PATHS = frozenset({"/health", "/version"})

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
}

_limiter = rate_limiter_from_env()


def _base_url(scope: Scope) -> str:
    """The public origin, as the *client* sees it.

    Behind Caddy the app listens on plain HTTP, so deriving this from the socket would
    advertise ``http://sift:8765`` in the OAuth metadata — and claude.ai would then send
    the browser to an unreachable internal host. The forwarded headers are the only
    truth here, which is why the Caddy block sets X-Forwarded-Proto / -Host explicitly.
    """
    headers = Headers(scope=scope)
    proto = headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = headers.get("x-forwarded-host", "") or headers.get("host", "")
    if not proto:
        proto = "https" if scope.get("scheme") in ("https", "wss") else "http"
    return f"{proto}://{host or 'localhost'}"


class AuthMiddleware:
    """Bearer auth + per-(token, IP) rate limiting. Everything but /health and /version.

    Written as **pure ASGI**, not Starlette's ``BaseHTTPMiddleware``, and that is not a
    style choice. BaseHTTPMiddleware pumps the response through an anyio memory stream,
    which buffers it — and MCP's Streamable HTTP transport answers with SSE. Short calls
    survive; a long one (a 2-minute `fetch_source`) deadlocks: the server returns 200 and
    the client hangs forever waiting for a body that is stuck in the middleware.

    A pure ASGI middleware passes ``send`` straight through, so the stream flows.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # The OAuth handshake IS the pre-auth path: claude.ai walks discovery,
        # registration and PKCE anonymously, before it holds any token at all. Gating
        # these behind the bearer would make the connector impossible to add.
        #
        # /library is exempt for a different reason: it is reached by a BROWSER, which
        # cannot send an Authorization header. It carries its own gate (?token= → session
        # cookie), so the route is the sole authority — the same shape as Folio's
        # static-server. Exempt from the middleware, never from authentication.
        if (
            path in PUBLIC_PATHS
            or path.startswith(OAUTH_PATH_PREFIXES)
            or path == "/library"
            or path.startswith("/library/")
            or scope.get("method") == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        principal = authorize(headers.get("authorization"))
        if principal is None:
            # RFC 9728: point the client at the resource metadata so an MCP client that
            # does not yet have a token can discover *where* to get one. This header is
            # how claude.ai finds the OAuth surface from a bare 401.
            resource = f"{_base_url(scope)}/.well-known/oauth-protected-resource"
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "hint": "Send 'Authorization: Bearer <token>'. Tokens come from "
                    "SIFT_TOKENS_FILE / SIFT_TOKENS / SIFT_API_KEY, or complete the "
                    "OAuth flow at /oauth/authorize.",
                },
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource}"'},
            )
            await response(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else "unknown"
        if not _limiter.allow(principal, ip):
            response = JSONResponse(
                {
                    "error": "rate limited",
                    "hint": f"Over {_limiter.burst} burst / {_limiter.per_sec}/s. Back off, or "
                    "raise SIFT_RATE_BURST / SIFT_RATE_PER_SEC (0 disables).",
                },
                status_code=429,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        scope["principal"] = principal
        if path.rstrip("/").endswith("/mcp"):
            log.info("[mcp] token=%s path=%s method=%s", principal, path, scope.get("method"))
        await self.app(scope, receive, send)


def _egress() -> dict[str, Any]:
    """Can this host actually reach a video source over IPv4?

    Worth a line in /health because the failure is otherwise invisible and baffling:
    Docker's embedded DNS can hand back an AAAA record and drop the A record, while the
    bridge network has no IPv6 route — so yt-dlp fails with "Network is unreachable" on a
    URL that works perfectly from the host. Surfacing the resolved families here turns a
    twenty-minute debug into a glance.
    """
    import socket  # noqa: PLC0415 - only the health route needs it

    try:
        infos = socket.getaddrinfo("www.youtube.com", 443, proto=socket.IPPROTO_TCP)
        families = sorted({"ipv4" if i[0] == socket.AF_INET else "ipv6" for i in infos})
        return {
            "dns": "ok",
            "families": families,
            "ipv4": "ipv4" in families,
        }
    except OSError as exc:
        return {"dns": f"FAILED — {exc}", "families": [], "ipv4": False}


@mcp.custom_route("/health", methods=["GET"])
async def healthz(_: Request) -> Response:
    """Liveness — public. Reports the toolchain and egress the pipeline depends on."""
    toolchain = check_toolchain()
    egress = _egress()
    healthy = not any(v.startswith("MISSING") for v in toolchain.values()) and egress["ipv4"]
    return JSONResponse(
        {
            "ok": healthy,
            "version": VERSION,
            "auth": describe_auth(),
            "toolchain": toolchain,
            "egress": egress,
            **engine.capabilities(),
        },
        status_code=200 if healthy else 503,
    )


@mcp.custom_route("/version", methods=["GET"])
async def version(_: Request) -> Response:
    """Running version — public, so a monitor can alert on a stale deploy without a token."""
    return JSONResponse({"name": "sift", "version": VERSION})


@mcp.custom_route("/tokens/whoami", methods=["GET"])
async def whoami(request: Request) -> Response:
    """Which named token you presented. The cheapest possible auth sanity check."""
    principal = request.scope.get("principal", OPEN_PRINCIPAL)
    return JSONResponse({"token": principal, "authenticated": principal != OPEN_PRINCIPAL})


# ── OAuth 2.0 + PKCE ─────────────────────────────────────────────────────────────
# All public: this is the handshake a client runs BEFORE it holds a token. claude.ai's
# Custom Connector will not accept a raw bearer, so without these Sift simply cannot be
# added to claude.ai. The key you paste at /oauth/authorize is the SAME tokens.json key
# you use from Claude Code or curl — see shared/oauth.py.

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


async def _capped_body(request: Request) -> bytes:
    """Read a pre-auth body under a hard cap — an anon POST must not OOM the box."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > oauth.MAX_BODY_BYTES:
            raise ValueError("body too large")
    return body


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
async def oauth_metadata(request: Request) -> Response:
    """RFC 8414 — where the authorize and token endpoints live."""
    return JSONResponse(oauth.metadata(_base_url(request.scope)), headers=_CORS)


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
async def oauth_resource(request: Request) -> Response:
    """RFC 9728 — what the 401's WWW-Authenticate header points at."""
    return JSONResponse(oauth.protected_resource(_base_url(request.scope)), headers=_CORS)


@mcp.custom_route("/oauth/register", methods=["POST", "OPTIONS"])
async def oauth_register(request: Request) -> Response:
    """RFC 7591 dynamic client registration — claude.ai registers itself here."""
    try:
        body = await _capped_body(request)
    except ValueError:
        return JSONResponse({"error": "invalid_request"}, status_code=413, headers=_CORS)
    status, payload = oauth.register(body)
    return JSONResponse(payload, status_code=status, headers=_CORS)


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize_get(request: Request) -> Response:
    """The login form. The one human step: paste your Sift key."""
    status, page = oauth.authorize_form(dict(request.query_params))
    return HTMLResponse(page, status_code=status, headers=_CORS)


@mcp.custom_route("/oauth/authorize", methods=["POST", "OPTIONS"])
async def oauth_authorize_post(request: Request) -> Response:
    """Validate the pasted key, mint a one-shot code, bounce back to the client."""
    try:
        body = await _capped_body(request)
    except ValueError:
        return HTMLResponse("too large", status_code=413, headers=_CORS)
    status, page, location = oauth.authorize_submit(body)
    if status == 302:
        return RedirectResponse(location, status_code=302, headers=_CORS)
    return HTMLResponse(page, status_code=status, headers=_CORS)


@mcp.custom_route("/oauth/token", methods=["POST", "OPTIONS"])
async def oauth_token(request: Request) -> Response:
    """Exchange the code (PKCE-verified) for an access token, or rotate a refresh token."""
    try:
        body = await _capped_body(request)
    except ValueError:
        return JSONResponse({"error": "invalid_request"}, status_code=413, headers=_CORS)
    status, payload = oauth.token(body)
    return JSONResponse(payload, status_code=status, headers=_CORS)


# ── The library, browsable ───────────────────────────────────────────────────────
# Same key as everything else. Folio dropped basic_auth from its equivalent route for
# exactly the reason its comment gives — a browser username/password popup "could never
# be satisfied by an access token anyway" — and handing someone a SECOND credential to
# look at their own library defeats the point of one key everywhere.
#
# ?token=sk-sift-… once → HttpOnly session cookie → 30 days. Bearer works too, for scripts.
# The cookie carries a minted session token, never the API key.


def _library_principal(request: Request) -> str | None:
    """Bearer, or the session cookie. (The ?token= hand-off is handled by the route.)"""
    principal = authorize(request.headers.get("authorization"))
    if principal:
        return principal
    cookie = request.cookies.get(oauth.SESSION_COOKIE, "")
    return authorize(f"Bearer {cookie}") if cookie else None


@mcp.custom_route("/library", methods=["GET"])
@mcp.custom_route("/library/{path:path}", methods=["GET"])
async def library(request: Request) -> Response:
    """Browse the durable record: projects, transcripts, candidates, clips, manifests."""
    rel = request.path_params.get("path", "")
    url_path = "/library/" + rel

    # Hand-off: a valid ?token= is swapped for a cookie and the token is dropped from the
    # URL, so it does not linger in history, logs, or a shared screenshot.
    presented = request.query_params.get("token", "")
    if presented:
        principal = authorize(f"Bearer {presented}")
        if not principal:
            return HTMLResponse(browse.gate_page(url_path), status_code=401)
        response = RedirectResponse(url_path, status_code=302)
        response.set_cookie(
            oauth.SESSION_COOKIE,
            oauth.mint_session(principal),
            max_age=oauth.SESSION_TTL_S,
            httponly=True,
            secure=request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https",
            samesite="lax",
            path="/library",
        )
        return response

    if _library_principal(request) is None:
        # A plain 401 with no WWW-Authenticate: a browser popup cannot carry a bearer.
        return HTMLResponse(browse.gate_page(url_path), status_code=401)

    root = projects_dir()
    target = browse.resolve(root, rel)
    if target is None:
        return HTMLResponse(browse.render(url_path, [], "Not found."), status_code=404)

    if target.is_dir():
        if rel and not request.url.path.endswith("/"):
            return RedirectResponse(url_path + "/", status_code=302)
        hint = "Empty. Nothing has been fetched into this project yet."
        return HTMLResponse(browse.render(url_path, browse.listing(target, url_path), hint))

    media = browse.INLINE_TYPES.get(target.suffix.lower())
    if media is None:
        return HTMLResponse(browse.render(url_path, [], "Not a readable file."), status_code=404)
    return FileResponse(target, media_type=media)


@mcp.custom_route("/clips/{batch_id}/{filename}", methods=["GET", "HEAD"])
async def serve_clip(request: Request) -> Response:
    """Serve a published artifact. Unguessable paths, no directory listing, Bearer-gated."""
    batch_id = request.path_params["batch_id"]
    filename = request.path_params["filename"]
    try:
        root = serve_dir().resolve()
        target = resolve_path(root / batch_id / filename)
        target.relative_to(root)  # no traversal out of the served root
    except (PathError, ValueError):
        return JSONResponse({"error": "not found"}, status_code=404)

    if not target.is_file() or target.suffix.lower() not in _MEDIA_TYPES:
        return JSONResponse({"error": "not found"}, status_code=404)

    return FileResponse(target, media_type=_MEDIA_TYPES[target.suffix.lower()])


def build_app():  # noqa: ANN201 - Starlette app, typed by FastMCP
    """The ASGI app: MCP at /mcp, wrapped in auth + rate limiting."""
    return mcp.http_app(path="/mcp", middleware=[Middleware(AuthMiddleware)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Sift MCP server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "http"])
    parser.add_argument("--host", default=os.environ.get("SIFT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIFT_PORT", "8765")))
    args = parser.parse_args()

    # Resolve auth BEFORE anything binds a socket. A configured-but-unusable token
    # source is a hard stop, never a silent downgrade to open — see shared/auth.py.
    try:
        load_tokens(force=True)
    except AuthConfigError as exc:
        log.error("auth misconfigured: %s", exc)
        raise SystemExit(2) from None

    state = engine.startup()
    log.info(
        "sift %s — transport=%s reconciled=%d indexed=%d",
        VERSION,
        args.transport,
        state["reconciled_jobs"],
        state["indexed_entities"],
    )
    for name, path in check_toolchain().items():
        if path.startswith("MISSING"):
            log.warning("%s: %s", name, path)

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)  # a banner would corrupt the stream
        return

    log.info("auth: %s", describe_auth())
    if describe_auth().startswith("UNAUTHENTICATED"):
        log.warning(
            "HTTP transport with NO auth — bind to localhost only, or set SIFT_API_KEY / "
            "SIFT_TOKENS / SIFT_TOKENS_FILE before exposing this."
        )
    if _limiter.enabled:
        log.info("rate limit: %d burst, %g/s per (token, ip)", _limiter.burst, _limiter.per_sec)

    import uvicorn  # noqa: PLC0415 - only the HTTP path needs the server

    uvicorn.run(build_app(), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
