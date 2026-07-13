"""Source acquisition: yt-dlp fetch, disk guard, URL dedup, transcript detection.

This is the most fragile stage in the system and it is written to be honest about
that. A datacenter IP draws bot challenges and throttling from every major video
host, so **fetch failure is a normal return path**, not an exception — it comes
back as an error dict whose hint names the cookies/proxy knob that fixes it.

Order of operations matters here:

1. **Probe before download.** One cheap metadata call gets duration, title, and —
   critically — whether an English transcript exists at all. A source with no
   captions fails here, before a single byte of video is pulled.
2. **Disk guard before download.** A 4-hour source at 720p is several GB and the
   box has one small disk. Estimate from the probe's real format sizes, double it
   for the merge, add a margin, and refuse rather than fill the disk.
3. **Download, then delete.** The video is disposable. The parsed transcript is
   not — it outlives the video, because captions and verification still need it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from _clip_helpers import (
    FETCH_SOURCE_TTL_HOURS,
    connect,
    cookies_path_default,
    get_max_height,
    now_ts,
    proxy_default,
)
from _clip_library import (
    DEFAULT_PROJECT,
    all_sources,
    ensure_project,
    find_source_by_url,
    load_source,
    new_source_id,
    project_dir,
    save_source,
    source_video_present,
    update_source,
    validate_project,
)
from _clip_library import load_transcript as _load_transcript
from _clip_transcript import parse_transcript
from shared.file_utils import PathError, free_bytes, resolve_path, safe_mkdir, sweep_dir
from shared.platform_utils import BinaryMissing, ffprobe_bin, run, ytdlp_cmd
from shared.version_control import atomic_write_json

log = logging.getLogger("clipper.fetch")

PROBE_TIMEOUT_S = 120.0
DOWNLOAD_TIMEOUT_S = float(os.environ.get("SIFT_DOWNLOAD_TIMEOUT_S", "3600"))
SUBTITLE_TIMEOUT_S = 180.0  # captions are small; this only guards a hung endpoint
DISK_MARGIN_BYTES = 300 * 1024 * 1024  # headroom left free after the merge
MERGE_FACTOR = 2.0  # yt-dlp holds the parts and the merged file at once
FALLBACK_BYTES_PER_S = 250 * 1024  # ~2 Mbps, used only when the probe reports no sizes


class FetchError(Exception):
    """A fetch failed in an expected way. Carries the hint that unblocks the caller."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# Signatures of the failures a VPS IP actually hits, mapped to the knob that fixes each.
_CHALLENGE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"sign in to confirm (you|your)", re.I),
        "Bot challenge. Export browser cookies and pass cookies_path=..., or set "
        "SIFT_COOKIES_PATH / SIFT_PROXY.",
    ),
    (
        re.compile(r"(HTTP Error 429|Too Many Requests|rate.?limit)", re.I),
        "Rate-limited on this IP. Set SIFT_PROXY to a residential proxy, or retry later.",
    ),
    (
        re.compile(r"(private video|members-only|join this channel)", re.I),
        "Source is private/members-only. Pass cookies_path=... for an account that can view it.",
    ),
    (
        re.compile(r"(age.?restricted|confirm your age|inappropriate for some users)", re.I),
        "Age-restricted source. Pass cookies_path=... for a signed-in account.",
    ),
    (
        re.compile(r"(video unavailable|not available in your country|geo.?restrict)", re.I),
        "Source unavailable from this IP/region. Set SIFT_PROXY and retry.",
    ),
    (
        re.compile(r"(unsupported url|is not a valid url)", re.I),
        "yt-dlp does not support this URL. Pass a direct video page URL.",
    ),
    (
        re.compile(
            r"(network is unreachable|errno 101|temporary failure in name resolution)", re.I
        ),
        "The host has no route to the source (a Docker bridge has no IPv6 route, so an "
        "AAAA record fails intermittently). Sift pins yt-dlp to IPv4 by default — if you "
        "set SIFT_FORCE_IPV4=0, unset it. Otherwise check the container's DNS/egress.",
    ),
]


def _classify(stderr: str) -> str:
    """Map yt-dlp stderr onto an actionable hint. Falls back to a generic one."""
    for pattern, hint in _CHALLENGE_PATTERNS:
        if pattern.search(stderr):
            return hint
    return (
        "yt-dlp could not fetch this source. Check the URL, then try cookies_path=... "
        "(SIFT_COOKIES_PATH) or a proxy (SIFT_PROXY)."
    )


