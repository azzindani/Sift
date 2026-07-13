"""The encode path: silence trim, reframe, captions, thumbnail — one ffmpeg pass.

Everything here is signal processing, not judgement. Silence detection, boundary
snapping, crop smoothing, and caption timing are pure functions of their inputs;
run twice on the same clip and you get the same bytes out.

The one genuinely tricky invariant is the **timeline map**. Dropping dead air and
crossfading between spans both move content around in time, so a word spoken at
2710.4s in the source may land at 12.1s in the clip. Captions must follow that
move exactly or they desync. So the segment list, the crop keyframes, and the
caption events are all built against one shared source→output mapping, computed
once, in ``build_timeline``.

MediaPipe is imported *inside* the crop function, never at module load: the
router and the caption-only path must not pay its ~300 MB import cost.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _clip_helpers import (
    TRIM_THRESHOLDS,
    get_encode_threads,
    get_max_clip_s,
)
from _clip_transcript import (
    FONT_FILE,
    build_ass_lines,
    build_ass_word_pop,
    font_path,
)
from shared.platform_utils import ffmpeg_bin, run

log = logging.getLogger("clipper.render")

SILENCE_TIMEOUT_S = 300.0
ENCODE_TIMEOUT_S = 1800.0
SEGMENT_TIMEOUT_S = 600.0
THUMB_TIMEOUT_S = 60.0
FACE_SAMPLE_TIMEOUT_S = 300.0

CROSSFADE_S = 0.15  # member-to-member join; video and audio must use the SAME value
MIN_SEGMENT_S = 0.25
MICRO_FADE_S = 0.02  # kills the click at a hard concat join

OUT_W = 1080
OUT_H = 1920
OUT_FPS = 30
AUDIO_RATE = 44100

# libass scans every file in `fontsdir` and tries to parse each one as a font, so the
# bundled font gets its own subdirectory. Point it at the job temp dir and it tries to
# load cap.ass and filtergraph.txt as fonts.
FONTS_SUBDIR = "fonts"

FACE_SAMPLE_FPS = 2.0
MAX_FACE_SAMPLES = 240
MAX_CROP_KEYPOINTS = 40
SMOOTH_WINDOW = 5  # samples in the moving average
MAX_PAN_PX_PER_S = 90.0  # crop glides, never snaps


class RenderError(Exception):
    """A render failed in an expected way. Carries an actionable hint."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class Segment:
    """One kept slice of source, and where it lands in the output."""

    src_start: float
    src_end: float
    out_start: float
    member: int

    @property
    def duration(self) -> float:
        return self.src_end - self.src_start

    def to_out(self, src_t: float) -> float:
        """Map a source timestamp inside this segment onto the output timeline."""
        return self.out_start + (src_t - self.src_start)


@dataclass
class Timeline:
    """The full source→output mapping for one clip."""

    segments: list[Segment] = field(default_factory=list)
    duration: float = 0.0
    members: int = 1

    def map_word(self, word: dict[str, Any]) -> dict[str, Any] | None:
        """Remap a transcript word into output time, or None if it fell in dropped air."""
        mid = (word["s"] + word["e"]) / 2
        for seg in self.segments:
            if seg.src_start <= mid < seg.src_end:
                start = seg.to_out(max(word["s"], seg.src_start))
                end = seg.to_out(min(word["e"], seg.src_end))
                if end <= start:
                    end = start + 0.08
                return {"w": word["w"], "s": round(start, 3), "e": round(end, 3)}
        return None

    def map_time(self, src_t: float) -> float | None:
        for seg in self.segments:
            if seg.src_start <= src_t < seg.src_end:
                return seg.to_out(src_t)
        return None


