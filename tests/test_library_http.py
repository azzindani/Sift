"""The browsable library — same key as everything else, no separate login.

Folio's route dropped basic auth for a stated reason: a browser username/password popup
"could never be satisfied by an access token anyway". A second credential to read your own
library defeats one-key-everywhere. These tests pin that down: the ONLY way in is a Sift
token, and the browser never holds the key itself.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import server
from shared import browse, oauth
from shared.auth import reset_for_tests

KEY = "sk-sift-library"


@pytest.fixture
def client(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    (projects / "ep42" / "clips" / "c_1").mkdir(parents=True)
    (projects / "ep42" / "project.yaml").write_text(
        "_protocol: sift/project/v1\nname: ep42\n", encoding="utf-8"
    )
    (projects / "ep42" / "clips" / "c_1" / "clip.yaml").write_text(
        "label: quote\nsource_start: 253.0\n", encoding="utf-8"
    )
    # Machinery, not the record — must never be listed.
    (projects / ".oauth-state").mkdir()

    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"claude-code": KEY}), encoding="utf-8")

    monkeypatch.setenv("SIFT_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("SIFT_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("SIFT_OAUTH_STATE_DIR", str(tmp_path / "oauth-state"))
    monkeypatch.setenv("SIFT_RATE_BURST", "0")
    reset_for_tests()
    oauth.reset_for_tests()
    with TestClient(server.build_app()) as c:
        yield c
    reset_for_tests()
    oauth.reset_for_tests()


# ── The gate ──


def test_no_token_reads_nothing(client):
    r = client.get("/library/", follow_redirects=False)
    assert r.status_code == 401
    assert "ep42" not in r.text  # not a single filename leaks into the gate page
    assert "Access token required" in r.text


def test_no_token_cannot_read_a_file_directly(client):
    """The gate is on every path, not just the index."""
    r = client.get("/library/ep42/project.yaml", follow_redirects=False)
    assert r.status_code == 401
    assert "sift/project/v1" not in r.text


def test_a_wrong_token_is_refused(client):
    r = client.get("/library/", params={"token": "sk-sift-WRONG"}, follow_redirects=False)
    assert r.status_code == 401


def test_no_basic_auth_challenge_is_ever_sent(client):
    """The bug being fixed: a browser popup cannot carry a bearer token."""
    r = client.get("/library/", follow_redirects=False)
    assert "www-authenticate" not in {k.lower() for k in r.headers}


# ── The Folio hand-off: one key, then a cookie ──


def test_the_same_mcp_key_opens_the_library(client):
    r = client.get("/library/", params={"token": KEY}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/library/"
    assert oauth.SESSION_COOKIE in r.cookies


def test_the_cookie_never_contains_the_api_key(client):
    """The browser holds a minted session token — losing it must not leak the key."""
    r = client.get("/library/", params={"token": KEY}, follow_redirects=False)
    session = r.cookies[oauth.SESSION_COOKIE]
    assert session != KEY
    assert KEY not in session


def test_the_cookie_is_httponly_and_scoped(client):
    r = client.get("/library/", params={"token": KEY}, follow_redirects=False)
    cookie_header = r.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "path=/library" in cookie_header


def test_the_token_is_dropped_from_the_url(client):
    """It must not linger in history, logs, or a shared screenshot."""
    r = client.get("/library/", params={"token": KEY}, follow_redirects=False)
    assert "token=" not in r.headers["location"]


def test_the_session_then_browses_without_the_key(client):
    client.get("/library/", params={"token": KEY})  # sets the cookie on the client
    r = client.get("/library/")
    assert r.status_code == 200
    assert "ep42" in r.text


def test_a_bearer_token_also_works_for_scripts(client):
    r = client.get("/library/", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    assert "ep42" in r.text


# ── What it shows ──


def test_it_walks_down_to_the_record(client):
    client.get("/library/", params={"token": KEY})

    assert "clips" in client.get("/library/ep42/").text

    leaf = client.get("/library/ep42/clips/c_1/clip.yaml")
    assert leaf.status_code == 200
    assert "label: quote" in leaf.text
    assert leaf.headers["content-type"].startswith("text/plain")


def test_dotfiles_are_machinery_and_stay_hidden(client):
    """.oauth-state holds live grants. It is not part of the record."""
    client.get("/library/", params={"token": KEY})
    assert ".oauth-state" not in client.get("/library/").text


def test_traversal_out_of_the_library_is_refused(client):
    """Over the wire a client normalises ../ away, so some of these never even reach the
    route and are refused by the middleware (401) rather than the resolver (404). Both are
    refusals; what matters is that no byte of /etc/passwd comes back. The resolver itself
    is pinned separately below, where no client can pre-normalise the input."""
    client.get("/library/", params={"token": KEY})
    for attack in ("../../etc/passwd", "ep42/../../../etc/passwd", "%2e%2e%2fetc%2fpasswd"):
        r = client.get(f"/library/{attack}")
        assert r.status_code in (401, 404), attack
        assert "root:" not in r.text


def test_the_resolver_refuses_to_escape_the_root(tmp_path):
    """The containment check itself, fed the raw strings a proxy could still deliver."""
    root = tmp_path / "projects"
    (root / "ep42").mkdir(parents=True)
    (root / "ep42" / "clip.yaml").write_text("label: quote\n", encoding="utf-8")

    assert browse.resolve(root, "ep42/clip.yaml") is not None
    for attack in ("../etc/passwd", "../../etc/passwd", "ep42/../../etc/passwd", "/etc/passwd"):
        assert browse.resolve(root, attack) is None, attack


def test_a_symlink_pointing_out_of_the_library_is_refused(client, tmp_path):
    """resolve() follows symlinks, so containment is checked on the RESOLVED path —
    a symlink planted inside the library cannot be used to read /etc."""
    escape = tmp_path / "projects" / "ep42" / "escape.yaml"
    escape.symlink_to("/etc/hostname")

    assert browse.resolve(tmp_path / "projects", "ep42/escape.yaml") is None

    client.get("/library/", params={"token": KEY})
    assert client.get("/library/ep42/escape.yaml").status_code in (403, 404)
