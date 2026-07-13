"""OAuth 2.0 + PKCE — the surface claude.ai's Custom Connector requires.

The load-bearing property, and the reason this exists: **one key, every platform.** The
same ``tokens.json`` entry must authenticate a direct ``Authorization: Bearer sk-sift-…``
(Claude Code, curl, n8n) *and* a claude.ai connector session, and both must resolve to
the same named principal in the audit log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest
from starlette.testclient import TestClient

import server
from shared import oauth
from shared.auth import authorize, reset_for_tests


@pytest.fixture
def client(monkeypatch, tmp_path):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"claude-web": "sk-sift-real"}), encoding="utf-8")
    monkeypatch.setenv("SIFT_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("SIFT_OAUTH_STATE_DIR", str(tmp_path / "oauth-state"))
    monkeypatch.setenv("SIFT_RATE_BURST", "0")
    reset_for_tests()
    oauth.reset_for_tests()
    with TestClient(server.build_app()) as c:
        yield c
    reset_for_tests()
    oauth.reset_for_tests()


def _pkce() -> tuple[str, str]:
    verifier = "a" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _full_flow(client, api_key: str = "sk-sift-real") -> dict:
    """Walk the exact sequence claude.ai walks: discover → register → authorize → token."""
    meta = client.get("/.well-known/oauth-authorization-server").json()

    reg = client.post(
        meta["registration_endpoint"].replace("http://testserver", ""),
        json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
    )
    client_id = reg.json()["client_id"]

    verifier, challenge = _pkce()
    resp = client.post(
        "/oauth/authorize",
        data={
            "api_key": api_key,
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    if resp.status_code != 302:
        return {"failed": resp.status_code}

    code = re.search(r"code=([^&]+)", resp.headers["location"]).group(1)
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return tok.json() | {"_status": tok.status_code, "_state": resp.headers["location"]}


# ── Discovery: everything before the token must be reachable with no token ──


def test_the_whole_handshake_is_public(client):
    """If any of these 401'd, the connector could never be added in the first place."""
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    assert client.get("/.well-known/oauth-protected-resource").status_code == 200
    assert client.post("/oauth/register", json={}).status_code == 201
    assert (
        client.get(
            "/oauth/authorize",
            params={
                "client_id": "claude-ai",
                "redirect_uri": "https://x/cb",
                "response_type": "code",
            },
        ).status_code
        == 200
    )


def test_metadata_advertises_absolute_urls_from_forwarded_headers(client):
    """Behind Caddy the app sees plain HTTP. It must still advertise the PUBLIC origin."""
    meta = client.get(
        "/.well-known/oauth-authorization-server",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "sift.casava.space"},
    ).json()

    assert meta["issuer"] == "https://sift.casava.space"
    assert meta["authorization_endpoint"] == "https://sift.casava.space/oauth/authorize"
    assert meta["token_endpoint"] == "https://sift.casava.space/oauth/token"
    assert "S256" in meta["code_challenge_methods_supported"]


def test_a_401_points_at_the_resource_metadata(client):
    """RFC 9728 — how a tokenless MCP client discovers where to authenticate."""
    r = client.post(
        "/mcp",
        json={},
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "sift.casava.space"},
    )
    assert r.status_code == 401
    assert (
        r.headers["www-authenticate"]
        == 'Bearer resource_metadata="https://sift.casava.space/.well-known/oauth-protected-resource"'
    )


# ── The point of the whole exercise ──


def test_one_key_authenticates_both_the_bearer_path_and_the_connector(client):
    """The same tokens.json entry, used two ways, is ONE principal."""
    granted = _full_flow(client)
    assert granted["_status"] == 200

    # Path 1 — Claude Code / curl / n8n: the raw key.
    assert authorize("Bearer sk-sift-real") == "claude-web"
    # Path 2 — claude.ai: an opaque OAuth token that maps to the SAME principal.
    assert authorize(f"Bearer {granted['access_token']}") == "claude-web"


