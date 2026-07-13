"""Publish: served directory, manifest, the verifiable summary, TTL retention.

This is the terminal stage, and it is where the output contract is honoured:
**no clip is ever returned without its source mapping.** Every entry carries
``source_url``, ``source_start``, ``source_end``, a deep link that drops the user
at the exact moment in the original, and the candidate spans it was built from —
so a bad pick is falsifiable in one click rather than taken on trust.

Publishing is also when the source video dies. It has been cut; it is the largest
thing on a small disk; keeping it would be hoarding. The transcript survives, so
verification and re-captioning still work.
"""

from __future__ import annotations

import html
import logging
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from _clip_fetch import delete_source_video, get_source, has_live_jobs
from _clip_helpers import (
    base_url,
    hhmmss,
    now_iso,
    now_ts,
    receipt_path,
    serve_dir,
    ttl_hours_default,
)
from _clip_library import (
    all_exports,
    clip_dir,
    expired_exports,
    export_dir,
    new_batch_id,
    touch_project,
)
from _clip_queue import get_job, job_segments
from _clip_select import get_clip
from shared.file_utils import sweep_dir
from shared.receipt import append_receipt
from shared.version_control import atomic_write_json, atomic_write_text

log = logging.getLogger("clipper.publish")


class PublishError(Exception):
    """A publish was rejected. Carries an actionable hint."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def source_deep_link(url: str, start: float) -> str:
    """A link that lands on the exact source moment — the heart of verification."""
    seconds = max(0, int(start))
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()

    if "youtube.com" in host or "youtu.be" in host:
        separator = "&" if parsed.query else "?"
        return f"{url}{separator}t={seconds}s"
    if "vimeo.com" in host:
        return f"{url}#t={seconds}s"

    separator = "&" if parsed.query else "?"
    return f"{url}{separator}{urlencode({'t': seconds})}"


def _collect(job_ids: list[str]) -> list[dict[str, Any]]:
    """Load the finished jobs, refusing to publish anything that isn't done."""
    if not job_ids:
        raise PublishError(
            "job_ids is empty",
            "Pass the job_ids returned by render_clip, once get_job reports status='done'.",
        )

    jobs: list[dict[str, Any]] = []
    for job_id in job_ids:
        job = get_job(job_id)
        if not job:
            raise PublishError(f"Unknown job_id: {job_id}", "Use a job_id returned by render_clip.")
        if job["status"] != "done":
            raise PublishError(
                f"Job {job_id} is {job['status']}, not done"
                + (f": {job['error']}" if job.get("error") else ""),
                "Poll get_job(job_id) until status='done', then publish. Failed jobs must be "
                "re-queued with render_clip.",
            )
        output = Path(job.get("output_path") or "")
        if not output.is_file():
            raise PublishError(
                f"Job {job_id} is done but its clip is missing: {output}",
                "The temp dir was swept. Re-run render_clip(clip_id) for this clip.",
            )
        jobs.append(job)
    return jobs