# --------------------------------------------------------------------------
# Silence detection and the trim map
# --------------------------------------------------------------------------

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silences(
    video: Path, start: float, end: float, threshold_db: int = -30, min_silence_s: float = 0.3
) -> list[tuple[float, float]]:
    """Silence intervals inside a span, in absolute source time.

    Input seeking (``-ss`` before ``-i``) rebases output timestamps to zero, so the
    reported offsets are relative to ``start`` and get shifted back here.
    """
    args = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(video),
        "-map",
        "0:a:0",
        "-af",
        f"silencedetect=noise={threshold_db}dB:d={min_silence_s}",
        "-f",
        "null",
        "-",
    ]
    result = run(args, timeout=SILENCE_TIMEOUT_S)
    if not result.ok:
        log.warning("silencedetect failed, treating span as all speech: %s", result.stderr[-200:])
        return []

    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in result.stderr.splitlines():
        begin = _SILENCE_START.search(line)
        if begin:
            pending = start + float(begin.group(1))
            continue
        finish = _SILENCE_END.search(line)
        if finish and pending is not None:
            silences.append((pending, start + float(finish.group(1))))
            pending = None
    if pending is not None:  # silence ran to the end of the span
        silences.append((pending, end))

    return [(max(s, start), min(e, end)) for s, e in silences if min(e, end) > max(s, start)]


def keep_segments(
    span_start: float,
    span_end: float,
    silences: list[tuple[float, float]],
    drop_threshold_s: float,
    pad_s: float,
) -> list[tuple[float, float]]:
    """Snap the span's edges to speech, then drop dead air longer than the threshold.

    Silences *shorter* than the threshold are natural pauses and stay — cutting them
    is what makes an edit sound frantic. Each dropped region keeps ``pad_s`` of its
    silence on both sides so words never lose their tails.
    """
    start, end = span_start, span_end

    # Snap the outer boundaries inward past any leading/trailing silence.
    for sil_start, sil_end in silences:
        if sil_start <= start < sil_end:
            start = min(max(start, sil_end - pad_s), end)
        if sil_start < end <= sil_end:
            end = max(min(end, sil_start + pad_s), start)
    if end - start < MIN_SEGMENT_S:
        return [(span_start, span_end)]  # all silence: keep the span rather than emit nothing

    drops: list[tuple[float, float]] = []
    for sil_start, sil_end in silences:
        lo, hi = max(sil_start, start), min(sil_end, end)
        if hi - lo <= drop_threshold_s:
            continue
        lo, hi = lo + pad_s, hi - pad_s
        if hi - lo > 0.1:
            drops.append((lo, hi))

    segments: list[tuple[float, float]] = []
    cursor = start
    for lo, hi in sorted(drops):
        if lo > cursor:
            segments.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < end:
        segments.append((cursor, end))

    return [(s, e) for s, e in segments if e - s >= MIN_SEGMENT_S] or [(start, end)]


def build_timeline(
    video: Path,
    members: list[dict[str, Any]],
    spec: dict[str, Any],
    max_duration_s: float | None = None,
) -> Timeline:
    """Trim each member span, then lay the survivors out on the output timeline.

    Members join with a crossfade, which overlaps them by ``CROSSFADE_S`` — so each
    member after the first starts that much earlier than a naive sum would suggest.
    Video and audio use the same crossfade duration, which is what keeps them in sync.
    """
    threshold = TRIM_THRESHOLDS.get(str(spec.get("trim_aggressiveness", "tight")), 0.5)
    pad = float(spec.get("pad_ms", 150)) / 1000.0
    noise = int(spec.get("silence_threshold_db", -30))
    budget = max_duration_s if max_duration_s is not None else float(spec.get("max_duration_s", 60))
    budget = min(budget, get_max_clip_s())

    timeline = Timeline(members=len(members))
    out_cursor = 0.0
    spent = 0.0

    for index, member in enumerate(members):
        span_start = float(member["start"])
        span_end = float(member["end"])
        silences = detect_silences(video, span_start, span_end, noise, min_silence_s=0.3)
        kept = keep_segments(span_start, span_end, silences, threshold, pad)

        if index > 0 and timeline.segments:
            out_cursor -= CROSSFADE_S  # the crossfade overlaps this member with the previous

        for src_start, src_end in kept:
            remaining = budget - spent
            if remaining <= MIN_SEGMENT_S:
                break
            length = min(src_end - src_start, remaining)
            timeline.segments.append(
                Segment(
                    src_start=round(src_start, 3),
                    src_end=round(src_start + length, 3),
                    out_start=round(out_cursor, 3),
                    member=index,
                )
            )
            out_cursor += length
            spent += length

        if spent >= budget - MIN_SEGMENT_S:
            break

    if not timeline.segments:
        raise RenderError(
            "Silence trimming removed the entire clip",
            "The span is silent. Widen it, or lower trim_aggressiveness for this label.",
        )

    timeline.duration = round(out_cursor, 3)
    timeline.members = len({seg.member for seg in timeline.segments})
    return timeline


