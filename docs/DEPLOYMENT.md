# DEPLOYMENT.md — Sift

How to run Sift as a **shared endpoint**: Docker, token auth, TLS, retention, backups.
The HTTP surface deliberately mirrors [Folio](https://github.com/azzindani/Folio)'s, so a
client configured for one works against the other.

---

## 1. The endpoint surface

| Path | Auth | Purpose |
|---|---|---|
| `POST /mcp` | **Bearer** | MCP JSON-RPC (Streamable HTTP): `initialize`, `tools/list`, `tools/call` |
| `GET /health` | public | Liveness + toolchain + auth mode. `503` when ffmpeg is broken. |
| `GET /version` | public | Running version, so a monitor can alert on a stale deploy without a token. |
| `GET /tokens/whoami` | **Bearer** | Which named token you presented. The cheapest auth sanity check. |
| `GET /clips/{batch}/{file}` | **Bearer** | Published clips, thumbnails, manifests, galleries. |
| `GET /library/*` | **Bearer / `?token=` / cookie** | Browse the library in a browser. Same key; no separate login. |
| `/oauth/*`, `/.well-known/oauth-*` | public | OAuth 2.0 + PKCE — the claude.ai Custom Connector handshake. |

`/health` and `/version` are public **on purpose**: a monitor that needs a secret to
check liveness is a monitor that stops working the day you rotate the secret. Neither
endpoint ever echoes a token value.

There is deliberately **no Basic Auth** anywhere. A browser username/password popup
cannot satisfy a bearer token, so adding one would only produce a prompt no agent can
answer.

---

## 2. Auth — four modes, in priority order

Identical contract to Folio's `src/mcp/auth.ts`.

| Priority | Env | Shape |
|---|---|---|
| 1 | `SIFT_TOKENS_FILE=/home/sift/tokens.json` | `{"claude": "sk-...", "hermes": "sk-..."}` |
| 2 | `SIFT_TOKENS` | `claude:sk-aaa,hermes:sk-bbb` |
| 3 | `SIFT_API_KEY` | a single shared bearer, registered as `default` |
| 4 | *(none)* | **open** — localhost only. The startup banner warns loudly. |

Wire format: `Authorization: Bearer <token>`.

**Use named tokens.** They cost nothing and they buy you two things: the audit log can
say *who* called a tool rather than just *that* someone did, and revoking one client is
one line out of a JSON file rather than a rotation across every client you own.

```sh
cp tokens.example.json tokens.json
openssl rand -hex 32          # once per client; paste into tokens.json
```

`tokens.json` is gitignored, and CI fails if it ever appears in the tree.

### Rate limiting

A token bucket per **(token, client-IP)** pair. Keyed on both, so one leaked token used
from a hundred hosts cannot multiply its allowance, and one shared NAT egress IP cannot
starve everyone behind it.

```
SIFT_RATE_BURST=40     # back-to-back requests allowed
SIFT_RATE_PER_SEC=10   # steady refill
```

Either at `0` disables it. Overflow returns `429` with a `Retry-After`.

---

## 3. Local

```sh
cp .env.example .env
cp tokens.example.json tokens.json     # put real random strings in it
docker compose up -d --build
```

- MCP: `http://localhost:8765/mcp` (Bearer)
- Health: `http://localhost:8765/health` (public)

```sh
curl -fsS localhost:8765/health | jq .
curl -fsS localhost:8765/tokens/whoami -H "Authorization: Bearer sk-..."
```

---

## 4. VPS with automatic HTTPS

1. Point DNS at the box: `A  sift.mydomain.tld -> <vps-ip>`
2. In `.env`:
   ```
   SIFT_DOMAIN=sift.mydomain.tld
   SIFT_ACME_EMAIL=you@mydomain.tld
   SIFT_TOKENS_FILE=/home/sift/tokens.json
   ```
3. ```sh
   docker compose --profile tls up -d --build
   ```

Caddy provisions a Let's Encrypt certificate and terminates HTTPS on `:443`, forwarding
`Authorization: Bearer` untouched. The app remains the sole auth gate.

Then point a client at it:

```json
{
  "mcpServers": {
    "sift": {
      "type": "http",
      "url": "https://sift.mydomain.tld/mcp",
      "headers": { "Authorization": "Bearer sk-sift-..." }
    }
  }
}
```

### 4a. The browsable library (`/library`)

The library is the durable record, so make it *visible*. Sift serves it itself — there is
nothing to mount, no static file server, and **no separate login**:

```
https://sift.mydomain.tld/library/?token=sk-sift-...
```

That is the **same key** as the MCP endpoint. It is exchanged once for an HttpOnly
`sift_session` cookie (30 days) and dropped from the URL, so it does not linger in history
or a screenshot. The cookie holds a *minted session token*, never the API key itself.
`Authorization: Bearer` works too, for scripts.

Behind a reverse proxy, just forward it — Sift is the sole gate:

```caddyfile
@library path /library /library/*
handle @library {
    reverse_proxy sift:8765 {
        header_up Host {host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

**Do not put basic auth in front of it.** A browser username/password popup can never be
satisfied by an access token, and a second credential for reading your own library defeats
the point of one key everywhere. (Folio's `/files` route started that way and dropped it
for exactly this reason; its Caddyfile still says so.)

Source **video** never appears there: it is downloaded, cut, and deleted. Transcripts,
candidates, clips and manifests do.

---

## 5. What to back up

```
sift-projects/     ← THE LIBRARY. YAML records + rendered clips. Back this up.
sift-data/         ← job queue + scratch. Rebuildable. Safe to lose.
```

That split is the whole point of the file-backed library. `sift-data/sift.db` holds the
render queue and an index from entity id to project — **the index is derived, never
authoritative**. Delete the database and it repopulates from the YAML on next boot.
Restore a `sift-projects/` backup onto a fresh box and the server simply picks it up.

To check: `rm sift-data/sift.db && docker compose restart sift` — everything still works.

---

## 6. Resources

The target box is 2 vCPU / 4 GB. `MCP_CONSTRAINED_MODE=1` (default in Docker) tightens
the profile: 480p source cap, 5-minute transcript windows, 24-frame vision budget.

| Knob | Default | Why |
|---|---|---|
| `SIFT_MEM_LIMIT` | `2g` | ffmpeg is modest; MediaPipe adds ~300–400 MB while a reframe runs. |
| `SIFT_CPUS` | `2` | Two encodes never run at once — the single render worker enforces that. |
| `SIFT_VISION` | `0` | MediaPipe face-follow. `1` adds ~300 MB to the image. |

**Disk is the binding constraint, not RAM.** A 3-hour source at 720p is ~2.7 GB. Sources
are downloaded, cut, and **deleted at publish**; a 24-hour TTL sweeps any that were
fetched and never published. Clips (~10 MB) and transcripts (~400 KB) are what persist,
so a hundred clips is under 2 GB.

Without the vision extra, `reframe="speaker"` degrades to a centred crop and says so in
the job's progress. That is a deliberate trade, not a silent failure.

---

## 7. Retention

`SIFT_TTL_HOURS` (default 168 = 7 days) controls how long a published **link** resolves.

Expiry **un-serves** a batch; it never deletes the clip from the library. The artifact
stays in `sift-projects/<project>/clips/<clip_id>/` where you can re-publish it. Deleting
a clip is an explicit act, never a timer.

---

## 8. Releases

Tag `v*.*.*` and CI publishes `ghcr.io/azzindani/sift:<version>` and `:latest`
(linux/amd64 + linux/arm64, so a cheap ARM VPS works too). The release job refuses to
publish if the tag disagrees with `pyproject.toml` **or** `server.VERSION` — the
`/version` endpoint is what a monitor alerts on, so it must not be able to lie.

Auto-update is **opt-in and off by default**:

```sh
# .env
SIFT_IMAGE=ghcr.io/azzindani/sift:latest
docker compose --profile autoupdate up -d
```

That runs Watchtower scoped by label to the Sift container only. It is off by default
because unattended upgrades are a real trade-off: you get fixes without touching the box,
but a bad release lands on your deployment automatically, and anyone who compromises the
registry runs code on your host. Pin a tag if you would rather update on purpose.

---

## 9. yt-dlp on a datacenter IP

This is the most fragile dependency in the system and the deployment has to own it.
Datacenter IPs draw bot challenges from every major video host. **Fetch failure is a
normal return path**, not an exception: it comes back as an error dict whose `hint` names
the knob that fixes it.

```
SIFT_COOKIES_PATH=/home/sift/cookies.txt   # mount it read-only
SIFT_PROXY=http://user:pass@proxy:8080
```

Verified behaviour: YouTube bot-challenges a datacenter IP and the tool reports
*"Bot challenge. Export browser cookies and pass cookies_path=..., or set
SIFT_COOKIES_PATH / SIFT_PROXY."* Hosts with open caption tracks (TED, etc.) work without
either.