def publish(job_ids: list[str], ttl_hours: int = 0) -> dict[str, Any]:
    """Move finished clips to the served dir; build the manifest, summary, and gallery."""
    jobs = _collect(job_ids)
    ttl = ttl_hours if ttl_hours > 0 else ttl_hours_default()

    source_ids = {job["source_id"] for job in jobs}
    primary = get_source(next(iter(source_ids)))
    if not primary:
        raise PublishError(
            "The source for these jobs is no longer in the database",
            "Call fetch_source(url) and rebuild the clips.",
        )

    project = primary.get("project") or "default"
    batch_id = new_batch_id()

    # The library holds the record; the served dir is a *view* onto it. The clip is
    # written into the project (durable) and copied into the served tree (TTL'd), so
    # link expiry never destroys the artifact — it only stops serving it.
    batch_dir = export_dir(project, batch_id)
    served_dir = serve_dir() / batch_id
    served_dir.mkdir(parents=True, exist_ok=True)

    created = now_ts()
    expires = created + ttl * 3600
    clips: list[dict[str, Any]] = []

    for job in jobs:
        clip = get_clip(job["clip_id"])
        source = get_source(job["source_id"])
        clip_id = job["clip_id"]

        # Durable copy into the library, then the served view.
        home = clip_dir(clip_id, project)
        home.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job["output_path"], home / "clip.mp4")
        shutil.copy2(job["output_path"], served_dir / f"{clip_id}.mp4")

        thumb_link = ""
        thumb_src = job.get("thumb_path") or ""
        if thumb_src and Path(thumb_src).is_file():
            shutil.copy2(thumb_src, home / "clip.jpg")
            shutil.copy2(thumb_src, served_dir / f"{clip_id}.jpg")
            thumb_link = f"{base_url()}/{batch_id}/{clip_id}.jpg"

        members = clip.get("member_rows", [])
        segments = job_segments(job["job_id"])
        span_start = min((m["start"] for m in members), default=0.0)
        span_end = max((m["end"] for m in members), default=0.0)
        best = max(members, key=lambda m: m["score"]) if members else {}

        clips.append(
            {
                "clip_id": clip_id,
                "label": clip.get("label", ""),
                "duration_s": round(float(job.get("duration") or 0), 2),
                "link": f"{base_url()}/{batch_id}/{clip_id}.mp4",
                "thumbnail": thumb_link,
                "source_url": source.get("url", ""),
                "source_start": round(span_start, 2),
                "source_end": round(span_end, 2),
                "source_link": source_deep_link(source.get("url", ""), span_start),
                "built_from": [
                    {
                        "candidate_id": member["candidate_id"],
                        "start": round(member["start"], 2),
                        "end": round(member["end"], 2),
                    }
                    for member in members
                ],
                "kept_segments": segments,
                "reason": best.get("reason", ""),
                "score": round(float(best.get("score") or 0), 1),
            }
        )

    clips.sort(key=lambda c: c["source_start"])

    manifest = {
        "_protocol": "sift/manifest/v1",
        "batch_id": batch_id,
        "project": project,
        "source": {
            "source_id": primary["source_id"],
            "url": primary.get("url", ""),
            "title": primary.get("title", ""),
            "duration_s": round(float(primary.get("duration") or 0), 2),
        },
        "created_at": now_iso(),
        "ttl_hours": ttl,
        "expires_at": _iso(expires),
        "expires_ts": expires,
        "serve_path": str(served_dir),
        "clips": clips,
    }

    manifest_path = batch_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    atomic_write_text(batch_dir / "index.html", render_gallery(manifest))
    # The served copy, so the gallery and manifest resolve over HTTP too.
    atomic_write_json(served_dir / "manifest.json", manifest)
    atomic_write_text(served_dir / "index.html", render_gallery(manifest))
    touch_project(project, exports=batch_id)

    # The source has now been cut and published. Delete the video; keep the transcript.
    deleted: list[str] = []
    for source_id in source_ids:
        if not has_live_jobs(source_id) and delete_source_video(source_id):
            deleted.append(source_id)

    swept = sweep_expired_batches()

    append_receipt(
        receipt_path(),
        "publish_outputs",
        {"batch_id": batch_id, "job_ids": job_ids, "ttl_hours": ttl},
        f"published {len(clips)} clip(s) to {batch_dir}; deleted {len(deleted)} source video(s)",
    )

    return {
        "batch_id": batch_id,
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "gallery": f"{base_url()}/{batch_id}/index.html",
        "links": [clip["link"] for clip in clips],
        "summary": render_summary(manifest),
        "sources_deleted": deleted,
        "batches_expired": swept,
    }


