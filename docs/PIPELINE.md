# PIPELINE.md — Clipper

Stage-by-stage technical design: what each step does, the exact tooling, the ffmpeg
filtergraphs, and the failure handling. This is the implementation reference for the
render path. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for the overall shape.

---

## Stage 1 — Fetch (`_clip_fetch.py`)

**Goal:** get the source video at a capped resolution and the transcript with the best
available timing, without filling the disk and without re-downloading a known source.

- **Disk guard first.** Estimate source size from yt-dlp metadata; check free space with
  a safety margin. If insufficient → error dict with a `hint` to free space or lower the
  resolution cap. Never start a download you can't finish.
- **URL dedup.** If `sources` already has this URL with a live `local_path`, reuse it.
- **Resolution cap.** `yt-dlp -f "bv*[height<=720]+ba/b[height<=720]"` — never pull 1080p+.
- **Transcript, word-level preferred.** Request `--write-auto-subs --sub-format json3`.
  json3 carries per-word timing. If only VTT is available, store `transcript_kind="vtt"`
  (cue-level). If neither, `transcript_kind="none"`.
- **Subprocess safety:** argument list, `shell=False`, `timeout`, `capture_output=True`.
- **Bot-challenge handling:** if yt-dlp reports a sign-in/bot challenge, return
  `success=False` with a `hint` pointing at the cookies-file / proxy env config. This is
  an *expected* path on VPS IPs, not an exception.

Returns metadata only — `source_id`, `duration`, `transcript_kind`, title — never the
transcript body (surgical read, STANDARDS §10).

---

## Stage 2 — Chunk the transcript (`_clip_transcript.py`)

**Goal:** let the agent read the whole transcript in bounded, overlapping windows so it
never misses a boundary-straddling moment and never sees referential dead-ends.

- **Overlapping windows.** Default window 10 min, look-back 2 min: `0–10, 8–18, 16–26, …`.
  The 2-min overlap is the fix for context loss across boundaries.
- **`read_transcript_chunk(source_id, index)`** returns exactly one window: the text plus
  per-segment `start`/`end`, a `has_more` flag, and `token_estimate`. Bounded by the
  constrained-mode limits.
- **Word-timing parse.** json3 → list of `{word, start, end}`. This feeds both cut-point
  precision and ASS caption generation. VTT → cue-level `{text, start, end}` only.

---

## Stage 3 — Candidate selection (agent) → persistence (`_clip_select.py`)

**The agent scores; the server stores.** The agent submits its picks; the tool validates
and persists. No model runs in the tool.

`add_candidates(source_id, candidates)` where each candidate is:

```json
{
  "start": 2710.4,
  "end": 2755.0,
  "label": "argument",
  "score": 8,
  "reason": "clean disagreement, lands without setup",
  "cues": {"text": true, "audio_spike": false, "vision_confirmed": false}
}
```

Server-side, deterministically:

- **Validate** spans against source duration; reject inverted/out-of-range with a `hint`.
- **Dedup/merge** overlapping candidates (>50% overlap): keep the **union of boundaries**
  and the **max score**, not a blind discard — so the better framing is never lost.
- **The cold-open rule** is enforced in the *skill* the agent follows (see
  [`LABELS_AND_SKILLS.md`](LABELS_AND_SKILLS.md)), not the engine: the agent must verify the
  first sentence is intelligible with no prior context, extending `start` if not. The engine
  can flag candidates whose first word is a bare pronoun as a soft warning in the response.

---

## Stage 4 — The cheap-cue → vision funnel

**Goal:** catch visual/audio moments (laughter, reactions, gags) the transcript misses,
without paying to watch hours of footage.

1. **Cheap cues, free, on-box:**
   - Transcript markers: `[laughter]`, `[applause]`, overlapping/short bursts.
   - Audio energy: invert `silencedetect` + volume spikes to flag excitement/laughter.
2. **Two-cue rule:** a non-text moment is only promoted to a candidate region if **two
   cheap cues agree** (e.g. audio spike + transcript marker). Kills false positives from
   mic bumps and music stings.
3. **Vision only on flagged regions:** `sample_frames(span, fps, max_frames)` extracts a
   capped, downscaled frame set for the agent's vision pass. Hard per-job frame budget
   enforced in the engine. The agent confirms "is something visually happening here."

**Geometry vs. semantics — two different tools:**
- **Geometry** (where is the face → crop box) is **MediaPipe, local, deterministic**, run
  inside the render step. Cheap, no API.
- **Semantics** (is this funny / who is the active speaker) is the **agent's vision pass**
  on sampled frames. Sparse, capped, API.

---

## Stage 5 — Boundary refinement + silence trim (`_clip_render.py`, pre-encode)

- **Snap to silence.** Run `silencedetect` on the chosen span; move cut points to the
  nearest silence so you never cut mid-word. Keep ~150 ms padding around speech.
