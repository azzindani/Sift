"""The SQLite job queue and the single render worker.

**One worker thread drains the queue.** That sentence is the entire concurrency
model, and it is the reason a 2-vCPU box survives this workload: two ffmpeg
encodes can never contend for the same two cores, because there is only ever one.
Enqueue is instant; the encode happens later; the agent polls ``get_job``.

**Restart reconciliation.** A job marked ``running`` when the process dies is a
lie — nothing is running it. On startup every such job is failed with a hint and
its temp dir is swept, so a crash can never leave a job that polls forever or a
temp dir that leaks disk.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from _clip_fetch import FetchError, download_video, get_source, load_transcript
from _clip_helpers import (
    REFRAME_MODES,
    connect,
    jobs_dir,
    new_id,
    now_ts,
    receipt_path,
    row_to_dict,
)
from _clip_render import (
    RenderError,
    _crop_width,
    build_ass,
    build_filtergraph,
    build_timeline,
    clamp_dimensions,
    detect_faces,
    encode,
    estimate_crf,
    extract_segments,
    make_thumbnail,
    probe_duration,
    smooth_centers,
    summarize_segments,
    trimmed_seconds,
    two_speaker_columns,
)
from _clip_select import get_clip
from shared.file_utils import safe_mkdir, sweep_dir
from shared.progress import step
from shared.receipt import append_receipt

log = logging.getLogger("clipper.queue")

_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_wake = threading.Event()
_shutdown = threading.Event()

POLL_INTERVAL_S = 1.0
OUT_LABEL = "1080x1920"


class QueueError(Exception):
    """An enqueue was rejected. Carries an actionable hint."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------
# Job rows
# --------------------------------------------------------------------------


def get_job(job_id: str) -> dict[str, Any]:
    """Load a job row with its progress decoded. {} if unknown."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return {}
    job = row_to_dict(row)
    job["progress"] = json.loads(job.get("progress") or "[]")
    job["options"] = json.loads(job.get("options") or "{}")

    if job["status"] == "running" and job["started_at"]:
        job["elapsed_seconds"] = round(now_ts() - job["started_at"], 1)
    elif job["finished_at"] and job["started_at"]:
        job["elapsed_seconds"] = round(job["finished_at"] - job["started_at"], 1)
    else:
        job["elapsed_seconds"] = 0.0
    return job


def _set_progress(job_id: str, entries: list[str]) -> None:
    with connect() as conn:
        conn.execute("UPDATE jobs SET progress = ? WHERE job_id = ?", (json.dumps(entries), job_id))


def _fail(job_id: str, error: str, hint: str, entries: list[str]) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE jobs SET status='failed', error=?, hint=?, progress=?, finished_at=?
               WHERE job_id = ?""",
            (error, hint, json.dumps(entries), now_ts(), job_id),
        )


def enqueue_fetch(source_id: str, project: str, cookies_path: str = "") -> str:
    """Queue the source-video download. The slow half of a fetch; never runs inline."""
    job_id = new_id("j")
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, kind, source_id, project, status, progress,
                   options, created_at)
               VALUES (?, 'fetch', ?, ?, 'queued', '[]', ?, ?)""",
            (job_id, source_id, project, json.dumps({"cookies_path": cookies_path}), now_ts()),
        )
    ensure_worker()
    _wake.set()
    return job_id


def pending_fetch(source_id: str) -> str:
    """The job_id of an in-flight download for this source, or "" if none."""
    with connect() as conn:
        row = conn.execute(
            """SELECT job_id FROM jobs WHERE kind='fetch' AND source_id=?
               AND status IN ('queued','running') ORDER BY created_at LIMIT 1""",
            (source_id,),
        ).fetchone()
    return str(row["job_id"]) if row else ""


def enqueue(clip_id: str, reframe: str = "speaker", captions: bool = True) -> str:
    """Write a queued job row and wake the worker. Never encodes inline."""
    if reframe not in REFRAME_MODES:
        raise QueueError(f"Unknown reframe {reframe!r}", f"Use one of: {' '.join(REFRAME_MODES)}")

    clip = get_clip(clip_id)
    if not clip:
        raise QueueError(
            f"Unknown clip_id: {clip_id}", "Call plan_clips(source_id) and use a returned clip_id."
        )
    if not clip.get("member_rows"):
        raise QueueError(
            f"Clip {clip_id} has no member candidates",
            "Re-run plan_clips(source_id); its candidates may have been replaced.",
        )

    source = get_source(clip["source_id"])
    local = source.get("local_path") or ""
    on_disk = bool(local) and Path(local).is_file()

    # The video may simply not have landed yet. One worker drains the queue in order, so a
    # render queued behind its own download finds the video waiting by the time it runs.
    if not on_disk and not pending_fetch(clip["source_id"]):
        raise QueueError(
            f"Source video for {clip['source_id']} is not on disk and no download is queued",
            "Call fetch_source(url) again — the video is deleted after publish_outputs.",
        )

    job_id = new_id("j")
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, kind, clip_id, source_id, project, status, progress,
                   options, created_at)
               VALUES (?, 'render', ?, ?, ?, 'queued', '[]', ?, ?)""",
            (
                job_id,
                clip_id,
                clip["source_id"],
                clip.get("project", "default"),
                json.dumps({"reframe": reframe, "captions": bool(captions)}),
                now_ts(),
            ),
        )

    ensure_worker()
    _wake.set()
    return job_id