def _iso(timestamp: float) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415 - local, keeps the module import light

    return (
        datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------
# The verifiable summary
# --------------------------------------------------------------------------


def render_summary(manifest: dict[str, Any]) -> str:
    """The human-checkable block: link, source range, deep link, and the agent's rationale."""
    source = manifest["source"]
    clips = manifest["clips"]
    lines = [
        f'Produced {len(clips)} clip(s) from "{source["title"]}" ({hhmmss(source["duration_s"])})',
        "",
    ]

    for index, clip in enumerate(clips, start=1):
        head = f"{index}. [{clip['label']} · {clip['duration_s']:.0f}s]"
        lines.append(f"{head}  {clip['link']}")
        lines.append(
            f"   source {hhmmss(clip['source_start'])}–{hhmmss(clip['source_end'])} · "
            f"{clip['source_link']}"
        )
        if clip.get("reason"):
            lines.append(f'   "{clip["reason"]}"')
        lines.append("")

    lines.append(f"Links expire in {manifest['ttl_hours']} hours ({manifest['expires_at']}).")
    lines.append("Every clip deep-links to its exact moment in the source — check any pick.")
    return "\n".join(lines)


def render_gallery(manifest: dict[str, Any]) -> str:
    """A self-contained review page: inline players, source deep links, rationale."""
    source = manifest["source"]
    cards: list[str] = []

    for clip in manifest["clips"]:
        reason = html.escape(clip.get("reason") or "")
        cards.append(
            f"""<article class="clip">
  <video controls preload="metadata" poster="{html.escape(clip["thumbnail"])}" src="{html.escape(clip["link"])}"></video>
  <div class="meta">
    <span class="label">{html.escape(clip["label"])}</span>
    <span class="dur">{clip["duration_s"]:.0f}s</span>
  </div>
  <p class="reason">{reason}</p>
  <p class="src">
    source {hhmmss(clip["source_start"])}–{hhmmss(clip["source_end"])} ·
    <a href="{html.escape(clip["source_link"])}" target="_blank" rel="noopener">verify in original</a>
  </p>
</article>"""
        )

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(source["title"])} — clips</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --card:#f6f6f7; --line:#e3e3e6; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111; --fg:#eee; --muted:#999; --card:#1b1b1d; --line:#2c2c30; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:2rem 1rem; background:var(--bg); color:var(--fg);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  header {{ max-width:64rem; margin:0 auto 2rem; }}
  h1 {{ font-size:1.4rem; margin:0 0 .25rem; }}
  .sub {{ color:var(--muted); font-size:.9rem; }}
  .grid {{ max-width:64rem; margin:0 auto; display:grid; gap:1.5rem;
           grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); }}
  .clip {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:.75rem; }}
  video {{ width:100%; aspect-ratio:9/16; border-radius:8px; background:#000; }}
  .meta {{ display:flex; justify-content:space-between; align-items:center; margin:.6rem 0 .3rem; }}
  .label {{ font-weight:600; text-transform:uppercase; font-size:.72rem; letter-spacing:.06em; }}
  .dur {{ color:var(--muted); font-size:.8rem; }}
  .reason {{ margin:.3rem 0; font-size:.9rem; }}
  .src {{ margin:.3rem 0 0; font-size:.78rem; color:var(--muted); }}
  a {{ color:inherit; }}
</style>
<header>
  <h1>{html.escape(source["title"])}</h1>
  <p class="sub">
    {len(manifest["clips"])} clip(s) · source {hhmmss(source["duration_s"])} ·
    <a href="{html.escape(source["url"])}" target="_blank" rel="noopener">original</a> ·
    expires {html.escape(manifest["expires_at"])}
  </p>
</header>
<main class="grid">
{chr(10).join(cards)}
</main>
"""


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def sweep_expired_batches() -> int:
    """Un-serve batches past their TTL. The library keeps the clips.

    Retention applies to *links*, not to artifacts. An expired batch stops resolving over
    HTTP; its clips stay in ``projects/<project>/clips/`` where you can re-publish them.
    Deleting a clip from the library is an explicit act, never a timer.
    """
    swept = 0
    for manifest in expired_exports():
        served = manifest.get("serve_path") or ""
        if served and Path(served).is_dir():
            sweep_dir(served)
            swept += 1
    if swept:
        log.info("un-served %d expired batch(es); their clips remain in the library", swept)
    return swept


def list_exports(project: str = "") -> list[dict[str, Any]]:
    """Published batches, newest first."""
    return sorted(all_exports(project), key=lambda m: m.get("created_at", ""), reverse=True)