# --------------------------------------------------------------------------
# Reframe — geometry is local and deterministic (MediaPipe), never an API call
# --------------------------------------------------------------------------


def _crop_width(src_w: int, src_h: int) -> int:
    """Widest 9:16 column that fits, rounded to an even width."""
    want = int(src_h * 9 / 16)
    return max(2, min(want, src_w) // 2 * 2)


def detect_faces(video: Path, timeline: Timeline, temp: Path) -> list[tuple[float, float]]:
    """Face-centre x (in source pixels) over time, as (source_time, x) samples.

    Returns [] when MediaPipe is unavailable or finds nothing — the caller then
    degrades to a centred crop rather than failing the render.
    """
    try:
        import mediapipe as mp  # noqa: PLC0415 - lazy: ~300 MB, render path only
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        log.info("MediaPipe unavailable (%s) — falling back to a centred crop", exc)
        return []

    frames_dir = temp / "faces"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # One ffmpeg call per kept segment, not per frame: on a 2-core box, 120 process
    # launches cost more than the decode they were meant to avoid. Dropped air is never
    # sampled — there is nothing there to follow.
    budget = MAX_FACE_SAMPLES
    sampled: list[tuple[float, Path]] = []

    for index, seg in enumerate(timeline.segments):
        if budget <= 0:
            break
        wanted = min(int(seg.duration * FACE_SAMPLE_FPS) + 1, budget)
        if wanted <= 0:
            continue

        result = run(
            [
                ffmpeg_bin(),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-ss",
                f"{seg.src_start:.3f}",
                "-to",
                f"{seg.src_end:.3f}",
                "-i",
                str(video),
                "-vf",
                f"fps={FACE_SAMPLE_FPS},scale=480:-2",
                "-frames:v",
                str(wanted),
                "-q:v",
                "5",
                str(frames_dir / f"s{index:02d}_%04d.jpg"),
            ],
            timeout=FACE_SAMPLE_TIMEOUT_S,
        )
        if not result.ok:
            log.warning(
                "face frame extraction failed on segment %d: %s", index, result.stderr[-200:]
            )
            continue

        for frame in sorted(frames_dir.glob(f"s{index:02d}_*.jpg")):
            # ffmpeg's fps filter emits frames on a fixed grid from the seek point.
            offset = (int(frame.stem.split("_")[-1]) - 1) / FACE_SAMPLE_FPS
            src_t = min(seg.src_start + offset, seg.src_end)
            sampled.append((src_t, frame))
            budget -= 1

    samples: list[tuple[float, float]] = []
    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.5
    )
    try:
        for src_t, frame in sampled:
            with Image.open(frame) as img:
                array = np.asarray(img.convert("RGB"))
            detection = detector.process(array)
            if not detection.detections:
                continue
            # Largest face wins — the active speaker is usually the biggest in frame.
            best = max(
                detection.detections,
                key=lambda d: d.location_data.relative_bounding_box.width
                * d.location_data.relative_bounding_box.height,
            )
            box = best.location_data.relative_bounding_box
            samples.append((src_t, box.xmin + box.width / 2))  # relative x centre, 0..1
    finally:
        detector.close()
        shutil.rmtree(frames_dir, ignore_errors=True)

    log.info("face detection: %d sample(s) from %d frame(s)", len(samples), len(sampled))
    return samples


