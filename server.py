"""FastMCP wrapper. Tool bodies are one line each — all logic lives in engine.py.

Nothing here may touch domain data. If a tool body grows a second line of logic, it
belongs in the engine.

The HTTP surface deliberately mirrors Folio's, so one client config style reaches both:

    POST /mcp             JSON-RPC — tools/list, tools/call, initialize   [Bearer]
    GET  /health          liveness + toolchain                            [public]
    GET  /version         running version                                 [public]
    GET  /tokens/whoami   which named token you are                       [Bearer]
    GET  /clips/{b}/{f}   published clip, thumbnail, manifest, gallery    [Bearer]

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
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

import engine
from _clip_helpers import serve_dir
from shared.auth import OPEN_PRINCIPAL, authorize, describe_auth, rate_limiter_from_env
from shared.file_utils import PathError, resolve_path
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
        if path in PUBLIC_PATHS or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        principal = authorize(headers.get("authorization"))
        if principal is None:
            # No WWW-Authenticate header: a browser popup cannot satisfy a bearer token,
            # so prompting for one would only confuse. Say what is missing instead.
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "hint": "Send 'Authorization: Bearer <token>'. Tokens come from "
                    "SIFT_TOKENS_FILE / SIFT_TOKENS / SIFT_API_KEY.",
                },
                status_code=401,
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


@mcp.custom_route("/health", methods=["GET"])
async def healthz(_: Request) -> Response:
    """Liveness — public. Reports the toolchain the render path depends on."""
    toolchain = check_toolchain()
    healthy = not any(v.startswith("MISSING") for v in toolchain.values())
    return JSONResponse(
        {
            "ok": healthy,
            "version": VERSION,
            "auth": describe_auth(),
            "toolchain": toolchain,
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
