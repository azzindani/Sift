"""Shot-aware 9:16 reframing: follow the speaker, and fall back to the whole frame.

Cropping a 16:9 source to 9:16 throws away two thirds of the width. That is the right
trade while a face is on screen and the crop can follow it. It is *wrong* the moment the
source cuts to a wide stage shot or a full-frame slide, and the first live TED render
proved it twice in one clip:

* On the wide shots MediaPipe finds no face — the speaker is roughly 4% of the frame
  height, far below what a detector will call a face. The crop then simply stays where it
  last was, and the output shows a giant red letter "D" and the backs of the audience's
  heads *while she is talking*, with the speaker outside the frame entirely.
* On a chart slide there is no face to find either, so the same centred crop keeps the
  middle third of a full-width chart: the title is sliced to "o / e / ren", the right half
  of the graph is gone. The crop destroys the exact information the moment exists to show.

So the layout is a decision *per span*, not per clip:

* a face is on screen  -> crop to 9:16 and follow it (smoothed, pan-capped, snapped at cuts)
* no face is on screen -> fit the whole frame into 9:16, over a blurred fill of itself

Both branches render to the same 1080x1920 and a single ``overlay=...:enable=`` switches
between them, so the switch costs one filter and lands on a real shot change. Nothing is
cut and re-joined to make it happen — the audio never learns the layout moved.

Everything here is a pure function of its inputs. No model, no inference (CLAUDE.md §6.1).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from shared.platform_utils import ffmpeg_bin, run

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _clip_render import Timeline

log = logging.getLogger("clipper.reframe")

OUT_W = 1080
OUT_H = 1920

FACE_SAMPLE_FPS = 2.0  # one look every 500 ms
MAX_FACE_SAMPLES = 240
FACE_SAMPLE_TIMEOUT_S = 300.0

MAX_CROP_KEYPOINTS = 40
SMOOTH_WINDOW = 5  # samples in the moving average
MAX_PAN_PX_PER_S = 90.0  # within a shot the crop glides, never snaps

# A crop that jumps more than this fraction of the source width between two samples 500 ms
# apart is a *camera cut*, not a person walking. Panning across it drags the framing over
# the new shot for a second, which is what sliced the speaker in half on the TED talk.
CUT_JUMP_FRAC = 0.12
SNAP_S = 0.04  # hold the old framing until one frame before the cut, then jump

# Hysteresis on the follow -> fit switch. One missed detection is a blink or a head turn,
# not a shot change; flipping the whole layout for 500 ms would look like a glitch.
MIN_FIT_SPAN_S = 1.2  # a faceless run shorter than this is ignored
FIT_BRIDGE_S = 0.8  # two faceless runs closer than this merge into one

# The blurred fill. Blurring at 1080x1920 costs real time on two vCPUs, and it is a *blur*:
# nothing is lost by computing it small and scaling it up.
BLUR_W = 270
BLUR_H = 480
BLUR_SIGMA = 8
FIT_LIFT_PX = 140  # nudge the fitted frame up, leaving the lower third to the captions


@dataclass(frozen=True)
class FaceTrack:
    """What the detector saw, and — just as load-bearing — where it saw nothing.

    ``hits`` alone cannot distinguish "we never looked there" from "we looked and the
    frame held no face". The second is the signal that picks the fit layout, so the times
    we sampled are kept even when they came back empty.
    """

    hits: list[tuple[float, float]] = field(default_factory=list)  # (src_t, relative x)
    sampled: list[float] = field(default_factory=list)  # every src_t we looked at

    def __bool__(self) -> bool:
        return bool(self.hits)


def crop_width(src_w: int, src_h: int) -> int:
    """Widest 9:16 column that fits, rounded to an even width."""
    want = int(src_h * 9 / 16)
    return max(2, min(want, src_w) // 2 * 2)


def detect_faces(video: Path, timeline: Timeline, temp: Path) -> FaceTrack:
    """Face-centre x (relative, 0..1) over time, plus every timestamp we looked at.

    Returns an empty track when MediaPipe is unavailable — the caller then reframes with
    the fit layout rather than failing the render.
    """
    try:
        import mediapipe as mp  # noqa: PLC0415 - lazy: ~300 MB, render path only
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        log.info("MediaPipe unavailable (%s) — the whole clip will use the fit layout", exc)
        return FaceTrack()

    frames_dir = temp / "faces"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # One ffmpeg call per kept segment, not per frame: on a 2-core box, 120 process launches
    # cost more than the decode they were meant to avoid. Dropped air is never sampled —
    # there is nothing there to follow.
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

    hits: list[tuple[float, float]] = []
    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.5
    )
    try:
        for src_t, frame in sampled:
            with Image.open(frame) as img:
                array = np.asarray(img.convert("RGB"))
            detection = detector.process(array)
            if not detection.detections:
                continue  # looked, found nothing — `sampled` remembers that we looked
            # Largest face wins — the active speaker is usually the biggest in frame.
            best = max(
                detection.detections,
                key=lambda d: d.location_data.relative_bounding_box.width
                * d.location_data.relative_bounding_box.height,
            )
            box = best.location_data.relative_bounding_box
            hits.append((src_t, box.xmin + box.width / 2))
    finally:
        detector.close()
        shutil.rmtree(frames_dir, ignore_errors=True)

    track = FaceTrack(hits=hits, sampled=[t for t, _ in sampled])
    log.info("face detection: %d hit(s) from %d frame(s)", len(track.hits), len(track.sampled))
    return track


def fit_spans(track: FaceTrack, timeline: Timeline) -> list[tuple[float, float]]:
    """Output-time spans that should use the fit layout because no face is on screen.

    A run of consecutive empty samples is a wide shot or a slide. Short runs are ignored
    (a blink is not a shot change) and near-adjacent runs are bridged, so the layout never
    flickers back and forth across a single missed detection.
    """
    if not track.sampled:
        return []

    hit_times = {round(t, 3) for t, _ in track.hits}
    step = 1.0 / FACE_SAMPLE_FPS

    empties: list[float] = []
    for src_t in sorted(track.sampled):
        if round(src_t, 3) in hit_times:
            continue
        out_t = timeline.map_time(src_t)
        if out_t is not None:
            empties.append(out_t)
    if not empties:
        return []

    # Group consecutive empty samples into runs. A gap of more than one sampling interval
    # means a face reappeared (or a dropped-air segment boundary fell between them).
    runs: list[list[float]] = [[empties[0]]]
    for out_t in empties[1:]:
        if out_t - runs[-1][-1] > step * 1.5:
            runs.append([out_t])
        else:
            runs[-1].append(out_t)

    spans = [(run[0], run[-1] + step) for run in runs]

    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] <= FIT_BRIDGE_S:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    kept = [(s, min(e, timeline.duration)) for s, e in merged if e - s >= MIN_FIT_SPAN_S]
    log.info(
        "fit layout on %d span(s): %s", len(kept), [(round(s, 1), round(e, 1)) for s, e in kept]
    )
    return kept


def enable_expr(spans: list[tuple[float, float]]) -> str:
    """ffmpeg `enable` expression that is true inside any of ``spans``.

    A sum of `between()` terms: each is 0 or 1 and they never overlap (``fit_spans``
    merges), so the sum is a clean boolean and there is no ordering to get wrong.
    """
    if not spans:
        return "0"
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in spans)


def smooth_centers(
    samples: list[tuple[float, float]], src_w: int, crop_w: int, timeline: Timeline
) -> list[tuple[float, float]]:
    """Smooth face centres, cap pan speed, and remap onto the output timeline.

    Returns (output_time, crop_x) keypoints. Jitter is what makes an auto-reframe look
    cheap, so the moving average and the pan-speed cap are not optional.
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

    # Smooth and cap WITHIN each shot; snap between them. Doing this across a cut is what
    # slices the speaker in half for a second after every camera change.
    capped: list[tuple[float, float]] = []
    for shot in _split_at_cuts(mapped, src_w):
        smoothed: list[tuple[float, float]] = []
        for i, (out_t, _) in enumerate(shot):
            lo = max(0, i - SMOOTH_WINDOW // 2)
            hi = min(len(shot), i + SMOOTH_WINDOW // 2 + 1)
            window = [x for _, x in shot[lo:hi]]
            smoothed.append((out_t, sum(window) / len(window)))

        # Pan-speed cap: inside a shot the crop glides toward the target.
        glided: list[tuple[float, float]] = [smoothed[0]]
        for out_t, target in smoothed[1:]:
            prev_t, prev_x = glided[-1]
            max_delta = MAX_PAN_PX_PER_S * max(out_t - prev_t, 1e-3)
            delta = max(-max_delta, min(target - prev_x, max_delta))
            glided.append((out_t, prev_x + delta))

        if capped:
            # The cut. Hold the outgoing framing right up to it, then jump in one frame —
            # otherwise the piecewise-linear expression would ramp between the two shots.
            cut_t, new_x = glided[0]
            hold_t = cut_t - SNAP_S
            if hold_t > capped[-1][0]:
                capped.append((hold_t, capped[-1][1]))
            capped.append((cut_t, new_x))
            capped.extend(glided[1:])
        else:
            capped.extend(glided)

    # Thin to a keypoint budget so the ffmpeg expression stays a sane size. Never drop a
    # cut: thinning one away would restore the very ramp this is here to prevent.
    if len(capped) > MAX_CROP_KEYPOINTS:
        cuts = {
            i
            for i in range(1, len(capped))
            if capped[i][0] - capped[i - 1][0] <= SNAP_S + 1e-6
            and abs(capped[i][1] - capped[i - 1][1]) > 1.0
        }
        keep = cuts | {i - 1 for i in cuts} | {0, len(capped) - 1}
        budget = max(0, MAX_CROP_KEYPOINTS - len(keep))
        rest = [i for i in range(len(capped)) if i not in keep]
        if budget and rest:
            stride = len(rest) / budget
            keep |= {rest[int(i * stride)] for i in range(budget)}
        capped = [capped[i] for i in sorted(keep)]

    if capped[0][0] > 0:
        capped.insert(0, (0.0, capped[0][1]))
    if capped[-1][0] < timeline.duration:
        capped.append((timeline.duration, capped[-1][1]))
    return capped


def _split_at_cuts(
    mapped: list[tuple[float, float]], src_w: int
) -> list[list[tuple[float, float]]]:
    """Group face samples into shots. A big jump between samples is a cut, not a pan."""
    threshold = CUT_JUMP_FRAC * src_w
    shots: list[list[tuple[float, float]]] = [[mapped[0]]]
    for prev, cur in zip(mapped, mapped[1:], strict=False):
        if abs(cur[1] - prev[1]) > threshold:
            shots.append([cur])
        else:
            shots[-1].append(cur)
    return shots


def crop_x_expr(keypoints: list[tuple[float, float]]) -> str:
    """A piecewise-linear ffmpeg expression for the crop x, in output time.

    Built as a *flat sum of half-open gates* rather than nested ifs: each gate contributes
    on exactly one interval, so there is no double-counting at the boundaries and no
    expression-parser recursion to blow up.
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


def follow_chain(
    src_h: int, crop_w: int, keypoints: list[tuple[float, float]], reframe: str
) -> str:
    """Crop a 9:16 column and follow the face. The whole-clip default when a face is on screen."""
    x_expr = crop_x_expr(keypoints) if reframe == "speaker" and keypoints else "(in_w-out_w)/2"
    return (
        f"crop=w={crop_w}:h={src_h}:x='{x_expr}':y=0,"
        f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
    )


def fit_chain(source: str, out: str) -> list[str]:
    """The whole frame, letterboxed over a blurred, darkened copy of itself.

    Used where a crop would lie: a wide shot the detector cannot lock onto, or a slide
    whose content spans the full width. Nothing is cropped away, so nothing is lost.
    """
    return [
        f"{source}split=2[fb_bg][fb_fg]",
        # The fill is blurred, so it is computed at 270x480 and scaled up — a full-size
        # gblur costs multiples of the encode itself on two vCPUs and buys nothing.
        f"[fb_bg]scale={BLUR_W}:{BLUR_H}:force_original_aspect_ratio=increase,"
        f"crop={BLUR_W}:{BLUR_H},gblur=sigma={BLUR_SIGMA},"
        f"scale={OUT_W}:{OUT_H},eq=brightness=-0.10,setsar=1[fb_bgb]",
        f"[fb_fg]scale={OUT_W}:-2,setsar=1[fb_fgs]",
        f"[fb_bgb][fb_fgs]overlay=(W-w)/2:(H-h)/2-{FIT_LIFT_PX}:format=auto,"
        f"setsar=1,format=yuv420p{out}",
    ]