def _tail(text: str, limit: int = 400) -> str:
    """Last meaningful line(s) of stderr, trimmed for the error dict."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    errors = [ln for ln in lines if "ERROR" in ln or "error" in ln]
    chosen = errors[-1] if errors else (lines[-1] if lines else "no output")
    return chosen[:limit]


def force_ipv4() -> bool:
    """Whether to pin yt-dlp to IPv4. On by default — see below.

    A default Docker bridge network has **no IPv6 route**. DNS still returns AAAA records,
    so yt-dlp intermittently picks an IPv6 address and dies with
    ``[Errno 101] Network is unreachable`` — it works, then it doesn't, on the same URL,
    which is the worst kind of failure to debug. Pinning to IPv4 makes it deterministic.

    Set ``SIFT_FORCE_IPV4=0`` on an IPv6-only host, or if you have configured IPv6 in Docker.
    """
    return os.environ.get("SIFT_FORCE_IPV4", "1").strip() not in {"0", "false", "no", "off"}


def _ytdlp_args(cookies_path: str = "") -> list[str]:
    """Common yt-dlp flags: no playlist expansion, quiet, cookies/proxy when configured."""
    args = [
        *ytdlp_cmd(),
        "--no-playlist",
        "--no-progress",
        "--no-color",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--retries",
        "3",
    ]
    if force_ipv4():
        args.append("--force-ipv4")
    # An explicit cookies_path= from the agent is a promise the file is there, so a bad one
    # is an error. SIFT_COOKIES_PATH is standing config for a jar that may not have been
    # dropped in yet — a missing one must not break every fetch of every site that needs no
    # cookies at all. Log it; the bot-challenge hint already names the knob.
    if cookies_path:
        resolved = resolve_path(cookies_path, allowed_exts=(".txt",), must_exist=True)
        args += ["--cookies", str(resolved)]
    elif configured := cookies_path_default():
        jar = Path(configured)
        if jar.is_file():
            args += ["--cookies", str(resolve_path(configured, allowed_exts=(".txt",)))]
        else:
            log.warning(
                "SIFT_COOKIES_PATH=%s does not exist — fetching without cookies", configured
            )
    proxy = proxy_default()
    if proxy:
        args += ["--proxy", proxy]
    return args


def validate_url(url: str) -> str:
    """Reject anything that is not an http(s) URL before it reaches a subprocess."""
    cleaned = (url or "").strip()
    if not re.match(r"^https?://[^\s]+$", cleaned, re.I):
        raise FetchError(
            f"Not an http(s) URL: {cleaned or '(empty)'}",
            "Pass a full video page URL, e.g. https://www.youtube.com/watch?v=...",
        )
    return cleaned


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


def probe(url: str, cookies_path: str = "") -> dict[str, Any]:
    """Fetch metadata only. Raises FetchError with an actionable hint on failure."""
    result = run(
        [*_ytdlp_args(cookies_path), "-J", "--skip-download", url], timeout=PROBE_TIMEOUT_S
    )

    if result.timed_out:
        raise FetchError(
            f"yt-dlp metadata probe timed out after {PROBE_TIMEOUT_S:.0f}s: {url}",
            "The host is slow or blocking. Set SIFT_PROXY, or retry later.",
        )
    if not result.ok:
        raise FetchError(f"yt-dlp probe failed: {_tail(result.stderr)}", _classify(result.stderr))

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(
            f"yt-dlp returned unparseable metadata: {exc}",
            "Retry; if it persists the extractor may be broken for this host.",
        ) from exc

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise FetchError(
                "URL is an empty playlist, not a video.",
                "Pass a single video URL, not a playlist or channel.",
            )
        info = entries[0]

    return info


def probe_with_captions(
    url: str, cookies_path: str = "", attempts: int = 3
) -> tuple[dict[str, Any], dict[str, str]]:
    """Probe until the caption list is populated, or we are confident there is none.

    A caption track is fetched by the extractor from a *separate* endpoint, and that call
    fails intermittently — yt-dlp then returns perfectly good metadata with an empty
    ``subtitles`` dict and no error. Taking that at face value tells the agent "this source
    has no transcript", which is not a slow path or a retry — it is a dead end that sends it
    to look for another source entirely.

    A missing caption list is expensive to be wrong about, and a probe is cheap. So when it
    comes back empty, we ask again before believing it.
    """
    info: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        info = probe(url, cookies_path)
        langs = caption_langs(info)
        if langs:
            return info, langs
        if attempt < attempts:
            log.warning(
                "probe %d/%d reported no captions for %s — retrying (the caption endpoint "
                "fails intermittently)",
                attempt,
                attempts,
                url,
            )
            time.sleep(1.5 * attempt)

    return info, {}


def caption_langs(info: dict[str, Any]) -> dict[str, str]:
    """English caption tracks available, mapped lang -> 'manual' | 'auto'."""
    found: dict[str, str] = {}
    for kind, key in (("auto", "automatic_captions"), ("manual", "subtitles")):
        for lang in info.get(key) or {}:
            if lang.lower().startswith("en"):
                found.setdefault(lang, kind)
                if kind == "manual":
                    found[lang] = "manual"  # manual beats auto for the same lang
    return found


def estimate_bytes(info: dict[str, Any], max_height: int) -> int:
    """Estimate download size from the probe's real format sizes, honestly rounded up."""
    duration = float(info.get("duration") or 0)
    best_video = 0
    best_audio = 0

    for fmt in info.get("formats") or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if not size:
            tbr = fmt.get("tbr") or 0  # kbit/s
            if tbr and duration:
                size = int(tbr * 1000 / 8 * duration)
        if not size:
            continue
        height = fmt.get("height") or 0
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        if vcodec != "none" and height and height <= max_height:
            best_video = max(best_video, int(size))
        elif vcodec == "none" and acodec != "none":
            best_audio = max(best_audio, int(size))

    total = best_video + best_audio
    if total <= 0:
        total = int(duration * FALLBACK_BYTES_PER_S) if duration else 0
    return total


