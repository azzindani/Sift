"""Domain router. Zero MCP imports — this module knows nothing about the protocol.

Every tool body in ``server.py`` is one line that calls into here. That split is
what makes the pipeline testable without a client, and it is why the eight public
functions below are the real API surface.

Two invariants hold across all of them:

* **No exception escapes.** ``@guard`` catches everything and converts it into an
  error dict carrying an actionable ``hint``. A tool that raises is a tool that
  ends an agent's run; a tool that returns a hint is one the agent can recover from.
* **Every return is a dict** with ``success`` first, a ``progress`` array, and a
  ``token_estimate`` — built only by ``ok_result`` / ``error_result``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from _clip_fetch import FetchError, get_source, load_transcript
from _clip_fetch import all_sources as _all_sources
from _clip_fetch import prepare_source as _prepare_source
from _clip_helpers import (
    LABELS,
    PLAN_MODES,
    REFRAME_MODES,
    error_result,
    get_max_candidates_returned,
    get_max_segments,
    get_overlap_s,
    get_window_s,
    hhmmss,
    init_state,
    ok_result,
)
from _clip_library import (
    DEFAULT_PROJECT,
    LibraryError,
    all_clips,
    ensure_project,
    list_projects,
    rebuild_index,
    validate_project,
)
from _clip_publish import PublishError
from _clip_publish import list_exports as _list_exports
from _clip_publish import publish as _publish
from _clip_queue import (
    QueueError,
    enqueue,
    enqueue_fetch,
    ensure_worker,
    pending_fetch,
    queue_depth,
    reconcile,
)
from _clip_queue import get_job as _get_job
from _clip_render import RenderError
from _clip_select import SelectError
from _clip_select import add_candidates as _add_candidates
from _clip_select import plan_clips as _plan_clips
from _clip_select import sample_frames as _sample_frames
from _clip_transcript import chunk_bounds, chunk_count, slice_segments
from shared.file_utils import PathError
from shared.platform_utils import BinaryMissing
from shared.progress import info, ok, warn

log = logging.getLogger("clipper.engine")

# Failures we raise on purpose. Each carries the hint that unblocks the caller.
_EXPECTED = (
    FetchError,
    SelectError,
    QueueError,
    PublishError,
    RenderError,
    LibraryError,
    PathError,
)


def guard(op: str) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Turn any failure into an error dict. The one place exceptions stop."""

    def decorate(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                init_state()
                return fn(*args, **kwargs)
            except _EXPECTED as exc:
                return error_result(exc.message, exc.hint)
            except BinaryMissing as exc:
                return error_result(str(exc), exc.hint)
            except Exception as exc:  # noqa: BLE001 - the contract is: never raise
                log.exception("unhandled error in %s", op)
                return error_result(
                    f"Unexpected error in {op}: {type(exc).__name__}: {exc}",
                    "This is a bug. Check the server log on stderr; retry the call.",
                )

        return wrapper

    return decorate


def _require_source(source_id: str) -> dict[str, Any]:
    source = get_source(source_id)
    if not source:
        raise FetchError(
            f"Unknown source_id: {source_id}",
            "Call fetch_source(url) first and use the source_id it returns.",
        )
    return source


def startup() -> dict[str, Any]:
    """Initialise state, reap interrupted jobs, start the worker. Called once by the server.

    The index is re-derived from the library on every boot. That is what lets you delete
    sift.db, restore a projects/ backup, or drop a project directory in by hand, and have
    the server simply pick it up.
    """
    init_state()
    ensure_project(DEFAULT_PROJECT)
    indexed = rebuild_index()
    reaped = reconcile()
    ensure_worker()
    return {"reconciled_jobs": reaped, "indexed_entities": indexed}


# --------------------------------------------------------------------------
# Tier 1 — read / inspect
# --------------------------------------------------------------------------


@guard("fetch_source")
def fetch_source(
    url: str,
    max_height: int = 720,
    cookies_path: str = "",
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Fetch the transcript now; queue the video download. Metadata only, never the body.

    Split in two on purpose. The probe + transcript are seconds; the video is minutes, and
    an MCP client times out around 30 (STANDARDS §23). So this returns as soon as the
    transcript is readable, and the download runs on the same single worker that does the
    encoding. The agent reads and selects while the bytes arrive — the two overlap instead
    of stacking.
    """
    progress: list[str] = []
    project = validate_project(project)
    info(progress, f"fetching {url} into project '{project}'")

    source = _prepare_source(url, max_height=max_height, cookies_path=cookies_path, project=project)
    source_id = source["source_id"]
    duration = float(source["duration"])
    chunks = chunk_count(duration)

    reused = bool(source.get("reused"))
    if reused and source.get("video_present"):
        ok(progress, "source already in this project — reusing it (no re-download)")
        job_id = pending_fetch(source_id)
    elif reused:
        # The video was deleted at publish. Same source_id, same transcript, same
        # candidates, same clips — just pull the bytes back so a re-render can run.
        ok(progress, f"source {source_id} already here; its video was deleted at publish")
        job_id = pending_fetch(source_id) or enqueue_fetch(source_id, project, cookies_path)
        info(progress, f"re-downloading the video as {job_id} — renders will queue behind it")
    else:
        ok(progress, "probed, disk-guarded, and pulled the transcript")
        job_id = enqueue_fetch(source_id, project, cookies_path)
        info(progress, f"video download queued as {job_id} — read the transcript while it lands")

    kind = source["transcript_kind"]
    if kind == "json3":
        ok(progress, "word-level timing available — word-pop captions enabled")
    else:
        warn(progress, "cue-level timing only (vtt) — captions will be line-level, not word-pop")

    return ok_result(
        "fetch_source",
        progress,
        source_id=source_id,
        project=project,
        title=source["title"],
        duration_s=round(duration, 2),
        duration_hms=hhmmss(duration),
        transcript_kind=kind,
        chunk_count=chunks,
        chunk_window_s=get_window_s(),
        chunk_overlap_s=get_overlap_s(),
        reused=reused,
        download_job_id=job_id,
        video_ready=bool(source.get("local_path")),
        next_step=(
            f"read_transcript_chunk('{source_id}', 0) — {chunks} chunk(s). The video is "
            "downloading meanwhile; render_clip will queue behind it automatically."
        ),
    )


@guard("read_transcript_chunk")
def read_transcript_chunk(source_id: str, index: int) -> dict[str, Any]:
    """One bounded, overlapping transcript window with per-segment timing."""
    progress: list[str] = []
    source = _require_source(source_id)
    transcript = load_transcript(source_id)
    if not transcript:
        raise FetchError(
            f"No transcript stored for {source_id}",
            "Call fetch_source(url) again — the parsed transcript is missing.",
        )

    duration = float(source["duration"])
    total = chunk_count(duration)
    index = int(index)
    if index < 0 or index >= total:
        raise SelectError(
            f"Chunk index {index} is out of range (0..{total - 1})",
            f"This source has {total} chunk(s). Read index 0 through {total - 1}.",
        )

    start, end = chunk_bounds(index)
    end = min(end, duration)
    segments = slice_segments(transcript.get("segments") or [], start, end)

    cap = get_max_segments()
    truncated = len(segments) > cap
    returned = segments[:cap]

    info(progress, f"window {hhmmss(start)}–{hhmmss(end)} (chunk {index + 1}/{total})")
    if index > 0:
        info(progress, f"first {get_overlap_s():.0f}s overlap the previous chunk — lead-in context")
    if truncated:
        warn(progress, f"segment cap hit: showing {cap} of {len(segments)}")

    return ok_result(
        "read_transcript_chunk",
        progress,
        source_id=source_id,
        index=index,
        window_start_s=round(start, 2),
        window_end_s=round(end, 2),
        segments=[
            {"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]}
            for s in returned
        ],
        returned=len(returned),
        total_available=len(segments),
        truncated=truncated,
        has_more=index + 1 < total,
        total_chunks=total,
        transcript_kind=source["transcript_kind"],
        next_step=(
            f"read_transcript_chunk('{source_id}', {index + 1})"
            if index + 1 < total
            else f"add_candidates('{source_id}', [...]) — you have read the whole transcript."
        ),
    )


@guard("sample_frames")
def sample_frames(source_id: str, start: float, end: float, fps: float = 1.0) -> dict[str, Any]:
    """Capped, downscaled frames plus the cheap audio/text cues for the same span."""
    progress: list[str] = []
    source = _require_source(source_id)
    transcript = load_transcript(source_id)

    result = _sample_frames(
        source, float(start), float(end), float(fps), transcript.get("segments") or []
    )

    ok(progress, f"sampled {result['returned']} frame(s) from {hhmmss(start)}–{hhmmss(end)}")
    info(
        progress,
        f"vision budget: {result['budget_used']}/{result['budget_total']} frames used",
    )
    if result["truncated"]:
        warn(
            progress,
            f"frame budget capped this call at {result['returned']} of {result['total_available']}",
        )

    cues = result["cues"]
    if cues["two_cue_agreement"]:
        ok(progress, "two cues agree (audio spike + transcript marker) — likely a real moment")
    elif cues["audio_spikes"] or cues["transcript_markers"]:
        info(progress, "only one cue fired — treat a visual-only pick with suspicion")

    return ok_result(
        "sample_frames",
        progress,
        source_id=source_id,
        frames=result["frames"],
        returned=result["returned"],
        total_available=result["total_available"],
        truncated=result["truncated"],
        budget_used=result["budget_used"],
        budget_total=result["budget_total"],
        cues=cues,
        next_step=(
            "Review the frames, then add_candidates(...) with "
            'cues={"vision_confirmed": true} if the moment is real.'
        ),
    )


@guard("get_job")
def get_job(job_id: str) -> dict[str, Any]:
    """Render job status, progress, and output path."""
    progress: list[str] = []
    job = _get_job(job_id)
    if not job:
        raise QueueError(
            f"Unknown job_id: {job_id}", "Use a job_id returned by render_clip(clip_id)."
        )

    status = job["status"]
    kind = job.get("kind", "render")
    fields: dict[str, Any] = {
        "job_id": job_id,
        "kind": kind,
        "clip_id": job["clip_id"],
        "source_id": job["source_id"],
        "status": status,
        "elapsed_seconds": job["elapsed_seconds"],
        "steps": job["progress"],
    }

    if status == "done" and kind == "fetch":
        ok(progress, f"source video downloaded in {job['elapsed_seconds']:.0f}s")
        fields["next_step"] = (
            "The video is on disk. render_clip(clip_id) will encode without waiting."
        )
    elif status == "done":
        ok(progress, f"render finished in {job['elapsed_seconds']:.0f}s")
        fields["output_path"] = job["output_path"]
        fields["duration_s"] = round(float(job.get("duration") or 0), 2)
        fields["next_step"] = f"publish_outputs(['{job_id}']) to get a shareable link."
    elif status == "failed":
        return error_result(
            job["error"] or "Render failed.",
            job["hint"] or "Re-queue with render_clip(clip_id).",
            progress,
            **fields,
        )
    else:
        depth = queue_depth()
        info(progress, f"{kind} job is {status}; {depth} job(s) queued or running")
        fields["queue_depth"] = depth
        fields["next_step"] = f"Poll get_job('{job_id}') again in ~15s."

    return ok_result("get_job", progress, **fields)


# --------------------------------------------------------------------------
# Tier 2 — structured
# --------------------------------------------------------------------------


@guard("add_candidates")
def add_candidates(source_id: str, candidates: list[dict]) -> dict[str, Any]:
    """Validate and persist the agent's picks. Merges >50% overlaps."""
    progress: list[str] = []
    source = _require_source(source_id)
    transcript = load_transcript(source_id)

    result = _add_candidates(
        source_id,
        candidates,
        float(source["duration"]),
        transcript.get("segments") or [],
    )

    ok(progress, f"validated and stored {result['submitted']} candidate(s)")
    if result["merged"]:
        info(
            progress,
            f"merged {result['merged']} overlapping pair(s) — union boundaries, max score kept",
        )
    for warning in result["warnings"]:
        warn(progress, warning)

    cap = get_max_candidates_returned()
    stored = result["candidates"]

    return ok_result(
        "add_candidates",
        progress,
        source_id=source_id,
        stored=result["stored"],
        submitted=result["submitted"],
        merged=result["merged"],
        warnings=result["warnings"],
        candidates=[
            {
                "candidate_id": c["candidate_id"],
                "start": c["start"],
                "end": c["end"],
                "label": c["label"],
                "score": c["score"],
            }
            for c in stored[:cap]
        ],
        returned=min(len(stored), cap),
        total_available=len(stored),
        truncated=len(stored) > cap,
        next_step=f"plan_clips('{source_id}') to group these into clip definitions.",
    )


@guard("plan_clips")
def plan_clips(source_id: str, mode: str = "auto") -> dict[str, Any]:
    """Deterministically group stored candidates into clip definitions."""
    progress: list[str] = []
    source = _require_source(source_id)

    clips = _plan_clips(source_id, mode, source.get("project", DEFAULT_PROJECT))
    ok(progress, f"planned {len(clips)} clip(s) with mode={mode!r}")
    info(progress, "no encoding happened — call render_clip(clip_id) to queue each one")

    return ok_result(
        "plan_clips",
        progress,
        source_id=source_id,
        mode=mode,
        clips=[
            {
                "clip_id": clip["clip_id"],
                "label": clip["label"],
                "members": len(clip["members"]),
                "est_duration_s": clip["est_duration_s"],
                "source_start": clip["source_start"],
                "source_end": clip["source_end"],
                "score": clip["score"],
                "reframe": clip["spec"]["reframe"],
                "reason": clip["reason"],
                **({"phrase": clip["phrase"]} if "phrase" in clip else {}),
            }
            for clip in clips
        ],
        clip_count=len(clips),
        next_step=f"render_clip('{clips[0]['clip_id']}') — then poll get_job(job_id).",
    )


# --------------------------------------------------------------------------
# Tier 3 — render / export
# --------------------------------------------------------------------------


@guard("render_clip")
def render_clip(clip_id: str, reframe: str = "speaker", captions: bool = True) -> dict[str, Any]:
    """Enqueue a render job and return immediately. Never encodes inline."""
    progress: list[str] = []
    job_id = enqueue(clip_id, reframe=reframe, captions=bool(captions))
    depth = queue_depth()

    ok(progress, f"queued job {job_id} (reframe={reframe}, captions={bool(captions)})")
    info(progress, f"{depth} job(s) queued or running — one encode at a time on this box")
    if depth > 1:
        info(progress, "it may be queued behind the source download; the worker runs them in order")

    return ok_result(
        "render_clip",
        progress,
        job_id=job_id,
        clip_id=clip_id,
        status="queued",
        queue_depth=depth,
        next_step=f"Poll get_job('{job_id}') until status='done' (~15-60s per clip).",
    )


@guard("publish_outputs")
def publish_outputs(job_ids: list[str], ttl_hours: int = 168) -> dict[str, Any]:
    """Move finished clips to the served dir; return links + a verifiable summary."""
    progress: list[str] = []
    result = _publish(list(job_ids), ttl_hours=int(ttl_hours))

    ok(progress, f"published {len(result['links'])} clip(s) as batch {result['batch_id']}")
    if result["sources_deleted"]:
        info(
            progress,
            f"deleted {len(result['sources_deleted'])} source video(s) — clips and transcript remain",
        )
    if result["batches_expired"]:
        info(progress, f"swept {result['batches_expired']} expired batch(es)")
    ok(progress, "every clip deep-links to its exact source moment — selections are checkable")

    return ok_result(
        "publish_outputs",
        progress,
        batch_id=result["batch_id"],
        links=result["links"],
        gallery=result["gallery"],
        summary=result["summary"],
        clips=result["manifest"]["clips"],
        manifest_path=result["manifest_path"],
        expires_at=result["manifest"]["expires_at"],
        ttl_hours=result["manifest"]["ttl_hours"],
        sources_deleted=result["sources_deleted"],
        next_step="Present the summary. Each source_link jumps to the moment in the original.",
    )


# --------------------------------------------------------------------------
# Library — the ninth tool. Discovery over the file-backed project library.
# --------------------------------------------------------------------------


@guard("list_library")
def list_library(project: str = "") -> dict[str, Any]:
    """Projects, or one project's sources / clips / exports. Reads the files."""
    progress: list[str] = []

    if not project:
        projects = list_projects()
        ok(progress, f"{len(projects)} project(s) in the library")
        return ok_result(
            "list_library",
            progress,
            projects=projects,
            returned=len(projects),
            total_available=len(projects),
            truncated=False,
            next_step=(
                "list_library(project='<name>') to see its sources and clips, or "
                "fetch_source(url, project='<name>') to start a new one."
            ),
        )

    project = validate_project(project)
    ensure_project(project)

    sources = [
        {
            "source_id": s["source_id"],
            "title": s.get("title", ""),
            "duration_hms": hhmmss(float(s.get("duration") or 0)),
            "transcript_kind": s.get("transcript_kind", ""),
            "video_on_disk": bool(s.get("local_path")),
            "url": s.get("url", ""),
        }
        for s in _all_sources(project)
    ]
    clips = [
        {
            "clip_id": c["clip_id"],
            "label": c.get("label", ""),
            "source_id": c.get("source_id", ""),
            "members": len(c.get("members") or []),
            "rendered": bool(c.get("rendered")),
        }
        for c in all_clips(project)
    ]
    exports = [
        {
            "batch_id": e["batch_id"],
            "created_at": e.get("created_at", ""),
            "expires_at": e.get("expires_at", ""),
            "clips": len(e.get("clips") or []),
            "links": [c["link"] for c in e.get("clips") or []],
        }
        for e in _list_exports(project)
    ]

    ok(
        progress,
        f"project '{project}': {len(sources)} source(s), {len(clips)} clip(s), "
        f"{len(exports)} export(s)",
    )
    if any(not s["video_on_disk"] for s in sources):
        info(
            progress,
            "some sources have no video on disk — deleted after publish; re-fetch to re-cut",
        )

    return ok_result(
        "list_library",
        progress,
        project=project,
        sources=sources,
        clips=clips,
        exports=exports,
        returned=len(sources) + len(clips) + len(exports),
        total_available=len(sources) + len(clips) + len(exports),
        truncated=False,
        next_step="render_clip(clip_id) for an unrendered clip, or publish_outputs([job_id]).",
    )


# --------------------------------------------------------------------------
# Introspection (not a tool — used by the server banner and tests)
# --------------------------------------------------------------------------


def capabilities() -> dict[str, Any]:
    """What this server accepts. Kept beside the registries so it cannot drift."""
    return {
        "labels": list(LABELS),
        "plan_modes": list(PLAN_MODES),
        "reframe_modes": list(REFRAME_MODES),
    }
