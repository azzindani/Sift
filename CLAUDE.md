# CLAUDE.md — Clipper

> Self-hosted MCP server that turns long-form video into short, publish-ready clips.
> An agentic AI model is the intelligence; this server is the deterministic executor.

This file is the entry point for any AI coding agent working in this repository.
It defines what Clipper is, how it is built, where it deliberately departs from the
organization's MCP `STANDARDS.md`, and what must never be done. Read it fully before
touching code.

**Standards reference:** https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md
Per that document's own closing rule, *when the standard conflicts with this CLAUDE.md,
this file takes precedence for this project.* The divergences below are intentional and
are the result of a different deployment target, not an oversight.

---

## 1. Project Overview and Goals

Clipper ingests a long source video (a 1–4 hour podcast, interview, or stream), reads
its transcript in overlapping chunks, lets an agentic model select clip-worthy moments,
then deterministically cuts, trims, reframes, captions, and assembles them into vertical
short-form clips (10–60s). It returns a set of links plus a human-checkable summary that
maps every clip back to its source timestamp and original video URL.

**Primary goals**

- **Agentic, not local.** The selection brain is an agentic cloud model (e.g. Claude).
  The server does zero AI inference. It validates, executes, and returns structured data.
- **Lightweight, spike-shaped execution.** Runs on a 2 vCPU / 4 GB VPS. Standby footprint
  is a thin router (~tens of MB). All heavy work (ffmpeg, frame sampling) runs as
  short-lived subprocesses that release RAM on exit. No resident model, no idle daemon.
- **Generalizable beyond quotes.** A label + skill registry lets the same pipeline produce
  quotes, jokes, stories, arguments, reactions, montages, and supercuts. New clip types
  are added as data (a label + a skill), not engine changes.
- **Disposable source, durable output.** Source video is downloaded, cut, and deleted.
  Only short clips, thumbnails, and the manifest persist.
- **Verifiable output.** Every clip links back to `start`/`end` in the source and the
  original URL so the user can double-check selections.

**Non-goals (v1)**

- Beating commercial clip tools on output polish. Edge is pipeline ownership and cost control.
- Real-time processing. This is a queue-and-cook async service.
- Splicing distant moments into one clip. v1 multi-cut only removes dead air *inside* a span.
- Complex motion graphics. Motion is limited to what ffmpeg filtergraphs provide.

---

## 2. Repository Structure

Single-server flat layout (one domain, one server). Promote to monorepo only if a second
server is added.

```
Clipper/
├── server.py                  # FastMCP wrapper — thin, one-liner tool bodies
├── engine.py                  # thin/partial router — domain logic, zero MCP imports
├── _clip_helpers.py           # shared imports, constants, _error, budget guards
├── _clip_fetch.py             # yt-dlp source + transcript (json3), dedup, disk guard
├── _clip_transcript.py        # chunking with overlap, word-timing parse, ASS generation
├── _clip_select.py            # candidate persistence, dedup/merge, grouping, clip plan
├── _clip_render.py            # ffmpeg: cut, trim-silence, reframe, caption, thumbnail
├── _clip_queue.py             # SQLite job queue, single render worker, restart reconcile
├── _clip_publish.py           # move to served dir, build manifest + summary + links
├── shared/                    # version_control, file_utils, platform_utils, progress, receipt
├── assets/
│   └── fonts/                 # bundled caption font(s) — referenced by path, never system-resolved
├── tests/
│   ├── fixtures/              # real transcripts (json3 + vtt), short test clips
│   └── test_engine.py
├── install/
│   ├── install.sh             # POSIX sh
│   └── install.bat
├── docs/
│   ├── ARCHITECTURE.md        # tiers, divergences, execution model, data flow
│   ├── PIPELINE.md            # stage-by-stage technical design + ffmpeg filtergraphs
│   ├── TOOLS.md               # the 8-tool surface, schemas, annotations, contracts
│   ├── LABELS_AND_SKILLS.md   # intent label registry + skill injection design
│   └── OUTPUT_CONTRACT.md     # manifest, summary, link lifecycle, retention
├── pyproject.toml             # requires-python = "==3.12.*", fastmcp pinned
├── uv.lock
├── .python-version
├── CLAUDE.md                  # this file
├── STANDARDS.md               # copy of the org standard (reference)
└── README.md
```

**Document index — read these for detail:**

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — execution model, tier mapping, full divergence table
- [`docs/PIPELINE.md`](docs/PIPELINE.md) — every stage, the ffmpeg filtergraphs, the cheap-cue→vision funnel
- [`docs/TOOLS.md`](docs/TOOLS.md) — the MCP tool surface and per-tool contracts
- [`docs/LABELS_AND_SKILLS.md`](docs/LABELS_AND_SKILLS.md) — how intent is detected and injected
- [`docs/OUTPUT_CONTRACT.md`](docs/OUTPUT_CONTRACT.md) — links, manifest, the verifiable summary

