# Sift

Turn long-form video into short, publish-ready vertical clips. Self-hosted MCP server,
agentic AI as the intelligence, file-backed project library, runs on a small VPS.

> **Deployment target:** Linux VPS (2 vCPU / 4 GB), agentic AI client over HTTP/MCP with a
> bearer token — the same endpoint style as [Folio](https://github.com/azzindani/Folio).
> This is **not** a local-model tool: the selection brain is an agentic cloud model, and
> fetch/vision are network operations by design. See [`CLAUDE.md`](CLAUDE.md) §4 for how it
> intentionally diverges from the org MCP `STANDARDS.md`.

## What it does

Give it a 3-hour podcast. It reads the transcript in overlapping windows, lets the agent
pick what's clip-worthy, then deterministically cuts, trims dead air, reframes to 9:16 with
a smoothed face-follow crop, burns in word-pop captions, and hands back links plus a
summary that **deep-links every clip back to its exact moment in the source**, so you can
falsify any pick in one click.

## Features

- **9 tools**, pipeline-shaped: fetch → read-chunk → add-candidates → plan → render → publish
- **The agent decides; the server only executes.** Zero inference in any tool.
- **File-backed project library** — YAML records you can read, diff, and hand-edit. Fix a
  clip's boundary in a text editor and the next `plan_clips` picks up the change.
- **Token-authed HTTP endpoint** (`Authorization: Bearer`), named tokens, rate limiting
- Chunked, overlapping transcript reading — no missed boundary-straddling moments
- Generalizes beyond quotes via a label + skill registry (jokes, stories, arguments,
  reactions, montages, supercuts) — new types are **data, not code**
- Spike-shaped execution: thin standby router, all heavy work in short-lived subprocesses
- Disposable source, durable output: a 3h source is ~2.7 GB and is deleted after the cut;
  clips (~10 MB) and transcripts (~400 KB) persist

## Quick start

```sh
cp .env.example .env
cp tokens.example.json tokens.json     # put real random strings in it
docker compose up -d --build
curl -fsS localhost:8765/health | jq .
```

Point a client at it:

```json
{
  "mcpServers": {
    "sift": {
      "type": "http",
      "url": "http://localhost:8765/mcp",
      "headers": { "Authorization": "Bearer sk-sift-..." }
    }
  }
}
```

For a public VPS with automatic HTTPS, set `SIFT_DOMAIN` in `.env` and run
`docker compose --profile tls up -d`. Full detail: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## The endpoint

| Path | Auth | Purpose |
|---|---|---|
| `POST /mcp` | Bearer | MCP JSON-RPC — `initialize`, `tools/list`, `tools/call` |
| `GET /health` | public | Liveness + toolchain. `503` when ffmpeg is broken. |
| `GET /version` | public | Running version — alert on a stale deploy without a token. |
| `GET /tokens/whoami` | Bearer | Which named token you are. |
| `GET /clips/{batch}/{file}` | Bearer | Published clips, thumbnails, manifests, galleries. |
| `/oauth/*`, `/.well-known/oauth-*` | public | OAuth 2.0 + PKCE — the claude.ai connector handshake. |

Auth is four modes in priority order — `SIFT_TOKENS_FILE` > `SIFT_TOKENS` > `SIFT_API_KEY` >
open (localhost only, warns loudly). Same contract as Folio, so one client config style
reaches both.

A configured-but-unusable token source is a **hard stop**, not a downgrade to open. A
missing, unreadable, malformed, or empty `tokens.json` aborts startup with a message
naming the fix. (The first live deployment mounted it `600 root:root` while the container
runs as uid 999 — the server came up serving every tool unauthenticated. It no longer can.)

### One key, every platform

`claude.ai` will not accept a raw bearer token; its Custom Connector requires an OAuth
handshake. So Sift exposes one — and bridges it to the *same* token registry:

```
Claude Code / curl / n8n  ──  Authorization: Bearer sk-sift-…   ┐
                                                                ├──►  principal "claude-code"
claude.ai Custom Connector ──  OAuth → paste that same key      ┘
```

The token `/oauth/token` mints is opaque, but it maps back to whichever `tokens.json`
entry you pasted at `/oauth/authorize`. Both paths are the same identity, and the audit
log names it the same way. There is nothing to configure.

**Adding it to claude.ai:** Settings → Connectors → *Add custom connector* → URL
`https://your-host/mcp`. Leave client ID and secret blank (dynamic registration handles
it). You'll get a Sift page asking for your API key — paste any value from `tokens.json`.

Access tokens last 24h and refresh silently for 30 days; grants are persisted, so a
container restart does not force you to reauthorize.

## The library

Work is organised into projects. **The files are the record** — SQLite is demoted to the
render queue and a *rebuildable* index.

```
sift-projects/<project>/
├── project.yaml            # index: sources, clips, exports
├── sources/<source_id>/
│   ├── source.yaml         # url, title, duration, transcript_kind
│   ├── transcript.json     # durable — outlives the video
│   └── video.mp4           # EPHEMERAL — deleted at publish
├── candidates/<source_id>.yaml   # the agent's picks — edit these by hand
├── clips/<clip_id>/
│   ├── clip.yaml           # members, label, assembly spec
│   ├── clip.mp4            # durable artifact
│   └── clip.jpg
└── exports/<batch_id>/     # manifest.json, index.html
```

Delete `sift-data/sift.db` and it repopulates from the YAML on next boot. The database can
never disagree with the library, because the library wins.

### Browsing it

Three ways in, and they see the same files:

| | |
|---|---|
| **`GET /library/`** | A browsable file server over the whole library — read-only, behind basic auth. Folio's `/files` pattern. Set `SIFT_BASIC_USER` / `SIFT_BASIC_HASH` and point your proxy at `SIFT_PROJECTS_DIR`; see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). |
| **`list_library()`** | The MCP tool — what the *agent* uses. Structured, bounded, no file dump. |
| **The filesystem** | It is just YAML. `git init` it, diff it, hand-edit a candidate boundary and the next `plan_clips` reads your edit. |

The browse gate is basic auth rather than the MCP bearer for one boring reason: a browser
cannot present an `Authorization: Bearer` header. Clips fetched by a *client* stay
bearer-gated at `/clips/…`.

## How it works

1. `fetch_source(url, project="ep42")` pulls the source at ≤720p + transcript, with a disk
   guard, URL dedup, and a caption check *before* any video is downloaded.
2. The agent reads the transcript in overlapping windows via `read_transcript_chunk`, judges
   clip-worthiness, labels intent, and submits picks with `add_candidates`.
3. `plan_clips` groups candidates by label/topic into clip definitions.
4. `render_clip` enqueues an async job. A **single worker** trims silence, cuts, reframes,
   captions, and thumbnails — one encode at a time. The agent polls `get_job`.
5. `publish_outputs` returns links plus a summary that deep-links back to the original.
6. `list_library` browses projects, sources, clips, and exports.

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/PIPELINE.md`](docs/PIPELINE.md) · [`docs/TOOLS.md`](docs/TOOLS.md) ·
[`docs/LABELS_AND_SKILLS.md`](docs/LABELS_AND_SKILLS.md) ·
[`docs/OUTPUT_CONTRACT.md`](docs/OUTPUT_CONTRACT.md) ·
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)

## Requirements

- **Docker** (recommended), or **Python 3.12** + **uv** + **ffmpeg** (libx264 + libass)
- An agentic AI client that speaks MCP

yt-dlp is a pinned Python dependency — no PATH lookup, version locked by `uv.lock`. The
caption font is bundled in `assets/fonts/`; system fonts are never resolved, because they
are not deterministic across hosts.

### Face-follow reframing (optional)

`reframe="speaker"` and `"stacked"` use MediaPipe. It is a ~300 MB optional extra,
lazy-imported inside the render worker:

```sh
SIFT_VISION=1 docker compose up -d --build     # or: uv sync --extra vision
```

**Without it the server still works.** `speaker` degrades to a centred crop and says so in
the job's progress. On a 4 GB box you may not want the footprint, and a centred crop on a
talking-head source is usually fine.

## Development

```sh
uv sync --all-extras
uv run pytest tests/ -q          # 73 tests; render tests run real ffmpeg, nothing is mocked
uv run ruff check . && uv run ruff format --check .
```

The render tests build a synthetic source (a tone cycling 6s on / 4s off, so `silencedetect`
has real dead air to find) and assert on the **actual encoded bytes** — dimensions, duration,
audio presence — because every interesting bug in this codebase lives in the filtergraph, not
in the Python around it.

## Caveats — the honest ones

- **yt-dlp on a VPS IP is the most fragile dependency.** Datacenter IPs draw bot challenges.
  Fetch failure is an *expected* return path: it comes back as an error dict whose hint names
  the knob (`SIFT_COOKIES_PATH`, `SIFT_PROXY`) that fixes it. Verified: YouTube challenges a
  datacenter IP; TED works without cookies.
- **Transcript timing is best-effort.** `json3` gives word-level timing (word-pop captions).
  VTT is cue-level, so captions fall back to styled lines — faking per-word timing from cue
  timing would put words on screen at the wrong moment, so we don't. Sources with no captions
  are rejected at fetch, *before* the video is downloaded.
- **`stacked` needs a real two-shot.** If MediaPipe doesn't find two distinct face clusters it
  falls back to speaker-follow and says so.
- **Label quality is bounded by signal.** Text-carried intent (quote, joke, argument) is
  dependable. Intent that lives only in the video is not — "funny" is subjective enough that
  no model scores it perfectly. The edge is the cheap-cue → vision funnel: free audio/text cues
  decide *where to look*, so vision is only paid for on flagged spans.
- **Copyright / platform ToS** for downloading and republishing is the operator's
  responsibility. The tool does not enforce it.

## License

MIT. Bundled font: Liberation Sans, SIL Open Font License 1.1 (see [`LICENSE`](LICENSE)).
