"""OAuth 2.0 Authorization Code + PKCE — a port of Folio's ``src/mcp/oauth.ts``.

Why this exists: claude.ai's Custom Connector does **not** accept a raw bearer token.
It walks an OAuth surface anonymously — discover, register, PKCE — and only then talks
MCP. Without these endpoints, Sift can be used from Claude Code, curl, n8n and Hermes
but *cannot be added to claude.ai at all*.

The bridge to the existing token registry is the whole point, and it is what makes one
key work everywhere: the access token minted by ``/oauth/token`` is opaque random bytes,
but it is *mapped back to the principal* whose ``sk-sift-…`` key was pasted at
``/oauth/authorize``. So the same ``tokens.json`` entry authenticates a direct
``Authorization: Bearer sk-sift-…`` (Claude Code, curl, n8n) **and** a claude.ai
connector session, and the audit log names the same principal for both.

Endpoints (all public — they are the pre-auth handshake):

    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    GET  /.well-known/oauth-protected-resource     RFC 9728 metadata
    POST /oauth/register                           RFC 7591 dynamic registration
    GET  /oauth/authorize                          login form — paste your Sift key
    POST /oauth/authorize                          form submit → code → 302
    POST /oauth/token                              code → access+refresh; refresh rotates

Access and refresh tokens are persisted to disk. In-memory only meant every container
restart forced claude.ai to re-authorize from scratch; a 30-day rotating refresh token
lets it mint new access tokens silently instead. Auth codes stay in memory — their
10-minute TTL is shorter than any restart window, so losing them is invisible.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("sift.oauth")

AUTH_CODE_TTL_S = 10 * 60
ACCESS_TOKEN_TTL_S = 24 * 60 * 60
REFRESH_TOKEN_TTL_S = int(os.environ.get("SIFT_REFRESH_TOKEN_TTL_S", 30 * 24 * 60 * 60))

# Without a TTL and a cap, every anonymous /oauth/register call leaks an entry —
# an unauthenticated memory-growth surface. 7 days matches agent rotation cycles.
CLIENT_TTL_S = 7 * 24 * 60 * 60
CLIENT_MAX = 256

# The OAuth body is always small (a login form, a code exchange, a DCR blob) and is
# read *before* any authentication, so cap it: an anon POST must not be able to OOM
# the container.
MAX_BODY_BYTES = int(os.environ.get("SIFT_OAUTH_MAX_BODY_BYTES", 256 * 1024))

OAUTH_PATH_PREFIXES = ("/oauth/", "/.well-known/oauth-")


def _state_dir() -> Path:
    configured = os.environ.get("SIFT_OAUTH_STATE_DIR", "").strip()
    if configured:
        return Path(configured)
    projects = os.environ.get("SIFT_PROJECTS_DIR", "").strip()
    return Path(projects or "/tmp") / ".oauth-state"


@dataclass
class _Grant:
    principal: str
    expires_at: float


@dataclass
class _AuthCode:
    principal: str
    redirect_uri: str
    client_id: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    expires_at: float


@dataclass
class _Client:
    redirect_uris: list[str]
    client_secret: str
    created_at: float


_lock = threading.Lock()
_auth_codes: dict[str, _AuthCode] = {}
_access: dict[str, _Grant] = {}
_refresh: dict[str, _Grant] = {}
_clients: dict[str, _Client] = {}
_loaded = False


def _static_client_id() -> str:
    return os.environ.get("SIFT_OAUTH_CLIENT_ID", "claude-ai").strip() or "claude-ai"


def _token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _load(path: Path) -> dict[str, _Grant]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    now = time.time()
    return {
        k: _Grant(v["principal"], v["expires_at"])
        for k, v in raw.items()
        if isinstance(v, dict) and v.get("expires_at", 0) > now
    }


def _persist(grants: dict[str, _Grant], path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            k: {"principal": g.principal, "expires_at": g.expires_at} for k, g in grants.items()
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("could not persist oauth grants to %s: %s", path, exc)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _access.update(_load(_state_dir() / "access-tokens.json"))
    _refresh.update(_load(_state_dir() / "refresh-tokens.json"))
    _clients.setdefault(
        _static_client_id(),
        _Client(redirect_uris=["*"], client_secret="", created_at=time.time()),
    )
    _loaded = True


def _reap() -> None:
    now = time.time()
    for code, rec in list(_auth_codes.items()):
        if rec.expires_at < now:
            del _auth_codes[code]

    for store, path in ((_access, "access-tokens.json"), (_refresh, "refresh-tokens.json")):
        expired = [k for k, g in store.items() if g.expires_at < now]
        for k in expired:
            del store[k]
        if expired:
            _persist(store, _state_dir() / path)

    static = _static_client_id()
    for cid, c in list(_clients.items()):
        if cid != static and now - c.created_at > CLIENT_TTL_S:
            del _clients[cid]
    if len(_clients) > CLIENT_MAX:
        evictable = sorted(
            ((cid, c) for cid, c in _clients.items() if cid != static),
            key=lambda kv: kv[1].created_at,
        )
        while len(_clients) > CLIENT_MAX and evictable:
            del _clients[evictable.pop(0)[0]]


def resolve_oauth_token(presented: str) -> str | None:
    """Map an ``/oauth/token``-issued access token back to its principal, or None."""
    with _lock:
        _ensure_loaded()
        _reap()
        grant = _access.get(presented)
        if grant is None or grant.expires_at < time.time():
            _access.pop(presented, None)
            return None
        return grant.principal


def _lookup_principal(api_key: str) -> str | None:
    """Validate a pasted Sift key against the SAME registry the bearer path uses."""
    from shared.auth import OPEN_PRINCIPAL, load_tokens  # noqa: PLC0415 - breaks a cycle

    registry = load_tokens()
    if registry.mode == "open":
        return OPEN_PRINCIPAL
    return registry.tokens.get(api_key)


def _issue_pair(principal: str, scope: str) -> dict[str, object]:
    access, refresh = _token(), _token()
    now = time.time()
    _access[access] = _Grant(principal, now + ACCESS_TOKEN_TTL_S)
    _refresh[refresh] = _Grant(principal, now + REFRESH_TOKEN_TTL_S)
    _persist(_access, _state_dir() / "access-tokens.json")
    _persist(_refresh, _state_dir() / "refresh-tokens.json")
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_S,
        "refresh_token": refresh,
        "scope": scope,
    }


# ── Handlers. Each returns (status, headers, body) so the transport stays dumb. ──


def metadata(base: str) -> dict[str, object]:
    """RFC 8414 — how a connector finds the authorize and token endpoints."""
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp"],
    }


def protected_resource(base: str) -> dict[str, object]:
    """RFC 9728 — what a 401 points at so the client knows where to authenticate."""
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    }


def register(body: bytes) -> tuple[int, dict[str, object]]:
    """RFC 7591 dynamic client registration. Public clients (PKCE) get no secret."""
    try:
        parsed = json.loads(body or b"{}")
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    uris = parsed.get("redirect_uris")
    redirect_uris = [str(u) for u in uris] if isinstance(uris, list) and uris else ["*"]

    method = parsed.get("token_endpoint_auth_method")
    secret = _token() if method and method != "none" else ""

    client_id = f"sift-{_token(8)}"
    with _lock:
        _ensure_loaded()
        _reap()
        _clients[client_id] = _Client(redirect_uris, secret, time.time())

    out: dict[str, object] = {
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post" if secret else "none",
    }
    if secret:
        out["client_secret"] = secret
    return 201, out


def authorize_form(query: dict[str, str]) -> tuple[int, str]:
    """The one human step: paste the same Sift key you use everywhere else."""
    for required in ("client_id", "redirect_uri", "response_type"):
        if not query.get(required):
            return 400, _page("Bad request", f"Missing <code>{_esc(required)}</code>.")
    if query["response_type"] != "code":
        return 400, _page("Bad request", "Only <code>response_type=code</code> is supported.")

    carried = (
        "client_id",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
    )
    hidden = "\n".join(
        f'<input type="hidden" name="{k}" value="{_esc(query[k])}">' for k in carried if k in query
    )
    client = _esc(query["client_id"])
    return (
        200,
        f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sift · Authorize</title>
<style>
  body{{font-family:Inter,system-ui,sans-serif;background:#0e0e16;color:#e8e8f0;display:flex;
       align-items:center;justify-content:center;min-height:100vh;margin:0}}
  .card{{background:#16182a;padding:32px;border-radius:12px;border:1px solid #2a2a4a;max-width:420px;width:100%}}
  h1{{margin:0 0 8px;font-size:20px;letter-spacing:-0.01em}}
  p{{color:#8892A4;font-size:14px;line-height:1.5;margin:0 0 16px}}
  label{{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8892A4;margin-bottom:6px}}
  input{{width:100%;padding:10px 12px;background:#0e0e16;border:1px solid #2a2a4a;border-radius:6px;
        color:#e8e8f0;font:14px Inter,sans-serif;box-sizing:border-box}}
  button{{margin-top:16px;width:100%;padding:10px;background:#E94560;color:#fff;border:0;border-radius:6px;
         font-weight:600;cursor:pointer}}
  code{{background:#0e0e16;padding:2px 6px;border-radius:4px;font-size:12px}}
</style></head>
<body><div class="card">
<h1>Authorize Sift access</h1>
<p>Client <code>{client}</code> wants to use the Sift MCP server. Paste your Sift API key —
the same one you set as <code>SIFT_API_KEY</code> or an entry in <code>tokens.json</code>.</p>
<form method="POST" action="/oauth/authorize">
{hidden}
<label for="api_key">Sift API key</label>
<input id="api_key" name="api_key" type="password" autocomplete="off" required>
<button type="submit">Authorize</button>
</form>
</div></body></html>""",
    )


