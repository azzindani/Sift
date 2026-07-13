"""End-to-end tests of the HTTP surface — the real ASGI app, real middleware.

These drive the same endpoints an agent or a `curl` would hit, so the auth gate is
tested where it actually runs rather than as a unit-tested function that the server
might have forgotten to wire in.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from shared.auth import reset_for_tests


@pytest.fixture
def client(monkeypatch):
    """The app with two named tokens, exactly as a compose deployment would run it."""
    monkeypatch.setenv("SIFT_TOKENS", "claude:sk-test-claude,hermes:sk-test-hermes")
    reset_for_tests()

    import server

    with TestClient(server.build_app()) as test_client:
        yield test_client
    reset_for_tests()


AUTH = {"Authorization": "Bearer sk-test-claude"}


def test_health_is_public(client):
    """A monitor must be able to alert on a dead deploy without holding a token."""
    response = client.get("/health")
    assert response.status_code in (200, 503)

    body = response.json()
    assert "toolchain" in body and "labels" in body
    assert body["version"]
    assert "named token" in body["auth"]  # reports the mode, never the secrets

    assert "sk-test-claude" not in response.text  # tokens never leak into a public endpoint


def test_version_is_public(client):
    body = client.get("/version").json()
    assert body["name"] == "sift"
    assert body["version"]


def test_mcp_requires_a_bearer_token(client):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert "Bearer" in response.json()["hint"]
    # No WWW-Authenticate: a browser password prompt cannot satisfy a bearer token.
    assert "www-authenticate" not in {k.lower() for k in response.headers}


def test_mcp_rejects_a_wrong_token(client):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer sk-not-a-real-token"},
    )
    assert response.status_code == 401


def test_whoami_names_the_token(client):
    body = client.get("/tokens/whoami", headers=AUTH).json()
    assert body == {"token": "claude", "authenticated": True}

    other = client.get("/tokens/whoami", headers={"Authorization": "Bearer sk-test-hermes"}).json()
    assert other["token"] == "hermes"  # the audit trail can tell them apart


def test_tools_list_over_http_returns_the_nine_tools(client):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers={**AUTH, "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    session = response.headers.get("mcp-session-id")

    headers = {**AUTH, "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session

    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=headers,
    )
    assert listed.status_code == 200

    payload = _parse(listed.text)
    names = {tool["name"] for tool in payload["result"]["tools"]}
    assert names == {
        "fetch_source",
        "read_transcript_chunk",
        "sample_frames",
        "get_job",
        "list_library",
        "add_candidates",
        "plan_clips",
        "render_clip",
        "publish_outputs",
    }


def test_auth_middleware_is_pure_asgi_not_basehttpmiddleware():
    """A regression guard, and the reason is worth stating.

    Starlette's BaseHTTPMiddleware pumps the response through an anyio memory stream,
    which buffers it. MCP's Streamable HTTP transport answers with SSE. Short calls
    survive the buffering; a long one does not — a 2-minute `fetch_source` returned 200
    on the server while the client hung forever waiting for a body stuck in the
    middleware. Pure ASGI passes `send` straight through, so the stream flows.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    import server

    assert not issubclass(server.AuthMiddleware, BaseHTTPMiddleware)


def test_a_real_tool_call_round_trips_over_http(client):
    """The body must actually arrive — not just the status line."""
    headers = {**AUTH, "Accept": "application/json, text/event-stream"}
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers=headers,
    )
    if session := init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = session
    client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )

    called = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_library", "arguments": {}},
        },
        headers=headers,
    )
    assert called.status_code == 200

    payload = _parse(called.text)
    body = json.loads(payload["result"]["content"][0]["text"])
    assert body["success"] is True
    assert body["op"] == "list_library"
    assert "projects" in body
    assert body["token_estimate"] > 0  # the response contract survives the transport