def smooth_centers(
    samples: list[tuple[float, float]], src_w: int, crop_w: int, timeline: Timeline
) -> list[tuple[float, float]]:
    """Smooth face centres, cap pan speed, and remap onto the output timeline.

    Returns (output_time, crop_x) keypoints. Jitter is what makes an auto-reframe
    look cheap, so the moving average and the pan-speed cap are not optional.
    """
    if not samples:
        return []

    mapped: list[tuple[float, float]] = []
    for src_t, rel_x in samples:
        out_t = timeline.map_time(src_t)
        if out_t is None:
            continue
        centre_px = rel_x * src_w
        crop_x = centre_px - crop_w / 2
        mapped.append((out_t, max(0.0, min(crop_x, float(src_w - crop_w)))))
    if not mapped:
        return []
    mapped.sort(key=lambda p: p[0])

    # Moving average.
    smoothed: list[tuple[float, float]] = []
    for i, (out_t, _) in enumerate(mapped):
        lo = max(0, i - SMOOTH_WINDOW // 2)
        hi = min(len(mapped), i + SMOOTH_WINDOW // 2 + 1)
        window = [x for _, x in mapped[lo:hi]]
        smoothed.append((out_t, sum(window) / len(window)))

    # Pan-speed cap: the crop glides toward the target instead of snapping to it.
    capped: list[tuple[float, float]] = [smoothed[0]]
    for out_t, target in smoothed[1:]:
        prev_t, prev_x = capped[-1]
        max_delta = MAX_PAN_PX_PER_S * max(out_t - prev_t, 1e-3)
        delta = max(-max_delta, min(target - prev_x, max_delta))
        capped.append((out_t, prev_x + delta))

    # Thin to a keypoint budget so the ffmpeg expression stays a sane size.
    if len(capped) > MAX_CROP_KEYPOINTS:
        stride = len(capped) / MAX_CROP_KEYPOINTS
        capped = [capped[int(i * stride)] for i in range(MAX_CROP_KEYPOINTS)]

    if capped[0][0] > 0:
        capped.insert(0, (0.0, capped[0][1]))
    if capped[-1][0] < timeline.duration:
        capped.append((timeline.duration, capped[-1][1]))
    return capped


def crop_x_expr(keypoints: list[tuple[float, float]]) -> str:
    """A piecewise-linear ffmpeg expression for the crop x, in output time.

    Built as a *flat sum of half-open gates* rather than nested ifs: each gate
    contributes on exactly one interval, so there is no double-counting at the
    boundaries and no expression-parser recursion to blow up.
    """
    if not keypoints:
        return "(in_w-out_w)/2"
    if len(keypoints) == 1:
        return f"{keypoints[0][1]:.1f}"

    terms: list[str] = []
    for i in range(len(keypoints) - 1):
        t0, x0 = keypoints[i]
        t1, x1 = keypoints[i + 1]
        if t1 - t0 < 1e-3:
            continue
        slope = (x1 - x0) / (t1 - t0)
        terms.append(f"(gte(t,{t0:.3f})*lt(t,{t1:.3f})*({x0:.1f}+{slope:.3f}*(t-{t0:.3f})))")

    last_t, last_x = keypoints[-1]
    terms.append(f"(gte(t,{last_t:.3f})*{last_x:.1f})")
    return "clip(" + "+".join(terms) + ",0,in_w-out_w)"


def two_speaker_columns(
    samples: list[tuple[float, float]], src_w: int, crop_w: int
) -> tuple[float, float] | None:
    """Split face centres into two clusters (a two-shot). None if it isn't one."""
    if len(samples) < 8:
        return None
    xs = sorted(rel_x for _, rel_x in samples)

    # Largest gap in the sorted centres is the split between the two speakers.
    best_gap, split = 0.0, -1
    for i in range(1, len(xs)):
        gap = xs[i] - xs[i - 1]
        if gap > best_gap:
            best_gap, split = gap, i
    if best_gap < 0.15 or split < 0:
        return None

    left, right = xs[:split], xs[split:]
    minority = min(len(left), len(right)) / len(xs)
    if minority < 0.2:
        return None

    def column(group: list[float]) -> float:
        centre = (sum(group) / len(group)) * src_w
        return max(0.0, min(centre - crop_w / 2, float(src_w - crop_w)))

    return column(left), column(right)


# --------------------------------------------------------------------------
# Filtergraph
# --------------------------------------------------------------------------


def extract_segments(
    video: Path, timeline: Timeline, src_w: int, src_h: int, temp: Path
) -> list[Path]:
    """Cut each kept segment into a normalized intermediate, using **input seeking**.

    This exists for two reasons, and both are load-bearing.

    *Speed.* A filter-only graph (`[0:v]trim=start=888:end=897`) makes ffmpeg decode the
    entire source and throw away everything outside the trim. On an 18-minute talk that is
    merely wasteful; on a 4-hour podcast on two vCPUs it is fatal. Seeking with ``-ss``
    *before* ``-i`` decodes only the span asked for, so cost scales with the clip, not the
    source.

    *Stability.* A source can change resolution mid-stream — TED's own mp4 declares
    854x480 in the header but 31,899 of its 31,982 frames are 640x480. ffmpeg configures
    the graph from the header, hits the first real frame, and reinitializes the whole
    filter chain; `concat` and `xfade` do not survive that. Writing each segment out at a
    pinned size, frame rate, pixel format, and sample rate means the assembly pass sees
    inputs that cannot disagree with each other.
    """
    segments: list[Path] = []

    for index, seg in enumerate(timeline.segments):
        out = temp / f"seg{index:03d}.mp4"
        fade_out = max(seg.duration - MICRO_FADE_S, 0.0)

        args = [
            ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{seg.src_start:.3f}",
            "-t",
            f"{seg.duration:.3f}",
            "-i",
            str(video),
            "-vf",
            (
                f"scale={src_w}:{src_h}:force_original_aspect_ratio=decrease,"
                f"pad={src_w}:{src_h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,fps={OUT_FPS},format=yuv420p"
            ),
            "-af",
            (
                f"aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                # Micro-fades kill the click at a hard concat join.
                f"afade=t=in:st=0:d={MICRO_FADE_S},"
                f"afade=t=out:st={fade_out:.3f}:d={MICRO_FADE_S}"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "16",  # near-lossless: this is an intermediate, the final encode sets quality
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(AUDIO_RATE),
            str(out),
        ]
        result = run(args, timeout=SEGMENT_TIMEOUT_S)
        if result.timed_out:
            raise RenderError(
                f"Segment extraction timed out at {seg.src_start:.0f}s",
                "The source may be corrupt. Retry render_clip, or shorten the clip.",
            )
        if not result.ok or not out.is_file():
            tail = "\n".join(result.stderr.strip().splitlines()[-3:])[:400]
            raise RenderError(
                f"Could not cut segment {seg.src_start:.1f}-{seg.src_end:.1f}s: {tail}",
                "Check the span lies inside the source. Re-run fetch_source if the video is damaged.",
            )
        segments.append(out)

    return segments


def build_filtergraph(
    timeline: Timeline,
    src_w: int,
    src_h: int,
    reframe: str,
    keypoints: list[tuple[float, float]],
    columns: tuple[float, float] | None,
    ass_file: str,
) -> str:
    """Assemble the pass-2 graph: concat → crossfade → crop → scale → captions.

    Input *i* is segment *i*'s intermediate file, already normalized by
    ``extract_segments`` — so every link in this graph has the same size, frame rate,
    timebase, and sample format by construction.
    """
    crop_w = _crop_width(src_w, src_h)
    parts: list[str] = []

    # 1. Pin the timebase on each input. `concat` emits 1/1000000 and `fps` emits 1/30,
    #    and xfade refuses to join links whose timebases differ ("First input link main
    #    timebase (1/30) do not match ... (1/1000000)"). Cheap insurance.
    for i in range(len(timeline.segments)):
        parts.append(f"[{i}:v]settb=1/{OUT_FPS},fps={OUT_FPS}[v{i}]")
        parts.append(f"[{i}:a]asettb=1/{AUDIO_RATE}[a{i}]")

    # 2. Concat the segments belonging to each member (hard cuts across dropped air).
    member_ids = sorted({seg.member for seg in timeline.segments})
    member_durations: dict[int, float] = {}
    for member in member_ids:
        indices = [i for i, seg in enumerate(timeline.segments) if seg.member == member]
        member_durations[member] = sum(timeline.segments[i].duration for i in indices)

        if len(indices) == 1:
            video_src, audio_src = f"[v{indices[0]}]", f"[a{indices[0]}]"
        else:
            chain = "".join(f"[v{i}][a{i}]" for i in indices)
            parts.append(f"{chain}concat=n={len(indices)}:v=1:a=1[cv{member}][ca{member}]")
            video_src, audio_src = f"[cv{member}]", f"[ca{member}]"

        parts.append(f"{video_src}settb=1/{OUT_FPS},format=yuv420p,setsar=1[mv{member}]")
        parts.append(f"{audio_src}asettb=1/{AUDIO_RATE}[ma{member}]")

    # 3. Crossfade member to member. Video and audio share CROSSFADE_S, so they stay locked.
    if len(member_ids) == 1:
        video_label, audio_label = f"[mv{member_ids[0]}]", f"[ma{member_ids[0]}]"
    else:
        video_label, audio_label = f"[mv{member_ids[0]}]", f"[ma{member_ids[0]}]"
        accumulated = member_durations[member_ids[0]]
        for step, member in enumerate(member_ids[1:], start=1):
            offset = max(accumulated - CROSSFADE_S, 0.0)
            parts.append(
                f"{video_label}[mv{member}]xfade=transition=fade:"
                f"duration={CROSSFADE_S}:offset={offset:.3f}[vx{step}]"
            )
            parts.append(f"{audio_label}[ma{member}]acrossfade=d={CROSSFADE_S}[ax{step}]")
            video_label, audio_label = f"[vx{step}]", f"[ax{step}]"
            accumulated += member_durations[member] - CROSSFADE_S

    # 4. Reframe to 9:16.
    if reframe == "stacked" and columns is not None:
        left_x, right_x = columns
        panel_h = OUT_H // 2
        panel_w = int(src_h * (OUT_W / panel_h))  # a 1080x960 panel is a 9:8 crop
        panel_w = max(2, min(panel_w, src_w) // 2 * 2)
        left_x = max(0.0, min(left_x, float(src_w - panel_w)))
        right_x = max(0.0, min(right_x, float(src_w - panel_w)))
        parts.append(f"{video_label}split=2[sa][sb]")
        parts.append(
            f"[sa]crop=w={panel_w}:h={src_h}:x={left_x:.0f}:y=0,scale={OUT_W}:{panel_h}[top]"
        )
        parts.append(
            f"[sb]crop=w={panel_w}:h={src_h}:x={right_x:.0f}:y=0,scale={OUT_W}:{panel_h}[bot]"
        )
        parts.append("[top][bot]vstack=inputs=2[vr]")
    else:
        x_expr = crop_x_expr(keypoints) if reframe == "speaker" and keypoints else "(in_w-out_w)/2"
        parts.append(
            f"{video_label}crop=w={crop_w}:h={src_h}:x='{x_expr}':y=0,"
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black[vr]"
        )

    # 5. Burn in captions from the bundled font. cwd is the job temp dir, so these are
    #    bare relative paths and need no filtergraph escaping.
    if ass_file:
        parts.append(f"[vr]subtitles=filename={ass_file}:fontsdir={FONTS_SUBDIR}[vout]")
    else:
        parts.append("[vr]null[vout]")
    parts.append(f"{audio_label}aresample={AUDIO_RATE},asetpts=PTS-STARTPTS[aout]")

    return ";".join(parts)


# --------------------------------------------------------------------------
# Captions
# --------------------------------------------------------------------------


def build_ass(
    transcript: dict[str, Any], timeline: Timeline, spec: dict[str, Any], temp: Path
) -> str:
    """Write the ASS file for a clip. Returns its filename, or "" if there's nothing to say.

    Word-level transcripts get word-pop captions. Cue-level transcripts get styled
    lines — faking per-word timing from cue timing would put words on screen at the
    wrong moment, so we don't.
    """
    style = str(spec.get("caption_style", "key_phrase"))
    words = transcript.get("words") or []

    if words and transcript.get("has_words"):
        mapped = [m for m in (timeline.map_word(w) for w in words) if m]
        if not mapped:
            return ""
        content = build_ass_word_pop(mapped, caption_style=style)
    else:
        segments = transcript.get("segments") or []
        mapped_segments: list[dict[str, Any]] = []
        for seg in segments:
            start = timeline.map_time(seg["start"])
            end = timeline.map_time(max(seg["end"] - 0.01, seg["start"]))
            if start is None or end is None or end <= start:
                continue
            mapped_segments.append({"start": start, "end": end, "text": seg["text"]})
        if not mapped_segments:
            return ""
        content = build_ass_lines(mapped_segments)

    ass_path = temp / "cap.ass"
    ass_path.write_text(content, encoding="utf-8")

    bundled = font_path()
    if not bundled.is_file():
        raise RenderError(
            f"Bundled caption font missing: {bundled}",
            "Restore assets/fonts/ — captions never resolve system fonts.",
        )
    fonts = temp / FONTS_SUBDIR
    fonts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled, fonts / FONT_FILE)
    return "cap.ass"


def clip_words(transcript: dict[str, Any], timeline: Timeline) -> list[dict[str, Any]]:
    """Words that survive into the clip, in output time (used for the caption + summary)."""
    return [m for m in (timeline.map_word(w) for w in transcript.get("words") or []) if m]


# --------------------------------------------------------------------------
# Encode
# --------------------------------------------------------------------------


def encode(
    inputs: list[Path],
    temp: Path,
    filtergraph: str,
    output: Path,
    crf: int = 23,
) -> None:
    """Run the assembly pass over the segment intermediates.

    cwd=temp, so the ASS file and fonts dir are bare relative names and need no
    filtergraph escaping (a path with a colon or backslash would otherwise break it).
    """
    args = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"]
    for path in inputs:
        args += ["-i", str(path)]
    args += [
        "-filter_complex",
        filtergraph,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-threads",
        str(get_encode_threads()),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        str(output),
    ]
    result = run(args, timeout=ENCODE_TIMEOUT_S, cwd=str(temp))

    if result.timed_out:
        raise RenderError(
            f"Encode timed out after {ENCODE_TIMEOUT_S:.0f}s",
            "The clip is too long or the box is loaded. Shorten the clip and retry render_clip.",
        )
    if not result.ok or not output.is_file():
        tail = "\n".join(result.stderr.strip().splitlines()[-4:])[:600]
        raise RenderError(
            f"ffmpeg encode failed (exit {result.returncode}): {tail}",
            "Check the clip span lies inside the source. Retry with reframe='center' if the "
            "crop expression is at fault.",
        )


def make_thumbnail(clip: Path, output: Path, label: str = "") -> bool:
    """Grab a frame ~15% into the clip and badge it. Never fatal — a clip beats no clip."""
    try:
        probe = run(
            [
                ffmpeg_bin(),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-ss",
                "0",
                "-i",
                str(clip),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(output),
            ],
            timeout=THUMB_TIMEOUT_S,
        )
        if not probe.ok or not output.is_file():
            return False
    except Exception as exc:  # noqa: BLE001 - a thumbnail must never fail a render
        log.warning("thumbnail extraction failed: %s", exc)
        return False

    if not label:
        return True

    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415 - lazy, light

        with Image.open(output) as img:
            frame = img.convert("RGB")
            draw = ImageDraw.Draw(frame)
            size = max(28, frame.width // 18)
            try:
                font = ImageFont.truetype(str(font_path()), size)
            except OSError:
                font = ImageFont.load_default()
            text = label.upper()
            box = draw.textbbox((0, 0), text, font=font)
            pad = size // 3
            x, y = pad, pad
            draw.rectangle(
                [x, y, x + (box[2] - box[0]) + 2 * pad, y + (box[3] - box[1]) + 2 * pad],
                fill=(0, 0, 0),
            )
            draw.text((x + pad, y + pad - box[1]), text, font=font, fill=(255, 215, 0))
            frame.save(output, quality=88)
    except Exception as exc:  # noqa: BLE001
        log.warning("thumbnail badge failed (frame kept): %s", exc)
    return True


def probe_duration(path: Path) -> float:
    """Duration of a rendered file, from the container. 0.0 if unreadable."""
    from shared.platform_utils import ffprobe_bin  # noqa: PLC0415 - avoids an import cycle at load

    result = run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        timeout=30.0,
    )
    if not result.ok:
        return 0.0
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError:
        return 0.0


def summarize_segments(timeline: Timeline) -> list[dict[str, float]]:
    """The source spans a clip was actually built from — surfaced in the manifest."""
    return [
        {
            "start": seg.src_start,
            "end": seg.src_end,
            "out_start": seg.out_start,
        }
        for seg in timeline.segments
    ]


def clip_text(transcript: dict[str, Any], timeline: Timeline) -> str:
    """Flat text of what the clip actually says, after trimming."""
    return " ".join(word["w"] for word in clip_words(transcript, timeline))


def trimmed_seconds(members: list[dict[str, Any]], timeline: Timeline) -> float:
    """How much dead air the trim removed. Reported in the job progress."""
    raw = sum(float(m["end"]) - float(m["start"]) for m in members)
    kept = sum(seg.duration for seg in timeline.segments)
    return round(max(0.0, raw - kept), 2)


def estimate_crf(duration: float) -> int:
    """Slightly higher quality for very short clips; they get scrutinized frame by frame."""
    return 21 if duration <= 20 else 23


def clamp_dimensions(width: int, height: int) -> tuple[int, int]:
    """Fall back to 720p if ffprobe could not read the source dimensions."""
    if width > 0 and height > 0:
        return width, height
    return 1280, 720