def queue_depth() -> int:
    """Jobs waiting or in flight — what the agent's ETA depends on."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------
# The render worker
# --------------------------------------------------------------------------


def _claim_next() -> dict[str, Any]:
    """Atomically take the oldest queued job. {} when the queue is empty."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return {}
            job_id = row["job_id"]
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE job_id=?",
                (now_ts(), job_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_job(job_id)


def _render(job: dict[str, Any]) -> None:
    """Run one job end to end. Raises RenderError on an expected failure."""
    job_id = job["job_id"]
    entries: list[str] = []

    def mark(message: str) -> None:
        step(entries, message)
        _set_progress(job_id, entries)

    clip = get_clip(job["clip_id"])
    source = get_source(job["source_id"])
    members = clip["member_rows"]
    spec = clip["spec"]
    options = job["options"]
    reframe = options.get("reframe", "speaker")
    captions = options.get("captions", True)

    local = source.get("local_path") or ""
    video = Path(local) if local else None
    if video is None or not video.is_file():
        # Nearly always: the download job this render was queued behind failed. Say *that*,
        # with its actual reason — "the video vanished" is true but useless.
        reason = _failed_fetch_reason(job["source_id"])
        if reason:
            raise RenderError(
                f"The source video was never downloaded: {reason}",
                "Fix the cause, then call fetch_source(url) again and re-run render_clip.",
            )
        raise RenderError(
            f"Source video for {job['source_id']} is not on disk",
            "It is deleted after publish_outputs. Call fetch_source(url) again, then "
            "re-run render_clip.",
        )

    temp = safe_mkdir(jobs_dir() / job_id)
    with connect() as conn:
        conn.execute("UPDATE jobs SET temp_dir=? WHERE job_id=?", (str(temp), job_id))

    mark(f"trimming silence across {len(members)} span(s)")
    timeline = build_timeline(video, members, spec)
    dropped = trimmed_seconds(members, timeline)
    mark(f"kept {timeline.duration:.1f}s, dropped {dropped:.1f}s of dead air")

    src_w, src_h = clamp_dimensions(int(source.get("width") or 0), int(source.get("height") or 0))
    crop_w = _crop_width(src_w, src_h)

    keypoints: list[tuple[float, float]] = []
    columns: tuple[float, float] | None = None
    effective_reframe = reframe

    if reframe in {"speaker", "stacked"}:
        mark(f"detecting faces for {reframe} reframe")
        samples = detect_faces(video, timeline, temp)
        if not samples:
            effective_reframe = "center"
            mark("no face data (MediaPipe absent or no face found) — using a centred crop")
        elif reframe == "stacked":
            columns = two_speaker_columns(samples, src_w, crop_w)
            if columns is None:
                effective_reframe = "speaker"
                keypoints = smooth_centers(samples, src_w, crop_w, timeline)
                mark("only one speaker on screen — falling back to speaker-follow")
            else:
                mark("two speakers detected — stacked two-shot layout")
        else:
            keypoints = smooth_centers(samples, src_w, crop_w, timeline)
            mark(f"smoothed {len(keypoints)} crop keypoints from {len(samples)} face samples")

    ass_file = ""
    if captions:
        transcript = load_transcript(job["source_id"])
        if not transcript:
            mark("no transcript on disk — rendering without captions")
        else:
            ass_file = build_ass(transcript, timeline, spec, temp)
            kind = "word-pop" if transcript.get("has_words") else "cue-level"
            mark(f"generated {kind} captions" if ass_file else "no words in span — no captions")

    # Cut the segments out first, with input seeking. This is what keeps a 4-hour source
    # from being decoded end to end, and what makes the assembly graph immune to a source
    # that changes resolution mid-stream.
    mark(f"cutting {len(timeline.segments)} segment(s) from the source")
    intermediates = extract_segments(video, timeline, src_w, src_h, temp)

    mark(f"encoding {timeline.duration:.1f}s at {OUT_LABEL}")
    output = temp / "clip.mp4"
    filtergraph = build_filtergraph(
        timeline, src_w, src_h, effective_reframe, keypoints, columns, ass_file
    )
    (temp / "filtergraph.txt").write_text(filtergraph, encoding="utf-8")
    encode(intermediates, temp, filtergraph, output, crf=estimate_crf(timeline.duration))

    for path in intermediates:  # the intermediates are large and no longer needed
        path.unlink(missing_ok=True)

    duration = probe_duration(output) or timeline.duration
    thumb = temp / "clip.jpg"
    if make_thumbnail(output, thumb, label=clip["label"]):
        mark("thumbnail written")
    else:
        thumb = Path("")
        mark("thumbnail extraction failed (clip is fine)")

    mark(f"done — {duration:.1f}s clip")

    with connect() as conn:
        conn.execute(
            """UPDATE jobs SET status='done', output_path=?, thumb_path=?, duration=?,
                   segments=?, progress=?, finished_at=? WHERE job_id=?""",
            (
                str(output),
                str(thumb) if thumb else "",
                duration,
                json.dumps(summarize_segments(timeline)),
                json.dumps(entries),
                now_ts(),
                job_id,
            ),
        )

    append_receipt(
        receipt_path(),
        "render_clip",
        {
            "job_id": job_id,
            "clip_id": job["clip_id"],
            "reframe": effective_reframe,
            "captions": bool(ass_file),
        },
        f"rendered {duration:.1f}s to {output} (dropped {dropped:.1f}s dead air)",
    )
    log.info("job %s done: %s (%.1fs)", job_id, output, duration)


OUT_LABEL = "1080x1920"


def _failed_fetch_reason(source_id: str) -> str:
    """The error from this source's most recent failed download job, if there is one."""
    with connect() as conn:
        row = conn.execute(
            """SELECT error FROM jobs WHERE kind='fetch' AND source_id=? AND status='failed'
               ORDER BY finished_at DESC LIMIT 1""",
            (source_id,),
        ).fetchone()
    return str(row["error"]) if row and row["error"] else ""


def _fetch(job: dict[str, Any]) -> None:
    """Run one download job. Raises FetchError on an expected failure."""
    job_id = job["job_id"]
    entries: list[str] = []

    def mark(message: str) -> None:
        step(entries, message)
        _set_progress(job_id, entries)

    source = get_source(job["source_id"])
    mark(f"downloading '{source.get('title', '')}' at <={source.get('max_height', 720)}p")

    record = download_video(job["source_id"], job["options"].get("cookies_path", ""))
    size = Path(record["local_path"]).stat().st_size if record.get("local_path") else 0
    mark(f"downloaded {size / 1e6:.0f} MB ({record['width']}x{record['height']})")

    with connect() as conn:
        conn.execute(
            """UPDATE jobs SET status='done', output_path=?, progress=?, finished_at=?
               WHERE job_id=?""",
            (record.get("local_path", ""), json.dumps(entries), now_ts(), job_id),
        )

    append_receipt(
        receipt_path(),
        "fetch_source",
        {"job_id": job_id, "source_id": job["source_id"], "url": source.get("url", "")},
        f"downloaded {size / 1e6:.0f} MB to {record.get('local_path', '')}",
    )
    log.info("fetch job %s done: %s", job_id, record.get("local_path", ""))


def _worker_loop() -> None:
    """Drain the queue, one job at a time, forever. Never dies on a job failure."""
    log.info("render worker started")
    while not _shutdown.is_set():
        try:
            job = _claim_next()
        except Exception as exc:  # noqa: BLE001 - a DB hiccup must not kill the worker
            log.exception("could not claim a job: %s", exc)
            _wake.wait(timeout=POLL_INTERVAL_S)
            _wake.clear()
            continue

        if not job:
            _wake.wait(timeout=POLL_INTERVAL_S)
            _wake.clear()
            continue

        job_id = job["job_id"]
        try:
            if job.get("kind") == "fetch":
                _fetch(job)
            else:
                _render(job)
        except FetchError as exc:
            log.warning("fetch job %s failed: %s", job_id, exc.message)
            _fail(job_id, exc.message, exc.hint, job.get("progress", []))
            append_receipt(
                receipt_path(), "fetch_source", {"job_id": job_id}, f"failed: {exc.message}"
            )
        except RenderError as exc:
            log.warning("job %s failed: %s", job_id, exc.message)
            _fail(job_id, exc.message, exc.hint, job.get("progress", []))
            append_receipt(
                receipt_path(), "render_clip", {"job_id": job_id}, f"failed: {exc.message}"
            )
        except Exception as exc:  # noqa: BLE001 - unexpected: still a failed job, not a dead worker
            log.exception("job %s crashed", job_id)
            _fail(
                job_id,
                f"Unexpected render error: {exc}",
                "Check the server log (stderr). Retry render_clip; if it repeats, "
                "try reframe='center' and captions=False to isolate the stage.",
                job.get("progress", []),
            )
            append_receipt(receipt_path(), "render_clip", {"job_id": job_id}, f"crashed: {exc}")


def ensure_worker() -> None:
    """Start the single render worker if it isn't already running. Idempotent."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _shutdown.clear()
        _worker = threading.Thread(target=_worker_loop, name="clipper-render", daemon=True)
        _worker.start()


def stop_worker(timeout: float = 5.0) -> None:
    """Signal the worker to stop. Used by tests and shutdown; never kills a live encode."""
    _shutdown.set()
    _wake.set()
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            _worker.join(timeout=timeout)


def reconcile() -> int:
    """Fail every interrupted job and sweep its temp dir. Returns how many were reaped.

    Called once at startup. A ``running`` row after a restart means the process died
    mid-encode — nothing is going to finish it, so leaving it running would strand the
    agent polling forever.
    """
    with connect() as conn:
        rows = conn.execute("SELECT job_id, temp_dir FROM jobs WHERE status='running'").fetchall()
        reaped = 0
        for row in rows:
            conn.execute(
                """UPDATE jobs SET status='failed',
                       error='Interrupted by a server restart mid-encode.',
                       hint='Call render_clip(clip_id) again to re-queue it.',
                       finished_at=? WHERE job_id=?""",
                (now_ts(), row["job_id"]),
            )
            if row["temp_dir"]:
                sweep_dir(row["temp_dir"])
            reaped += 1

    if reaped:
        log.warning("reconciled %d interrupted job(s) after restart", reaped)
    return reaped


def wait_for(job_id: str, timeout: float = 600.0) -> dict[str, Any]:
    """Block until a job leaves the queue. For tests and synchronous callers only."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job.get("status") in {"done", "failed"}:
            return job
        time.sleep(0.25)
    return get_job(job_id)


def job_segments(job_id: str) -> list[dict[str, float]]:
    """The source spans a finished job's clip was cut from (for the manifest's built_from).

    Read from the row the worker wrote, not recomputed: silence detection is
    expensive, and by publish time the source video is usually already deleted.
    """
    with connect() as conn:
        row = conn.execute("SELECT segments FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return []
    return json.loads(row["segments"] or "[]")