def test_every_tool_is_async_so_a_slow_call_cannot_freeze_the_server():
    """A blocking tool body would take the whole endpoint down, not just one call.

    FastMCP invokes a *sync* tool function directly on the event loop — no thread offload.
    Every tool here does blocking I/O (subprocess, file, SQLite) and `fetch_source` can sit
    in a yt-dlp probe for fifteen seconds. On the loop, that stalls health checks, in-flight
    SSE streams, and every other client. On a shared endpoint that is an outage, not a slow
    call. So each tool body must be `async def` and hand its work to a thread.
    """
    import inspect

    import server

    tools = [
        server.fetch_source,
        server.read_transcript_chunk,
        server.sample_frames,
        server.get_job,
        server.list_library,
        server.add_candidates,
        server.plan_clips,
        server.render_clip,
        server.publish_outputs,
    ]
    for tool in tools:
        fn = getattr(tool, "fn", tool)
        assert inspect.iscoroutinefunction(fn), (
            f"{getattr(fn, '__name__', tool)} is sync — it would block the event loop"
        )


@pytest.mark.timeout(30)
def test_the_event_loop_stays_responsive_while_a_tool_blocks(client, monkeypatch):
    """Prove it end to end: hold a tool in a sleep and hit /health at the same time."""
    import threading
    import time

    started = threading.Event()

    def slow_list_library(_project: str = "") -> dict:
        started.set()
        time.sleep(3.0)  # a stand-in for a yt-dlp probe or an ffmpeg call
        return {"success": True, "op": "list_library", "progress": [], "token_estimate": 1}

    monkeypatch.setattr("engine.list_library", slow_list_library)

    headers = {**AUTH, "Accept": "application/json, text/event-stream"}
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        headers=headers,
    )
    if session := init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = session
    client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )

    result: dict = {}

    def call_the_slow_tool() -> None:
        result["response"] = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_library", "arguments": {}},
            },
            headers=headers,
        )

    caller = threading.Thread(target=call_the_slow_tool)
    caller.start()
    assert started.wait(timeout=10), "the tool never started"

    # The tool is mid-sleep right now. /health must still answer — quickly.
    began = time.monotonic()
    health = client.get("/health")
    elapsed = time.monotonic() - began

    assert health.status_code in (200, 503)
    assert elapsed < 2.0, f"/health blocked for {elapsed:.1f}s behind a busy tool"

    caller.join(timeout=20)
    assert result["response"].status_code == 200


def test_clips_route_is_gated_and_refuses_traversal(client):
    assert client.get("/clips/b_x/nope.mp4").status_code == 401  # no token at all

    assert client.get("/clips/b_x/nope.mp4", headers=AUTH).status_code == 404
    assert client.get("/clips/..%2f..%2fetc/passwd", headers=AUTH).status_code == 404
    # A file type we never publish is not servable even if it somehow existed.
    assert client.get("/clips/b_x/secrets.env", headers=AUTH).status_code == 404


def test_rate_limit_returns_429(monkeypatch):
    monkeypatch.setenv("SIFT_API_KEY", "sk-rl")
    monkeypatch.setenv("SIFT_RATE_BURST", "3")
    monkeypatch.setenv("SIFT_RATE_PER_SEC", "1")
    reset_for_tests()

    import importlib

    import server

    importlib.reload(server)  # the limiter is built from env at import
    with TestClient(server.build_app()) as client:
        headers = {"Authorization": "Bearer sk-rl"}
        codes = [client.get("/tokens/whoami", headers=headers).status_code for _ in range(8)]

    assert 429 in codes, f"rate limit never engaged: {codes}"
    assert codes[0] == 200  # the burst is allowed before throttling kicks in

    monkeypatch.delenv("SIFT_RATE_BURST")
    monkeypatch.delenv("SIFT_RATE_PER_SEC")
    reset_for_tests()
    importlib.reload(server)


def _parse(text: str) -> dict:
    """Streamable HTTP replies may arrive as SSE ("data: {...}") or as plain JSON."""
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    return json.loads(text)
