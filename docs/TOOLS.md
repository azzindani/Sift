# TOOLS.md — Sift

The MCP tool surface: **9 tools**, pipeline-shaped. Every tool returns a dict with `success`
first, a `progress` array, and `token_estimate` (STANDARDS §16). Docstrings are ≤80 chars
(§11). Parameters use only allowed primitive types (§11). All file paths pass
`resolve_path()` first (§18). The server does no inference — selection/labeling arrive as
arguments (§2).

Annotation legend (STANDARDS §12): `RO`=readOnlyHint, `D`=destructiveHint,
`I`=idempotentHint, `OW`=openWorldHint.

---

## Tier 1 — read / inspect

### `fetch_source` — network
```
fetch_source(url: str, max_height: int = 720, cookies_path: str = "",
             project: str = "default") -> dict
"""Fetch video + transcript by URL into a project. Returns metadata."""
```
- Annotations: RO=False, D=False, I=False, OW=True
- **Async, and split in two.** The probe + disk guard + *transcript* happen inline
  (seconds). The **video download is queued** on the same single worker that encodes, and
  the tool returns immediately with a `download_job_id`.
  A 3-hour source takes minutes to pull and an MCP client times out around 30 seconds, so a
  synchronous fetch would simply never return over HTTP (STANDARDS §23).
- The agent reads the transcript and picks candidates *while the video downloads* — the two
  overlap instead of stacking. `render_clip` queues behind the download automatically,
  because one worker drains the queue in order.
- Refuses a source with **no captions before a single byte of video is pulled**.
- Returns `source_id`, `project`, `duration`, `transcript_kind` (`json3`/`vtt`), `title`,
  `chunk_count`, `download_job_id`, `video_ready`.
- On bot challenge / throttle: `success=False`, `hint` → set `cookies_path` or proxy env.
- Never returns the transcript body.

### `read_transcript_chunk`
```
read_transcript_chunk(source_id: str, index: int) -> dict
"""Read one overlapping transcript window. Bounded, with timing."""
```
- Annotations: RO=True, D=False, I=True, OW=False
- Returns one window (default 10 min, 2 min look-back overlap): segments with
  `start`/`end`, `index`, `has_more`, `token_estimate`. Constrained-mode lowers window size.

### `sample_frames`
```
sample_frames(source_id: str, start: float, end: float, fps: float = 1.0) -> dict
"""Sample capped, downscaled frames in a span for vision review."""
```
- Annotations: RO=True, D=False, I=True, OW=False
- For the agent's *semantic* vision pass only (visual moments, speaker disambiguation).
- Enforces a hard per-job frame budget; `fps` clamped; frames downscaled (~512px).
- Returns frame references/paths + count, never raw video. Flags `truncated` if capped.

### `get_job`
```
get_job(job_id: str) -> dict
"""Read render job status, progress, and output path."""
```
- Annotations: RO=True, D=False, I=True, OW=False
- Covers **both** job kinds — `kind` is `fetch` (source download) or `render` (encode).
- Returns `status` (`queued`/`running`/`done`/`failed`), `elapsed_seconds`, `progress`,
  `output_path` when done, `error`+`hint` when failed.

### `list_library`
```
list_library(project: str = "") -> dict
"""List projects, or one project's sources, clips, and exports."""
```
- Annotations: RO=True, D=False, I=True, OW=False
- No argument → every project with counts. With a project → its sources, clips, and
  exports (including published links and whether each source's video is still on disk).
- Reads the **files**, not the database. A project dropped in from a backup shows up.

---

## Tier 2 — structured

### `add_candidates`
```
add_candidates(source_id: str, candidates: list[dict]) -> dict
"""Persist agent-selected clip candidates. Dedups overlaps."""
```
- Annotations: RO=False, D=False, I=False, OW=False
- `candidates` op-array (STANDARDS §13 shape): each `{start, end, label, score, reason, cues}`.
- Validates spans; **dedup/merge** >50% overlap keeping union boundaries + max score.
- Soft-flags candidates opening on a bare pronoun (cold-open warning). Max 50 per call.
- Returns stored count, merged count, and any warnings.

### `plan_clips`
```
plan_clips(source_id: str, mode: str = "auto") -> dict
"""Group candidates into clip definitions by label/topic."""
```
- Annotations: RO=False, D=False, I=False, OW=False
- `mode`: `auto` `by_label` `by_topic` `montage` `supercut`.
- Deterministic grouping over stored candidates → clip definitions (member candidate ids,
  target label, reframe strategy). The agent can override membership in a follow-up call.
- Returns `clip_id`s and their composition. No encoding happens here.

---

## Tier 3 — render / export

### `render_clip` — async
```
render_clip(clip_id: str, reframe: str = "speaker", captions: bool = True) -> dict
"""Enqueue render: trim, reframe, caption, thumbnail. Returns job id."""
```
- Annotations: RO=False, D=False, I=False, OW=False
- `reframe`: `speaker` `center` `stacked`. **Enqueues a job and returns immediately** with
  `job_id` — never encodes inline. Single worker drains the queue (encode concurrency = 1).
- Worker steps: silence-trim → cut → assemble → reframe (cached MediaPipe crop) → ASS
  caption burn-in → thumbnail → write `output_path`.
- Agent polls `get_job(job_id)` for completion.

### `publish_outputs`
```
publish_outputs(job_ids: list[str], ttl_hours: int = 168) -> dict
"""Move clips to served dir. Returns links + verifiable summary."""
```
- Annotations: RO=False, D=False, I=True, OW=False
- Moves done clips + thumbnails to the served directory under unguessable paths.
- Builds `manifest.json` + a summary mapping each clip to `source_url`, `source_start`,
  `source_end`, label, and link. Sets retention TTL.
- Returns the list of links and the summary (see [`OUTPUT_CONTRACT.md`](OUTPUT_CONTRACT.md)).

---

## Why these 8 (and not the §9 four-tool loop)

A video pipeline is not a CRUD edit loop, so LOCATE→INSPECT→PATCH→VERIFY is replaced by a
pipeline shape. The standard's spirit is preserved:

- **Reads separated from actions:** `fetch_source`, `read_transcript_chunk`, `sample_frames`,
  `get_job` are read/inspect; the rest act.
- **Surgical returns:** metadata, one bounded window, capped frames, status — never bulk
  transcript or raw video.
- **A verify step:** `get_job` is the VERIFY analog; the agent confirms render success before
  `publish_outputs`.
- **The agent is the intelligence:** scoring and labeling come in as arguments to
  `add_candidates`; no tool reasons about content.

Tool count is 9 — under the §8 ceiling of 10. Discovery over the library costs exactly one
tool: `list_library`. Project *creation* stays implicit (`fetch_source(url, project="ep42")`
scaffolds it), so adding the library did not cost a second tool. New clip *types* are added as labels + skills
(data), not new tools, so the surface stays fixed as the engine generalizes.