- **Dead-air trim (the "multi-cut" the user means).** Within one span, drop silence
  segments above a duration threshold and concat the speech segments. This is signal
  processing, not a judgment call — fully deterministic.

```
# silence map
ffmpeg -i in.mp4 -af silencedetect=noise=-30dB:d=0.5 -f null -
# → parse start/end of silences from stderr, invert to keep-segments,
#   pad each by 150ms, build the trim/concat filtergraph
```

---

## Stage 6 — Cut & assemble

**Single contiguous cut (default, fast):**
```
ffmpeg -ss <start> -to <end> -i in.mp4 -c copy out_seg.mp4    # stream-copy, near-instant
```

**Multi-segment assemble (dead-air removal, or v2 distant splice):**
```
# build a concat filtergraph; crossfade audio at joins to avoid pops, xfade video
[0:a][1:a]acrossfade=d=0.08[a]; [0:v][1:v]xfade=transition=fade:duration=0.15[v]
```
v1 restricts multi-cut to removing dead air *inside* one span. Distant-moment splicing is
v2 and gated behind a coherence check.

---

## Stage 7 — Reframe 16:9 → 9:16 (`_clip_render.py`)

- **MediaPipe face detection** on frames sampled at ~2–5 fps → bounding boxes.
- **Smooth** the box center over time (moving average / Kalman) so the crop doesn't jitter;
  cap pan speed so it glides rather than snaps.
- **Interpolate** between sampled frames — do not detect every frame.
- **Crop coords computed at selection/plan time and cached in the clip row**, so the encode
  step is pure ffmpeg with no ML in the hot loop.

```
# apply a time-segmented smoothed crop, then scale to 1080x1920
crop=w=ih*9/16:h=ih:x='<smoothed_x_expr>':y=0, scale=1080:1920
```

- **Two-speaker moments:** the `argument`/`interview` skill may request a **stacked layout**
  (two stacked 16:9 crops) instead of speaker-follow, so you don't lose a reaction shot.

---

## Stage 8 — Captions (`_clip_transcript.py` → ASS, burned in at encode)

**Reuse the transcript — no re-transcription.** The words and timing already exist; ASS
generation is a mechanical reformat of the same data into a styled subtitle file.

- **json3 (word-level) → word-pop ASS:** per-word timing drives karaoke-style highlight.
- **VTT (cue-level) → line ASS:** styled lines, no per-word pop (the data doesn't support it).
- **One hardcoded style template** (font, size, outline, position, highlight color). Templating
  is the efficient path: write the style once, every clip reuses it, less CPU and a consistent look.
- **Bundled font.** Reference `assets/fonts/<font>.ttf` via ffmpeg `fontsdir` + the ASS
  `fontname`. Never rely on system fonts — they're non-deterministic across hosts.

```
ffmpeg -i clip.mp4 -vf "subtitles=cap.ass:fontsdir=assets/fonts" -c:a copy \
  -c:v libx264 -preset veryfast -threads 2 captioned.mp4
```

---

## Stage 9 — Thumbnail (`_clip_render.py`)

- Extract a frame at the clip's hook moment: `ffmpeg -ss <hook> -frames:v 1`.
- Optionally let MediaPipe pick the sampled frame with the clearest face.
- Composite title/branding with **Pillow** (light) — load, draw, save. Sub-second spike.

---

## Stage 10 — Encode discipline

- **Re-encode only the short clip**, never the source.
- `-preset veryfast`, `-threads 2`, `-crf` tuned for short-form. ~15–60 s wall time for a
  60 s 720p clip on 2 weak vCPUs.
- **Encode concurrency = 1**, enforced by the single render worker draining the SQLite queue.
- Async tool (STANDARDS §23): `render_clip` enqueues and returns a `job_id`; `get_job` reports
  `status`, `elapsed_seconds`, and intermediate progress.

---

## Stage 11 — Publish

Move finished clips + thumbnails to the served directory; build the manifest and the
verifiable summary. Fully specified in [`OUTPUT_CONTRACT.md`](OUTPUT_CONTRACT.md).

---

## Filtergraph summary (one pass where possible)

For a captioned, reframed, dead-air-trimmed single clip, chain in one encode to avoid
re-encoding twice:

```
ffmpeg -i in.mp4 -filter_complex "
  [0:v]trim=...,setpts=...[v0]; ... ; concat=n=K:v=1:a=0[vc];
  [vc]crop=ih*9/16:ih:'<x>':0,scale=1080:1920[vr];
  [vr]subtitles=cap.ass:fontsdir=assets/fonts[vout];
  [0:a]atrim=...,asetpts=...[a0]; ... ; concat=n=K:v=0:a=1[aout]
" -map "[vout]" -map "[aout]" -c:v libx264 -preset veryfast -threads 2 out.mp4
```

Build the graph programmatically from the cached segment list + crop expression + ASS path.
Keep it to a single encode; only fall back to multi-pass if the graph gets unwieldy.
