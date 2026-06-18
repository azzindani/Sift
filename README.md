# Clipper

Turn long-form video into short, publish-ready vertical clips: self-hosted MCP server,
agentic AI as the intelligence, runs on a small VPS.

> **Deployment target:** Linux VPS (2 vCPU / 4 GB), agentic AI client over HTTP/MCP.
> This is **not** a local-model tool — the selection brain is an agentic cloud model, and
> fetch/vision are network operations by design. See [`CLAUDE.md`](CLAUDE.md) §4 for how this
> intentionally diverges from the org MCP `STANDARDS.md` (which targets offline local models).

## Features

- **8 tools**, pipeline-shaped: fetch → read-chunk → add-candidates → plan → render → publish
- Agent decides what to clip; the server only executes (zero inference in tools)
- Chunked, overlapping transcript reading — no missed boundary-straddling moments
- Generalizes beyond quotes via a label + skill registry (jokes, stories, arguments,
  reactions, montages, supercuts) — new types are data, not code
- Spike-shaped execution: thin standby router, all heavy work in short-lived subprocesses
- 9:16 reframe with smoothed face-follow crop, word-pop captions reused from the transcript,
  dead-air trimming, thumbnails, transitions — all via ffmpeg
- Disposable source, durable output: links + a verifiable summary mapping each clip to its
  source timestamp and original URL

## How it works

1. `fetch_source(url)` pulls the source at ≤720p + transcript (`json3` word-timing), with a
   disk guard and URL dedup.
2. The agent reads the transcript in overlapping windows via `read_transcript_chunk`, judges
   clip-worthiness, labels intent, and submits picks with `add_candidates`.
3. `plan_clips` groups candidates by label/topic into clip definitions.
4. `render_clip` enqueues an async job; a single worker trims silence, cuts, reframes,
   captions, and thumbnails. The agent polls `get_job`.
5. `publish_outputs` moves clips to the served directory and returns links plus a summary that
   deep-links back to the original video for verification.

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/PIPELINE.md`](docs/PIPELINE.md) · [`docs/TOOLS.md`](docs/TOOLS.md) ·
[`docs/LABELS_AND_SKILLS.md`](docs/LABELS_AND_SKILLS.md) ·
[`docs/OUTPUT_CONTRACT.md`](docs/OUTPUT_CONTRACT.md)

## Requirements

- **Python 3.12 or higher**
- **uv** — https://docs.astral.sh/uv/getting-started/installation/
- **ffmpeg** (with libx264) and **yt-dlp** available on the host
- An agentic AI client that speaks MCP over HTTP
- A bundled caption font in `assets/fonts/` (referenced by path, never system-resolved)

## Install (VPS, HTTP transport)

Self-updating launch (STANDARDS §31): clone-guard on `.git`, `git fetch + reset --hard`,
`uv sync` on launch, `MCP_CONSTRAINED_MODE` env var.

```sh
d="$HOME/.mcp_servers/Clipper"
if [ ! -d "$d/.git" ]; then rm -rf "$d"; git clone https://github.com/azzindani/Clipper.git "$d" --quiet
else cd "$d" && git fetch origin --quiet && git reset --hard FETCH_HEAD --quiet; fi
cd "$d" && uv sync --quiet
MCP_CONSTRAINED_MODE=1 uv run python server.py --transport http --port 8765
```

Front it with Caddy for TLS + the served clips directory. Optionally wrap with Sablier-style
on-demand lifecycle for true zero-idle RAM at the cost of cold-start latency.

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `MCP_CONSTRAINED_MODE` | VPS limits: encode concurrency 1, lower vision frame budget, capped source resolution, smaller read windows | `1` on small boxes |
| `CLIPPER_COOKIES_PATH` | yt-dlp cookies file (mitigates VPS bot challenges) | unset |
| `CLIPPER_PROXY` | proxy for yt-dlp | unset |
| `CLIPPER_SERVE_DIR` | served outputs directory | `./served` |
| `CLIPPER_TTL_HOURS` | output retention | `168` |

## A note on the constrained box

This server runs on a 2 vCPU / 4 GB VPS. Standby is a thin router (~tens of MB); all heavy
work happens as short-lived subprocesses that release memory on exit. It is a **queue-and-cook
async service**, not real-time — run one render at a time and let jobs cook. Disk, not RAM, is
the limiting resource: sources are downloaded at capped resolution, processed, and deleted.

## Caveats

- **yt-dlp on a VPS IP** is the most fragile dependency — datacenter IPs draw bot challenges.
  Configure `CLIPPER_COOKIES_PATH` / `CLIPPER_PROXY`; fetch failure is an expected path.
- **Transcript timing** is best-effort: `json3` gives word-level (word-pop captions); VTT is
  cue-level (line captions + snap-to-silence cuts); no-caption sources need external
  transcription, which v1 does not run by default.
- **Copyright / platform ToS** for downloading and republishing content is the operator's
  responsibility.

## License

MIT (dependencies vetted against the approved-license list in `STANDARDS.md` §33).
