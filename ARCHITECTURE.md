# ARCHITECTURE.md — Clipper

How Clipper is shaped, why it stays light, and how it maps onto (and departs from)
the org MCP standard. Read [`../CLAUDE.md`](../CLAUDE.md) first.

---

## 1. The mental model

Clipper is a **deterministic executor** wrapped in an MCP surface. The agentic model is
the intelligence: it reads transcript chunks, decides which moments are clip-worthy, labels
them, and orchestrates the pipeline. The server never reasons about content — it fetches,
persists, cuts, encodes, and reports.

```
Agent's job:   read chunks → judge clip-worthiness → label intent → orchestrate → verify
Server's job:  fetch, store candidates, run signal processing, encode, publish links
```

This is the standard's §2 split, and it is the line that keeps the server testable,
cheap, and free of hidden model calls.

---

## 2. Execution model — spike-shaped, no idle daemon

The standby process is a thin MCP router plus SQLite: tens of MB, holding no models and
no video. Every heavy operation is a short-lived child process that releases its memory
on exit.

```
PERSISTENT (always resident while a session/HTTP connection is open):
  MCP router + SQLite job/candidate state         ~30–80 MB

ON-DEMAND (spawned per operation, RAM reclaimed on exit):
  yt-dlp            network-bound, light CPU
  ffmpeg            the only real CPU cost; one encode at a time
  frame sampling    ffmpeg -ss -frames:v, fast
  MediaPipe         lazy-imported, CPU, only inside render when reframing
```

**Three rules keep it light:**

1. **Lazy imports.** MediaPipe, OpenCV, and any heavy library are imported *inside* the
   function that needs them, never at module load (STANDARDS §15). The router and the
   non-vision path never pay their import cost.
2. **Enqueue, don't encode.** Render tools write a job row and return. A single worker
   drains the queue, so two encodes never share the two cores.
3. **Disposable source.** Download → cut → delete. Source video never lingers on disk.

**Why not Sablier/HTTP-on-demand (from the lab setup)?** That gives true zero-idle RAM but
adds container cold-start latency on every request. For one tool on one box, the thin-router
+ shelled-out-subprocess model gets ~95% of the benefit with none of the orchestration. The
HTTP transport is still supported (§30) for remote access; Sablier-style lifecycle is an
optional deployment wrapper, not a code dependency.

---

## 3. Resource budget (2 vCPU / 4 GB VPS)

| Concern | Reality | Mitigation |
|---|---|---|
| RAM | Router tiny; MediaPipe ~200–400 MB when loaded; ffmpeg modest | Lazy import, serialize, reclaim on exit |
| CPU | Only re-encodes cost (reframe + caption burn-in force pixel changes) | `-preset veryfast`, `-threads 2` cap, encode concurrency = 1 |
| Disk | A 1–4h source at 1080p is 2–8 GB — the real killer | Fetch at capped resolution (720p), disk guard before download, delete after cut, dedup by URL |
| Throughput | Few jobs in parallel | Queue-and-cook; this is async, not real-time |

`MCP_CONSTRAINED_MODE=1` (set by the installer on small boxes) tightens: encode concurrency 1,
vision frame budget lowered, source resolution capped, candidate return limits lowered.

---

## 4. Tier mapping (STANDARDS §7)

Clipper is a single server. Its 8 tools span tiers; the surface is small enough to load
together for an agentic client (the §8 local-VRAM tool ceiling does not bind here, but the
discipline does).

| Tier | Tools | Nature |
|---|---|---|
| 1 — read/inspect | `fetch_source` (network), `read_transcript_chunk`, `sample_frames`, `get_job` | Surgical reads; return metadata/bounded windows/frame refs, never bulk |
| 2 — structured | `add_candidates`, `plan_clips` | Persist agent decisions; deterministic grouping |
| 3 — render/export | `render_clip` (async), `publish_outputs` | Heavy encode (queued) and finalization to links |

Full contracts in [`TOOLS.md`](TOOLS.md).

---

## 5. Data flow

```
                         ┌─────────────────────────────────────────────┐
                         │                 AGENT (LLM)                  │
                         │  reads chunks · judges · labels · orchestrates│
                         └───────┬───────────────────────────┬─────────┘
                                 │ calls tools               │ polls
                                 ▼                           ▼
  fetch_source ──► [source @720p + transcript json3] ──► (disk guard, URL dedup, delete-after-cut)
       │                                 │
       │                                 ▼
       │                     read_transcript_chunk(i)  ◄── overlapping windows (2-min look-back)
       │                                 │
       │                                 ▼
       │                     add_candidates([{start,end,label,score,reason}, ...])
       │                                 │   (validate → dedup/merge → persist in SQLite)
       │                                 ▼
       │   sample_frames(span) ──► [capped frames] ──► agent vision pass (semantic only)
       │                                 │
       │                                 ▼
       │                          plan_clips()  ──► group by label/topic → clip definitions
       │                                 │
       │                                 ▼
       │                      render_clip(clip_id)  ──► ENQUEUE job, return job_id
       │                                 │
       │                          ┌──────┴───────┐
       │                          │ single render │  silence-trim → cut → multi-cut assemble
       │                          │   worker      │  → 9:16 reframe (MediaPipe smoothed)
       │                          │  (drains queue)│  → ASS caption burn-in → thumbnail
       │                          └──────┬───────┘
       │                                 ▼
       │                            get_job(job_id)  ◄── agent polls until done
       │                                 │
       │                                 ▼
       └────────────────────► publish_outputs(job_ids)
                                         │
                                         ▼
                          served dir + manifest.json + summary
                          (links + per-clip source_url/start/end)
```

---

## 6. State

SQLite is the single source of runtime state (STANDARDS §19 companion-state spirit, adapted —
there is no source file to snapshot, so state lives in the DB).

- **`sources`** — `source_id`, `url`, `duration`, `transcript_kind` (`json3`/`vtt`/`none`),
  `local_path` (nullable once deleted), `fetched_at`.
- **`candidates`** — `id`, `source_id`, `start`, `end`, `label`, `score`, `reason`,
  `cues` (json), `dedup_group`.
- **`clips`** — `clip_id`, `source_id`, `label`, member candidate ids, target spec.
- **`jobs`** — `job_id`, `clip_id`, `status` (`queued`/`running`/`done`/`failed`),
  `temp_dir`, `output_path`, `error`, timestamps.

**Restart reconciliation:** on startup, any `running` job is marked `failed` (interrupted)
and its `temp_dir` is swept. No orphaned jobs, no leaked temp files.

**Receipt log (STANDARDS §25):** every render and publish appends a receipt; `append_receipt`
never raises.

---

## 7. The hard external dependencies (own them honestly)

- **yt-dlp on a VPS IP** is the most fragile point: datacenter IPs draw bot challenges and
  throttling. Cookies-file and proxy config are first-class. Fetch failure is a *normal*
  return path with a `hint`, not an exception.
- **Transcript quality** is variable. `--sub-format json3` carries word timing; VTT is
  cue-level only. Fetch records `transcript_kind` so downstream stages know whether word-pop
  captions are possible or whether to fall back to cue-level + snap-to-silence.
- **No-caption sources** require external transcription (API), which is a cost and latency
  hit; on 2c/4GB, local Whisper is too slow to be the default. v1 returns a clear error and
  lets the agent decide whether to invoke a transcription path.
- **Copyright / ToS** of downloading and republishing is the operator's responsibility;
  documented in the README, not enforced by the tool.

These are covered operationally in [`PIPELINE.md`](PIPELINE.md) §1 and [`OUTPUT_CONTRACT.md`](OUTPUT_CONTRACT.md).