def authorize_submit(body: bytes) -> tuple[int, str, str]:
    """Validate the pasted key, mint a one-shot code, return the redirect target."""
    form = dict(parse_qsl(body.decode("utf-8", "replace")))
    principal = _lookup_principal(form.get("api_key", ""))
    if not principal:
        return 401, _page("Invalid API key", '<a href="javascript:history.back()">Go back</a>'), ""

    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")

    with _lock:
        _ensure_loaded()
        _reap()
        client = _clients.get(client_id)
        if client is None:
            return (
                400,
                _page(
                    "Unknown client_id",
                    "Register via <code>/oauth/register</code> or set <code>SIFT_OAUTH_CLIENT_ID</code>.",
                ),
                "",
            )
        if "*" not in client.redirect_uris and redirect_uri not in client.redirect_uris:
            return (
                400,
                _page("Invalid redirect_uri", "That URI is not registered for this client."),
                "",
            )

        code = _token()
        _auth_codes[code] = _AuthCode(
            principal=principal,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_challenge=form.get("code_challenge", ""),
            code_challenge_method=form.get("code_challenge_method", ""),
            scope=form.get("scope", "mcp"),
            expires_at=time.time() + AUTH_CODE_TTL_S,
        )

    parts = urlsplit(redirect_uri)
    query = dict(parse_qsl(parts.query))
    query["code"] = code
    if form.get("state"):
        query["state"] = form["state"]
    location = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    return 302, "", location