def disk_guard(estimated: int, dest: Path) -> None:
    """Refuse a download that cannot finish. Sweeps expired sources first."""
    needed = int(estimated * MERGE_FACTOR) + DISK_MARGIN_BYTES
    free = free_bytes(dest)

    if free < needed:
        reclaimed = sweep_expired_sources()
        free = free_bytes(dest)
        log.info("disk guard swept %d expired source(s), free now %d", reclaimed, free)

    if free < needed:
        raise FetchError(
            f"Insufficient disk: need ~{needed / 1e9:.1f} GB, {free / 1e9:.1f} GB free at {dest}",
            "Free disk space, lower max_height (e.g. 480), or publish and expire old batches.",
        )


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def find_existing(url: str, project: str) -> dict[str, Any]:
    """A prior source for this URL in this project whose video is still on disk."""
    return find_source_by_url(url, project)


def _stream_dimensions(video: Path) -> tuple[int, int]:
    """Dimensions from the container header. May not describe most of the frames."""
    try:
        result = run(
            [
                ffprobe_bin(),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(video),
            ],
            timeout=60.0,
        )
        if result.ok:
            streams = json.loads(result.stdout).get("streams") or [{}]
            return int(streams[0].get("width") or 0), int(streams[0].get("height") or 0)
    except (BinaryMissing, json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
        log.warning("ffprobe stream dimensions failed: %s", exc)
    return 0, 0


def _probe_dimensions(video: Path, duration: float) -> tuple[int, int]:
    """Dimensions of a real decoded frame from the middle of the video.

    The container header is not trustworthy. A TED talk's mp4 advertises 854x480 but
    only its 83-frame branded intro is that size — the other 31,899 frames, i.e. the
    actual talk, are 640x480. Sizing the crop from the header would compute the reframe
    against a resolution that 99.7% of the video does not have.

    So: decode one frame from the middle, where the real content lives. Fall back to the
    header only if that fails.
    """
    midpoint = max(1.0, duration / 2)
    try:
        result = run(
            [
                ffprobe_bin(),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                f"{midpoint:.0f}%+#1",
                "-show_entries",
                "frame=width,height",
                "-of",
                "json",
                str(video),
            ],
            timeout=120.0,
        )
        if result.ok:
            frames = json.loads(result.stdout).get("frames") or []
            if frames:
                width = int(frames[0].get("width") or 0)
                height = int(frames[0].get("height") or 0)
                if width > 0 and height > 0:
                    header = _stream_dimensions(video)
                    if header != (width, height):
                        log.warning(
                            "source resolution changes mid-stream: header says %sx%s, "
                            "real frames are %sx%s — using the frames",
                            header[0],
                            header[1],
                            width,
                            height,
                        )
                    return width, height
    except (BinaryMissing, json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
        log.warning("ffprobe frame dimensions failed: %s", exc)

    return _stream_dimensions(video)


def _locate_subtitle(dest: Path) -> tuple[Path | None, str]:
    """Find the downloaded subtitle file. json3 (word timing) wins over vtt (cue level)."""
    for pattern, kind in (("*.json3", "json3"), ("*.vtt", "vtt")):
        matches = sorted(dest.glob(pattern))
        if matches:
            return matches[0], kind
    return None, "none"


def prepare_source(
    url: str, max_height: int = 0, cookies_path: str = "", project: str = DEFAULT_PROJECT
) -> dict[str, Any]:
    """Probe, disk-guard, and fetch the **transcript only**. Fast — seconds, not minutes.

    The video download is deliberately *not* done here. A 3-hour source takes minutes to
    pull, and an MCP client times out around 30 seconds — a synchronous fetch tool would
    simply never return over HTTP (STANDARDS §23: anything over ~30s must be async). So
    this call does the cheap, decisive work:

      * probe metadata (title, duration, and whether captions exist *at all*),
      * refuse a source with no transcript before a single byte of video is pulled,
      * refuse a download that would not fit on the disk,
      * pull the subtitles — a few hundred KB, a couple of seconds — and parse them.

    The agent can therefore start reading the transcript and picking candidates
    immediately, while the video downloads in the background. The reading time and the
    download overlap instead of stacking.

    Raises FetchError (never an unhandled exception) so the engine can turn any failure
    into an error dict with a hint.
    """
    url = validate_url(url)
    height = max_height if max_height > 0 else get_max_height()
    project = validate_project(project)
    ensure_project(project)

    existing = find_existing(url, project)
    if existing:
        # Same URL, same source_id — the transcript, the candidates and every clip built
        # from them stay valid. The video is only a cache; if publish deleted it, pull it
        # again into the SAME record rather than minting a duplicate source.
        existing["reused"] = True
        existing["video_present"] = source_video_present(existing)
        return existing

    info, langs = probe_with_captions(url, cookies_path)
    duration = float(info.get("duration") or 0)
    title = str(info.get("title") or "untitled")

    if duration <= 0:
        raise FetchError(
            f"Source reports no duration (live stream?): {title}",
            "Live streams are not supported. Pass a finished VOD URL.",
        )

    if not langs:
        raise FetchError(
            f"No English transcript available for: {title}",
            "Sift selects from the transcript, so a caption track is required. This was "
            "re-probed to rule out a transient caption-endpoint failure. Use a source with "
            "captions, or transcribe externally and re-host with subtitles.",
        )

    source_id = new_source_id()
    dest = safe_mkdir(project_dir(project) / "sources" / source_id)

    try:
        estimated = estimate_bytes(info, height)
        disk_guard(estimated, dest)

        # Subtitles only. --skip-download is what keeps this call short.
        result = run(
            [
                *_ytdlp_args(cookies_path),
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                ",".join(sorted(langs)) or "en",
                "--sub-format",
                "json3/vtt/best",
                "-o",
                str(dest / "source.%(ext)s"),
                url,
            ],
            timeout=SUBTITLE_TIMEOUT_S,
        )
        if result.timed_out:
            raise FetchError(
                f"Subtitle download timed out after {SUBTITLE_TIMEOUT_S:.0f}s: {title}",
                "The caption endpoint is slow or blocking. Retry, or set SIFT_PROXY.",
            )
        if not result.ok:
            raise FetchError(
                f"Subtitle download failed: {_tail(result.stderr)}", _classify(result.stderr)
            )

        sub_file, kind = _locate_subtitle(dest)
        if sub_file is None:
            raise FetchError(
                f"Transcript was advertised but not downloaded for: {title}",
                "Retry fetch_source; the caption endpoint may have been throttled.",
            )

        parsed = parse_transcript(sub_file, kind)
        if not parsed["segments"]:
            raise FetchError(
                f"Transcript downloaded but parsed to zero segments ({kind}): {title}",
                "The caption track is empty. Use a source with a real transcript.",
            )

        transcript_path = dest / "transcript.json"
        atomic_write_json(transcript_path, parsed)

        record = {
            "_protocol": "sift/source/v1",
            "source_id": source_id,
            "project": project,
            "url": url,
            "title": title,
            "duration": duration,
            "transcript_kind": kind,
            "transcript_path": str(transcript_path),
            "local_path": "",  # the video is not here yet — the download job fills this in
            "max_height": height,
            "width": 0,
            "height": 0,
            "estimated_bytes": estimated,
            "frames_sampled": 0,
            "fetched_at": now_ts(),
        }
        save_source(record)

        record["reused"] = False
        record["has_words"] = parsed["has_words"]
        record["segment_count"] = len(parsed["segments"])
        return record

    except Exception:
        sweep_dir(dest)  # never leave a half-prepared source behind
        raise


def download_video(source_id: str, cookies_path: str = "") -> dict[str, Any]:
    """Pull the source video. Runs on the queue worker — this is the slow half of a fetch."""
    record = load_source(source_id)
    if not record:
        raise FetchError(f"Unknown source_id: {source_id}", "Call fetch_source(url) first.")
    if record.get("local_path") and Path(record["local_path"]).is_file():
        return record  # already on disk

    url = record["url"]
    height = int(record.get("max_height") or get_max_height())
    dest = safe_mkdir(project_dir(record["project"]) / "sources" / source_id)

    disk_guard(int(record.get("estimated_bytes") or 0), dest)

    result = run(
        [
            *_ytdlp_args(cookies_path),
            "-f",
            f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--no-write-subs",
            "--no-write-auto-subs",
            "-o",
            str(dest / "source.%(ext)s"),
            url,
        ],
        timeout=DOWNLOAD_TIMEOUT_S,
    )

    if result.timed_out:
        raise FetchError(
            f"Download timed out after {DOWNLOAD_TIMEOUT_S:.0f}s: {record['title']}",
            "Raise SIFT_DOWNLOAD_TIMEOUT_S, lower max_height, or use a shorter source.",
        )
    if not result.ok:
        raise FetchError(f"Download failed: {_tail(result.stderr)}", _classify(result.stderr))

    videos = [p for p in dest.glob("source.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
    if not videos:
        raise FetchError(
            f"yt-dlp reported success but wrote no video file in {dest}",
            "Retry render_clip; if it persists, lower max_height on fetch_source.",
        )
    video = videos[0]
    width, video_height = _probe_dimensions(video, float(record["duration"]))

    return update_source(source_id, local_path=str(video), width=width, height=video_height)


# --------------------------------------------------------------------------
# Source lifecycle — disposable video, durable transcript
# --------------------------------------------------------------------------


def get_source(source_id: str) -> dict[str, Any]:
    """Load a source record from the library. {} if unknown."""
    return load_source(source_id)


def load_transcript(source_id: str) -> dict[str, Any]:
    """The parsed transcript. Survives deletion of the video — that is the point."""
    return _load_transcript(source_id)


def delete_source_video(source_id: str) -> bool:
    """Delete the source video, keeping the transcript. Returns True if a file went away.

    Called once a source's clips are published — the video has served its purpose
    and it is the single biggest thing on the disk.
    """
    source = get_source(source_id)
    if not source:
        return False
    local = source.get("local_path") or ""
    removed = False
    if local:
        try:
            path = resolve_path(local)
            if path.is_file():
                path.unlink()
                removed = True
        except (PathError, OSError) as exc:
            log.warning("could not delete source video %s: %s", local, exc)
    update_source(source_id, local_path="")  # the transcript stays; only the video goes
    return removed


def has_live_jobs(source_id: str) -> bool:
    """True if any job for this source is still queued or running."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE source_id = ? AND status IN ('queued','running')",
            (source_id,),
        ).fetchone()
    return bool(row and row["n"])


def sweep_expired_sources() -> int:
    """Delete source videos older than the TTL with no live jobs. Returns the count."""
    cutoff = now_ts() - FETCH_SOURCE_TTL_HOURS * 3600
    swept = 0
    for record in all_sources():
        if not record.get("local_path"):
            continue
        if float(record.get("fetched_at") or 0) >= cutoff:
            continue
        if has_live_jobs(record["source_id"]):
            continue
        if delete_source_video(record["source_id"]):
            swept += 1
    return swept


def all_sources_in(project: str = "") -> list[dict[str, Any]]:
    """Every source record, in one project or across the library."""
    return all_sources(project)