def test_the_oauth_token_actually_opens_mcp(client):
    granted = _full_flow(client)
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        },
        headers={
            "Authorization": f"Bearer {granted['access_token']}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 200


def test_whoami_names_the_same_principal_through_oauth(client):
    granted = _full_flow(client)
    r = client.get("/tokens/whoami", headers={"Authorization": f"Bearer {granted['access_token']}"})
    assert r.json() == {"token": "claude-web", "authenticated": True}


def test_state_is_echoed_back_so_the_client_can_correlate(client):
    granted = _full_flow(client)
    assert "state=xyz" in granted["_state"]


# ── The parts that keep it from being a hole ──


def test_a_wrong_api_key_never_gets_a_code(client):
    assert _full_flow(client, api_key="sk-sift-WRONG") == {"failed": 401}


def test_pkce_verifier_must_match(client):
    _, challenge = _pkce()
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://x/cb"]})
    cid = reg.json()["client_id"]
    resp = client.post(
        "/oauth/authorize",
        data={
            "api_key": "sk-sift-real",
            "client_id": cid,
            "redirect_uri": "https://x/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    code = re.search(r"code=([^&]+)", resp.headers["location"]).group(1)

    bad = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://x/cb",
            "client_id": cid,
            "code_verifier": "b" * 64,  # not the verifier the challenge was derived from
        },
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"


def test_an_auth_code_is_one_shot(client):
    """A replayed code must not mint a second token, even with the right verifier."""
    verifier, challenge = _pkce()
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://x/cb"]})
    cid = reg.json()["client_id"]
    resp = client.post(
        "/oauth/authorize",
        data={
            "api_key": "sk-sift-real",
            "client_id": cid,
            "redirect_uri": "https://x/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    code = re.search(r"code=([^&]+)", resp.headers["location"]).group(1)
    exchange = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://x/cb",
        "client_id": cid,
        "code_verifier": verifier,
    }
    assert client.post("/oauth/token", data=exchange).status_code == 200
    replay = client.post("/oauth/token", data=exchange)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_redirect_uri_must_match_the_one_the_code_was_issued_for(client):
    verifier, challenge = _pkce()
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://x/cb"]})
    cid = reg.json()["client_id"]
    resp = client.post(
        "/oauth/authorize",
        data={
            "api_key": "sk-sift-real",
            "client_id": cid,
            "redirect_uri": "https://x/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    code = re.search(r"code=([^&]+)", resp.headers["location"]).group(1)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://attacker.example/cb",
            "client_id": cid,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 400


def test_refresh_rotates_and_the_old_one_dies(client):
    """Otherwise a leaked refresh token is a permanent key."""
    granted = _full_flow(client)
    first = granted["refresh_token"]

    again = client.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first}
    )
    assert again.status_code == 200
    assert again.json()["refresh_token"] != first
    assert authorize(f"Bearer {again.json()['access_token']}") == "claude-web"

    replay = client.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first}
    )
    assert replay.status_code == 400


def test_grants_survive_a_restart(client, monkeypatch, tmp_path):
    """In-memory only meant every container bounce forced claude.ai to re-authorize."""
    granted = _full_flow(client)
    token = granted["access_token"]

    oauth.reset_for_tests()  # simulate the process dying
    assert authorize(f"Bearer {token}") == "claude-web"  # reloaded from disk


def test_an_unknown_client_id_is_rejected(client):
    r = client.post(
        "/oauth/authorize",
        data={
            "api_key": "sk-sift-real",
            "client_id": "never-registered",
            "redirect_uri": "https://x/cb",
            "response_type": "code",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_an_oversized_pre_auth_body_is_refused(client):
    """/oauth/* is read before any auth — an anon POST must not OOM the box."""
    r = client.post("/oauth/token", content=b"x" * (oauth.MAX_BODY_BYTES + 1))
    assert r.status_code == 413