---

## 3. Architecture Principles (conformant)

These follow `STANDARDS.md` and are not negotiable.

- **Engine/server split (§14).** `server.py` tool bodies are one line: `return engine.x(...)`.
  Anything touching domain data lives in `engine.py` or a `_clip_*` sub-module. Sub-modules
  have zero MCP imports. No file exceeds 1,000 lines (§15).
- **Server does no inference (§2).** The agent decides *what* to clip and *why*. The server
  only persists and executes. There is no "smart" guessing inside any tool.
- **Surgical reads (§10).** No tool returns the full transcript or a video. `read_transcript_chunk`
  returns one bounded window. `fetch_source` returns metadata, not the transcript body.
  Frame sampling returns capped frame references, never raw video.
- **Tool count discipline (§8).** 8 tools. Never exceed 10. Fewer, sharper tools.
- **Dict returns + token_estimate (§16).** Every tool returns a dict with `success` first,
  a `progress` array, and `token_estimate`. Never a string/list/None/bool.
- **Error contract (§17).** No exceptions reach the caller. Every failure is a dict with
  `error` and an actionable `hint` naming a specific tool or value.
- **Subprocess safety (§18).** Every ffmpeg/yt-dlp call uses an argument list, `shell=False`,
  an explicit `timeout`, and `capture_output=True`. All paths pass `resolve_path()` first.
- **No stdout (§28).** stdout is the MCP channel. All logs go to stderr via `logging`.
- **CPU-first (§21).** ffmpeg and MediaPipe run on CPU. No CUDA/GPU dependency anywhere.
- **Self-updating mcp.json (§31).** Clone-guard on `.git`, `git fetch + reset --hard`,
  `uv sync` on launch, `MCP_CONSTRAINED_MODE` env var, `600000` timeout.
- **Async for long work (§23).** Renders are async jobs; tools return immediately with a
  job id and the agent polls `get_job`.

---

## 4. Divergences from STANDARDS.md (intentional — agentic, not local)

The standard targets a local 9B model on 8 GB VRAM with offline sovereignty. Clipper targets
an agentic cloud model orchestrating a 2c/4GB VPS. The following departures are deliberate.

| # | STANDARDS rule | Clipper divergence | Why |
|---|---|---|---|
| 1 | §4 Self-hosted execution — no cloud API as primary engine, must run offline | Clipper **requires** network: yt-dlp fetches sources, the agent (cloud LLM) does selection, optional vision API scores visual moments | The deployment target is agentic, not sovereign-offline. The *MCP server* is self-hosted; *execution* is intentionally network-native. This is the §4 "explicitly scoped to network operations" exception, applied to the whole tool. |
| 2 | §20/§21 Token budget for ~12K local context, ~100–300 tokens/turn | Budget discipline kept, but tuned for **cost and long-loop context saturation**, not a tiny KV cache. Chunked transcript reading exists to control input-token spend and avoid context bloat in long agentic runs. | The constraint is API token economics and loop length, not local VRAM. |
| 3 | §9 LOCATE→INSPECT→PATCH→VERIFY four-tool loop | Replaced with a **pipeline shape**: fetch → read-chunk → add-candidates → plan → render → publish. Spirit kept (reads separated from actions, surgical returns, a `get_job` verify step). | A video pipeline is not a CRUD edit loop. Forcing the four-tool pattern produces a bad abstraction. |
| 4 | §19 Snapshot-before-write on source data | Source is **disposable**, not edited — downloaded, cut, deleted. No source snapshot. Job state + receipt log are persisted instead; render outputs are immutable artifacts. | There is no source mutation to roll back. The durable record is the manifest. |
| 5 | §26 Output beside input file, else ~/Downloads | Output goes to a **served directory** and the tool returns **HTTP links + a manifest**, with a TTL retention policy. | Outputs are consumed remotely over HTTP on a VPS, not opened locally. |
| 6 | §8/§21 Hardware tier = local LLM VRAM | `MCP_CONSTRAINED_MODE` reframed to mean **VPS resource limits** (2c/4GB): caps concurrency to 1 encode, lowers vision frame budget, lowers source resolution. | The hardware constraint is the box, not a local model. |
| 7 | README "Tested on Windows 11 / LM Studio" framing | README documents **Linux VPS + agentic client** deployment as primary, with HTTP transport. | The runtime is a server, not a desktop LM Studio install. |

Everything not listed here conforms to the standard.

---

## 5. Domain-Specific Tool Design Rules

- **The agent scores; the server stores.** Selection tools accept the agent's chosen spans,
  scores, and labels — they do not compute them. `add_candidates` validates and persists.
- **Read tools are bounded and overlapping.** `read_transcript_chunk(index)` returns one
  window with a fixed look-back overlap (default 2 minutes) so the agent always sees lead-in
  context and never misses a boundary-straddling moment.