def token(body: bytes) -> tuple[int, dict[str, object]]:
    """Exchange a code (PKCE-verified) or rotate a refresh token."""
    form = dict(parse_qsl(body.decode("utf-8", "replace")))
    grant_type = form.get("grant_type", "")

    with _lock:
        _ensure_loaded()
        _reap()

        if grant_type == "refresh_token":
            presented = form.get("refresh_token", "")
            rec = _refresh.get(presented)
            if rec is None or rec.expires_at < time.time():
                _refresh.pop(presented, None)
                _persist(_refresh, _state_dir() / "refresh-tokens.json")
                return 400, _err("invalid_grant", "Refresh token is missing, expired, or revoked.")
            del _refresh[presented]  # single-use: rotate on every refresh
            return 200, _issue_pair(rec.principal, form.get("scope", "mcp"))

        if grant_type != "authorization_code":
            return 400, _err(
                "unsupported_grant_type", "Supported grants: authorization_code, refresh_token."
            )

        code = form.get("code", "")
        rec_code = _auth_codes.pop(code, None)  # one-shot, whether or not it validates
        if rec_code is None or rec_code.expires_at < time.time():
            return 400, _err("invalid_grant", "Auth code is missing, expired, or already used.")

        if form.get("redirect_uri", "") != rec_code.redirect_uri:
            return 400, _err("invalid_grant", "redirect_uri mismatch.")

        # Confidential clients prove themselves with a secret; public (PKCE-only)
        # clients prove possession with the code_verifier checked just below.
        client = _clients.get(rec_code.client_id)
        if client is not None and client.client_secret:
            presented = form.get("client_secret", "")
            if not secrets.compare_digest(presented, client.client_secret):
                return 401, _err("invalid_client", "client_secret mismatch.")

        if rec_code.code_challenge:
            verifier = form.get("code_verifier", "")
            computed = _s256(verifier) if rec_code.code_challenge_method == "S256" else verifier
            if not secrets.compare_digest(computed, rec_code.code_challenge):
                return 400, _err("invalid_grant", "PKCE code_verifier mismatch.")

        return 200, _issue_pair(rec_code.principal, rec_code.scope)


def _err(code: str, description: str) -> dict[str, object]:
    return {"error": code, "error_description": description}


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>Sift · {_esc(title)}</title></head>"
        '<body style="font-family:system-ui,sans-serif;background:#0e0e16;color:#e8e8f0;padding:40px">'
        f"<h1>{_esc(title)}</h1><p>{body}</p></body></html>"
    )


def reset_for_tests() -> None:
    """Drop every grant, code and non-static client. Tests only."""
    global _loaded
    with _lock:
        _auth_codes.clear()
        _access.clear()
        _refresh.clear()
        _clients.clear()
        _loaded = False