- **Render tools enqueue, never encode inline.** `render_clip` writes a job row and returns a
  `job_id` immediately. A single worker drains the queue. This is the concurrency limit.
- **Determinism stays in the engine.** Silence detection, dedup/merge, grouping by label,
  crop-coordinate smoothing, and ASS generation are pure functions of their inputs — no model.
- **Vision is funneled and capped.** Cheap cues (audio energy, transcript markers) flag regions
  first; the agent's vision pass runs only on flagged spans, under a hard per-job frame budget.
- **Every output is traceable.** Every clip records `source_url`, `source_start`, `source_end`,
  and the candidate(s) it was built from, surfaced in the manifest for user verification.

See [`docs/TOOLS.md`](docs/TOOLS.md) for the full tool surface and contracts.

---

## 6. What the AI Must Never Do

In addition to the global `STANDARDS.md` §36 prohibitions (no stdout, dict returns only,
no `eval`/`exec`, `shell=False`, `resolve_path` first, ≤10 tools, no business logic in
`server.py`, etc.), Clipper adds:

1. **Never put AI inference inside a tool.** No tool may call an LLM or "decide" what is
   clip-worthy. The agent decides; the tool persists. Scoring/labeling come *in* as arguments.
2. **Never encode video inside a tool body.** Render tools enqueue jobs. The worker encodes.
3. **Never run more than one encode at a time** on the constrained box. The queue enforces this.
4. **Never download a source without the disk guard** and without deleting it after the cut step.
5. **Never resolve fonts from the system.** Caption rendering references the bundled font in
   `assets/fonts/` by path via ffmpeg `fontsdir`. System fonts are non-deterministic across hosts.
6. **Never send dense frame streams to the vision API.** Sample sparse, respect the frame budget,
   interpolate crop coordinates between samples.
7. **Never return a clip without its source mapping** (`source_url`, `source_start`, `source_end`).
8. **Never leave a `running` job after restart.** Startup reconciliation marks interrupted jobs
   `failed` and sweeps their temp directories.
9. **Never assume a transcript exists or is word-level.** Detect at fetch time; degrade with a
   clear hint (snap-to-silence for cue-level timing; explicit error for no-caption sources).
10. **Never treat yt-dlp success as guaranteed.** Bot challenges and throttling on VPS IPs are
    expected; fetch failure is a normal path with a `hint` about cookies/proxy config.

---

## 7. Progress Tracker

### Phase 0 — Design (this doc set)
- [x] Pipeline walkthrough + loophole analysis
- [x] CLAUDE.md with divergences from STANDARDS
- [x] ARCHITECTURE.md
- [x] PIPELINE.md
- [x] TOOLS.md
- [x] LABELS_AND_SKILLS.md
- [x] OUTPUT_CONTRACT.md

### Phase 1 — Skeleton + fetch
- [ ] Repo scaffold, `pyproject.toml` (`==3.12.*`, fastmcp pinned), `uv sync`
- [ ] `shared/` modules (file_utils with `resolve_path`, progress, receipt, platform_utils)
- [ ] `_clip_fetch.py`: yt-dlp source @ capped resolution + `--sub-format json3`, disk guard, URL dedup
- [ ] `fetch_source` + `read_transcript_chunk` tools, transcript word-timing parse
- [ ] Tests: fetch metadata, chunk overlap, json3 + vtt fixtures, no-caption error path

### Phase 2 — Selection + plan
- [ ] `_clip_select.py`: `add_candidates` (validate, dedup/merge), `plan_clips` (group by label)
- [ ] Label registry + skill files (see LABELS_AND_SKILLS.md)
- [ ] `sample_frames` tool (budget-capped) for the agent's vision pass
- [ ] Tests: dedup/merge boundaries, grouping, frame budget enforcement

### Phase 3 — Render + queue
- [ ] `_clip_queue.py`: SQLite job queue, single worker, restart reconciliation
- [ ] `_clip_render.py`: silencedetect trim, single-cut, multi-cut w/ acrossfade+xfade,
      9:16 reframe + MediaPipe crop smoothing, ASS caption burn-in, thumbnail
- [ ] `render_clip` (async enqueue) + `get_job` tools
- [ ] Tests: trim correctness, crop smoothing stability, caption sync, encode serialization

### Phase 4 — Publish + ship
- [ ] `_clip_publish.py`: served dir, manifest + verifiable summary, TTL cleanup
- [ ] `publish_outputs` tool
- [ ] HTTP transport, mcp.json (self-updating), install scripts, Caddy/serve config
- [ ] README per STANDARDS §35 (VPS-adapted), CI (lint/format/pyright/docstring/test)
- [ ] End-to-end test on the 2c/4GB VPS: one 2h source → ≥5 clips → links + summary
