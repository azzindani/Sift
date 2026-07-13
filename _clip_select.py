"""Candidate persistence, dedup/merge, the vision funnel, and clip planning.

**The agent scores; the server stores.** Nothing in this module decides what is
clip-worthy — scores, labels, and reasons arrive as arguments. What the server
does own is everything deterministic: validating spans, merging overlaps without
losing the better framing, capping the vision budget, and grouping candidates
into clip definitions. Same inputs, same outputs, every time.

The one subtle rule is the merge. Two candidates that overlap by more than half
are the same moment seen twice, so they collapse into the **union of their
boundaries** carrying the **max score** — never a blind discard, because the
wider framing is usually the one that survives the cold-open test.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from _clip_helpers import (
    LABELS,
    MAX_CANDIDATE_S,
    MAX_OPS_PER_CALL,
    MIN_CANDIDATE_S,
    MIN_CLIP_S,
    PLAN_MODES,
    assembly_params,
    get_frame_budget,
    get_max_clip_s,
    get_max_frames_per_call,
    new_id,
    now_iso,
    now_ts,
)
from _clip_library import (
    clear_clips,
    load_clip,
    new_clip_id,
    save_candidates,
    save_clip,
    source_dir,
    update_source,
)
from _clip_library import load_candidates as _load_candidates
from _clip_transcript import span_text
from shared.file_utils import safe_mkdir
from shared.platform_utils import ffmpeg_bin, run

log = logging.getLogger("clipper.select")

MERGE_OVERLAP_RATIO = 0.5  # >50% of the shorter span => same moment
FRAME_SAMPLE_TIMEOUT_S = 120.0
AUDIO_CUE_TIMEOUT_S = 180.0
FRAME_WIDTH = 512  # downscale before the frames ever leave the box

# A clip that opens on one of these is referring to something the viewer never saw.
COLD_OPEN_PRONOUNS = set(
    """he she it they them him her his hers its their theirs this that these those
    we us our and but so because which who""".split()  # noqa: SIM905 - a word list reads better than 24 quoted strings
)

# How much of a source's opening is presumed to be a title card. The transcript starts
# before the picture does — TED's first word is at 0.4s, its logo sting runs to 3.3s.
INTRO_GUARD_S = 8.0

STOPWORDS = set(
    """the a an and or but if then than that this these those is are was were be been
    being to of in on at for with as by from it its i you he she we they them his her
    our your my me him us do does did have has had not no yes so just like really know
    think would could should will can about what when there here get got one up out all
    very kind sort""".split()  # noqa: SIM905 - a word list reads better than 80 quoted strings
)


class SelectError(Exception):
    """A candidate/plan input was invalid. Carries the hint that fixes it."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_candidate(raw: Any, index: int, duration: float) -> dict[str, Any]:
    """Validate one agent-submitted candidate. Raises SelectError naming the bad field."""
    where = f"candidates[{index}]"
    if not isinstance(raw, dict):
        raise SelectError(
            f"{where} is {type(raw).__name__}, not an object",
            'Each candidate is {"start": float, "end": float, "label": str, "score": number, "reason": str}.',
        )

    try:
        start = float(raw.get("start"))
        end = float(raw.get("end"))
    except (TypeError, ValueError) as exc:
        raise SelectError(
            f"{where} has a non-numeric start/end: {raw.get('start')!r}..{raw.get('end')!r}",
            "Pass start and end as seconds (floats) from the source timeline.",
        ) from exc

    if not (math.isfinite(start) and math.isfinite(end)):
        raise SelectError(f"{where} start/end must be finite numbers", "Pass real second offsets.")
    if end <= start:
        raise SelectError(
            f"{where} is inverted or empty: start={start:.2f} end={end:.2f}",
            "Ensure end > start; both are seconds from the start of the source.",
        )
    if start < 0 or end > duration + 1.0:
        raise SelectError(
            f"{where} span {start:.1f}..{end:.1f}s falls outside the source (0..{duration:.1f}s)",
            f"Clamp the span inside 0..{duration:.1f}s. Use read_transcript_chunk to confirm timing.",
        )

    length = end - start
    if length < MIN_CANDIDATE_S:
        raise SelectError(
            f"{where} is {length:.1f}s — shorter than the {MIN_CANDIDATE_S:.0f}s minimum",
            f"Widen the span to at least {MIN_CANDIDATE_S:.0f}s.",
        )
    if length > MAX_CANDIDATE_S:
        raise SelectError(
            f"{where} is {length:.1f}s — longer than the {MAX_CANDIDATE_S:.0f}s maximum",
            f"Split it into separate candidates, each under {MAX_CANDIDATE_S:.0f}s.",
        )

    label = str(raw.get("label") or "").strip().lower()
    if label not in LABELS:
        raise SelectError(
            f"{where} has unknown label {label!r}",
            f"Use one of: {' '.join(LABELS)}",
        )

    try:
        score = float(raw.get("score", 5))
    except (TypeError, ValueError) as exc:
        raise SelectError(
            f"{where} has a non-numeric score: {raw.get('score')!r}", "Pass score as 1-10."
        ) from exc
    score = max(0.0, min(10.0, score))

    cues = raw.get("cues") or {}
    if not isinstance(cues, dict):
        raise SelectError(
            f"{where} cues must be an object",
            'Pass cues as {"text": true, "audio_spike": false, "vision_confirmed": false}.',
        )

    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "label": label,
        "score": score,
        "reason": str(raw.get("reason") or "").strip()[:400],
        "cues": cues,
    }


# --------------------------------------------------------------------------
# Dedup / merge
# --------------------------------------------------------------------------


def overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Intersection over the *shorter* span: 1.0 when one fully contains the other."""
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    if inter <= 0:
        return 0.0
    shortest = min(a["end"] - a["start"], b["end"] - b["start"])
    return inter / shortest if shortest > 0 else 0.0


def _fuse(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Fuse two overlapping candidates: union boundaries, max score, winner's framing."""
    winner, loser = (a, b) if a["score"] >= b["score"] else (b, a)
    cues = {**(loser.get("cues") or {}), **(winner.get("cues") or {})}
    fused = {
        **winner,
        "start": min(a["start"], b["start"]),
        "end": max(a["end"], b["end"]),
        "score": max(a["score"], b["score"]),
        "cues": cues,
    }
    fused["end"] = min(fused["end"], fused["start"] + MAX_CANDIDATE_S)
    # Keep both ids so an existing DB row is updated in place rather than duplicated.
    for key in ("candidate_id",):
        if key in winner:
            fused[key] = winner[key]
        elif key in loser:
            fused[key] = loser[key]
    return fused


def merge_all(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse every >50%-overlapping pair until stable. Returns (merged, fuse_count)."""
    working = sorted(candidates, key=lambda c: (c["start"], c["end"]))
    fusions = 0

    changed = True
    while changed:
        changed = False
        out: list[dict[str, Any]] = []
        for cand in working:
            if out and overlap_ratio(out[-1], cand) > MERGE_OVERLAP_RATIO:
                out[-1] = _fuse(out[-1], cand)
                fusions += 1
                changed = True
            else:
                out.append(cand)
        working = sorted(out, key=lambda c: (c["start"], c["end"]))

    return working, fusions


def cold_open_warning(text: str, start: float) -> str:
    """Soft-flag a candidate that opens on something the viewer cannot follow.

    Two ways that happens, and the first live batch produced both.

    *Textually*, the first word refers to something the viewer never saw ("but…", "it…").

    *Visually*, the span starts inside the source's opening seconds. A produced video
    opens on a title card, and the transcript does not say so — the TED talk's first word
    lands at 0.4s, but the first 3.3s of **video** are a logo sting and a second of pure
    black. A clip that starts there spends its entire hook on someone else's branding.
    The server cannot know where the card ends without decoding, so it says so and leaves
    the call to the agent, which can look with ``sample_frames``.
    """
    first = re.sub(r"[^a-z']", "", (text or "").strip().split(" ")[0].lower()) if text else ""
    if first and first in COLD_OPEN_PRONOUNS:
        return (
            f"Candidate at {start:.1f}s opens on '{first}' — a bare reference with no antecedent. "
            "Extend start to include what it refers to (the 2-min chunk overlap shows it)."
        )
    if start < INTRO_GUARD_S:
        return (
            f"Candidate starts at {start:.1f}s, inside the source's opening {INTRO_GUARD_S:.0f}s. "
            "Produced videos open on a title card or logo sting, and the transcript does not "
            "show it — the clip would spend its hook on branding, or on black. Call "
            "sample_frames() across the first seconds and start after the card."
        )
    return ""


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def load_candidates(source_id: str) -> list[dict[str, Any]]:
    """Every stored candidate for a source, in timeline order.

    Read straight from ``candidates/<source_id>.yaml`` — so if a human edits that file
    to nudge a boundary, the next plan_clips picks the edit up. That is the point of
    keeping the record in YAML rather than a DB row.
    """
    return _load_candidates(source_id)


def add_candidates(
    source_id: str,
    incoming: list[dict[str, Any]],
    duration: float,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate, merge against what's already stored, and persist. Returns a summary."""
    if not isinstance(incoming, list) or not incoming:
        raise SelectError(
            "candidates is empty",
            'Pass a non-empty list, e.g. [{"start": 120.0, "end": 165.0, "label": "quote", '
            '"score": 8, "reason": "..."}].',
        )
    if len(incoming) > MAX_OPS_PER_CALL:
        raise SelectError(
            f"{len(incoming)} candidates exceeds the {MAX_OPS_PER_CALL}-per-call cap",
            f"Split into batches of {MAX_OPS_PER_CALL} or fewer and call add_candidates again.",
        )

    # Validate the entire array before a single row is written.
    validated = [validate_candidate(raw, i, duration) for i, raw in enumerate(incoming)]

    existing = load_candidates(source_id)
    before = len(existing)
    combined, fusions = merge_all(existing + validated)

    warnings: list[str] = []
    for cand in combined:
        cand["text"] = span_text(segments, cand["start"], cand["end"])[:2000]
        warning = cold_open_warning(cand["text"], cand["start"])
        if warning:
            warnings.append(warning)

    for cand in combined:
        cand.setdefault("candidate_id", new_id("cand"))
        cand.setdefault("created_at", now_ts())
        cand["source_id"] = source_id

    # One atomic rewrite of the whole file: a half-written candidate list is never observable.
    save_candidates(source_id, combined)

    return {
        "stored": len(combined),
        "submitted": len(validated),
        "merged": fusions,
        "previously_stored": before,
        "warnings": warnings,
        "candidates": combined,
    }


# --------------------------------------------------------------------------
# The vision funnel — cheap cues first, capped frames second
# --------------------------------------------------------------------------

_RMS_LINE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf)")
_PTS_LINE = re.compile(r"pts_time:([\d.]+)")
TRANSCRIPT_MARKERS = re.compile(r"\[(laughter|laughs|applause|music|cheering|crosstalk)\]", re.I)


def audio_cues(video: Path, start: float, end: float) -> dict[str, Any]:
    """Cheap, free, on-box: find energy spikes (laughter, applause) in a span.

    Half-second RMS windows; a spike is a window well above the span's own speech
    baseline. This is one of the two cues the two-cue rule needs — it says *where
    to look*, never *what is there*.
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
        "aresample=8000,asetnsamples=4000,astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f",
        "null",
        "-",
    ]
    result = run(args, timeout=AUDIO_CUE_TIMEOUT_S)
    if not result.ok:
        return {"spikes": [], "available": False}

    samples: list[tuple[float, float]] = []
    pts = 0.0
    for line in result.stdout.splitlines():
        pts_match = _PTS_LINE.search(line)
        if pts_match:
            pts = float(pts_match.group(1))
            continue
        rms_match = _RMS_LINE.search(line)
        if rms_match:
            raw = rms_match.group(1)
            if raw == "-inf":
                continue
            samples.append((start + pts, float(raw)))

    if len(samples) < 4:
        return {"spikes": [], "available": False}

    levels = [lvl for _, lvl in samples]
    mean = sum(levels) / len(levels)
    variance = sum((lvl - mean) ** 2 for lvl in levels) / len(levels)
    stdev = math.sqrt(variance)
    threshold = mean + 1.2 * stdev

    spikes: list[dict[str, float]] = []
    run_start: float | None = None
    last_t = samples[0][0]
    for timestamp, level in samples:
        if level >= threshold:
            if run_start is None:
                run_start = timestamp
            last_t = timestamp
        elif run_start is not None:
            if last_t - run_start >= 0.5:  # sustained, not a mic bump
                spikes.append({"start": round(run_start, 2), "end": round(last_t + 0.5, 2)})
            run_start = None
    if run_start is not None and last_t - run_start >= 0.5:
        spikes.append({"start": round(run_start, 2), "end": round(last_t + 0.5, 2)})

    return {
        "spikes": spikes[:10],
        "available": True,
        "baseline_db": round(mean, 1),
        "threshold_db": round(threshold, 1),
    }


def frames_used(source_id: str) -> int:
    """Frames already spent on this source's vision pass. The budget is per source."""
    from _clip_library import load_source  # noqa: PLC0415 - avoids an import cycle at load

    return int(load_source(source_id).get("frames_sampled") or 0)


def sample_frames(
    source: dict[str, Any],
    start: float,
    end: float,
    fps: float,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract capped, downscaled frames for the agent's semantic vision pass.

    Enforces a hard per-source frame budget — the funnel exists so vision is paid
    for on flagged spans only, never on hours of footage. Returns frame *paths*,
    never pixels, plus the cheap cues for the same span so the agent can apply the
    two-cue rule before deciding the moment is real.
    """
    source_id = source["source_id"]
    video_path = source.get("local_path") or ""
    if not video_path or not Path(video_path).is_file():
        raise SelectError(
            f"Source video for {source_id} is no longer on disk",
            "Call fetch_source(url) again — the video is deleted after publish.",
        )

    duration = float(source.get("duration") or 0)
    start = max(0.0, float(start))
    end = min(float(end), duration)
    if end <= start:
        raise SelectError(
            f"Empty span: start={start:.1f} end={end:.1f}",
            f"Pass end > start, inside 0..{duration:.1f}s.",
        )

    budget = get_frame_budget()
    used = frames_used(source_id)
    remaining = budget - used
    if remaining <= 0:
        raise SelectError(
            f"Vision frame budget exhausted for {source_id} ({used}/{budget} frames used)",
            "The funnel is capped on purpose. Select from the transcript, or fetch the source "
            "again for a fresh budget.",
        )

    fps = max(0.1, min(float(fps), 4.0))
    per_call = min(get_max_frames_per_call(), remaining)
    wanted = math.ceil((end - start) * fps)
    take = min(wanted, per_call)

    video = Path(video_path)
    out_dir = safe_mkdir(source_dir(source_id) / "frames" / f"{int(start)}_{int(end)}")
    for stale in out_dir.glob("*.jpg"):
        stale.unlink(missing_ok=True)

    args = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(video),
        "-vf",
        f"fps={fps},scale={FRAME_WIDTH}:-2",
        "-frames:v",
        str(take),
        "-q:v",
        "4",
        str(out_dir / "frame_%03d.jpg"),
    ]
    result = run(args, timeout=FRAME_SAMPLE_TIMEOUT_S)
    if result.timed_out:
        raise SelectError(
            f"Frame sampling timed out on {start:.0f}..{end:.0f}s",
            "Narrow the span or lower fps.",
        )

    frames = sorted(out_dir.glob("*.jpg"))
    if not frames and not result.ok:
        raise SelectError(
            f"ffmpeg could not sample frames: {result.stderr.strip().splitlines()[-1][:200] if result.stderr.strip() else 'no output'}",
            "Check the span lies inside the source and the video file is intact.",
        )

    update_source(source_id, frames_sampled=used + len(frames))

    cues = audio_cues(video, start, end)
    markers = sorted(
        {
            m.group(0).lower()
            for seg in segments
            if seg["end"] > start and seg["start"] < end
            for m in TRANSCRIPT_MARKERS.finditer(seg["text"])
        }
    )

    step = 1.0 / fps
    return {
        "frames": [
            {"path": str(path), "t": round(start + i * step, 2)} for i, path in enumerate(frames)
        ],
        "returned": len(frames),
        "total_available": wanted,
        "truncated": len(frames) < wanted,
        "budget_used": used + len(frames),
        "budget_total": budget,
        "cues": {
            "audio_spikes": cues.get("spikes", []),
            "transcript_markers": markers,
            "two_cue_agreement": bool(cues.get("spikes")) and bool(markers),
        },
    }


# --------------------------------------------------------------------------
# Planning — deterministic grouping into clip definitions
# --------------------------------------------------------------------------


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-z']{3,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _fit_budget(cands: list[dict[str, Any]], max_s: float) -> list[dict[str, Any]]:
    """Take the highest-scoring candidates that fit the duration budget, then re-order in time."""
    chosen: list[dict[str, Any]] = []
    total = 0.0
    for cand in sorted(cands, key=lambda c: -c["score"]):
        length = cand["end"] - cand["start"]
        if total + length > max_s:
            continue
        chosen.append(cand)
        total += length
    return sorted(chosen, key=lambda c: c["start"])


def _dominant_label(cands: list[dict[str, Any]]) -> str:
    return Counter(c["label"] for c in cands).most_common(1)[0][0]


def _make_clip(
    source_id: str, members: list[dict[str, Any]], mode: str, project: str
) -> dict[str, Any]:
    label = _dominant_label(members)
    params = assembly_params(label)
    duration = sum(m["end"] - m["start"] for m in members)
    return {
        "_protocol": "sift/clip/v1",
        "clip_id": new_clip_id(),
        "source_id": source_id,
        "project": project,
        "created": now_iso(),
        "label": label,
        "members": [m["candidate_id"] for m in members],
        "spec": params,
        "mode": mode,
        "est_duration_s": round(min(duration, get_max_clip_s()), 2),
        "source_start": round(min(m["start"] for m in members), 2),
        "source_end": round(max(m["end"] for m in members), 2),
        "score": round(max(m["score"] for m in members), 2),
        "reason": max(members, key=lambda m: m["score"]).get("reason", ""),
    }


def _group_by_topic(cands: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Greedy agglomerative clustering on content-word overlap. Deterministic."""
    remaining = sorted(cands, key=lambda c: -c["score"])
    clusters: list[list[dict[str, Any]]] = []
    vocab: list[set[str]] = []

    for cand in remaining:
        words = _content_words(cand.get("text", "") + " " + cand.get("reason", ""))
        placed = False
        for i, seen in enumerate(vocab):
            if _jaccard(words, seen) >= 0.2:
                clusters[i].append(cand)
                vocab[i] = seen | words
                placed = True
                break
        if not placed:
            clusters.append([cand])
            vocab.append(words)
    return clusters


def _top_phrase(cands: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """The phrase shared by the most candidates, and the candidates containing it.

    Ranks by *how many candidates* contain a phrase, not how often it occurs — a word
    said five times in one candidate is not a supercut. Single words count, because an
    entity supercut ("every mention of funding") is the commonest kind; the longest
    phrase wins a tie, so "raise money" beats a bare "money".
    """
    doc_freq: Counter[str] = Counter()
    for cand in cands:
        words = re.findall(r"[a-z']+", (cand.get("text") or "").lower())
        grams: set[str] = set()
        for size in (3, 2, 1):
            for i in range(len(words) - size + 1):
                gram_words = words[i : i + size]
                if all(w in STOPWORDS for w in gram_words):
                    continue
                if size == 1 and len(gram_words[0]) < 4:
                    continue  # too short to be an entity worth cutting on
                grams.add(" ".join(gram_words))
        doc_freq.update(grams)  # a set per candidate => this counts candidates, not repeats

    best_key: tuple[int, int, int] | None = None
    best_phrase = ""
    for phrase, freq in doc_freq.items():
        if freq < 2:
            continue
        key = (freq, len(phrase.split()), len(phrase))
        if best_key is None or key > best_key:
            best_key, best_phrase = key, phrase

    if not best_phrase:
        return "", []

    pattern = re.compile(rf"\b{re.escape(best_phrase)}\b")
    hits = [c for c in cands if pattern.search((c.get("text") or "").lower())]
    return best_phrase, sorted(hits, key=lambda c: c["start"])


def plan_clips(source_id: str, mode: str, project: str) -> list[dict[str, Any]]:
    """Group stored candidates into clip definitions. A pure function of the YAML records."""
    if mode not in PLAN_MODES:
        raise SelectError(f"Unknown plan mode {mode!r}", f"Use one of: {' '.join(PLAN_MODES)}")

    cands = [c for c in load_candidates(source_id) if c["end"] - c["start"] >= MIN_CLIP_S]
    if not cands:
        raise SelectError(
            f"No candidates of at least {MIN_CLIP_S:.0f}s stored for {source_id}",
            "Call add_candidates(source_id, [...]) first.",
        )

    max_s = get_max_clip_s()
    clips: list[dict[str, Any]] = []

    if mode == "auto":
        for cand in sorted(cands, key=lambda c: -c["score"]):
            capped = dict(cand)
            capped["end"] = min(
                capped["end"], capped["start"] + assembly_params(cand["label"])["max_duration_s"]
            )
            clips.append(_make_clip(source_id, [capped], mode, project))

    elif mode == "by_label":
        for label in sorted({c["label"] for c in cands}):
            group = _fit_budget([c for c in cands if c["label"] == label], max_s)
            if group:
                clips.append(_make_clip(source_id, group, mode, project))

    elif mode == "by_topic":
        for cluster in _group_by_topic(cands):
            group = _fit_budget(cluster, max_s)
            if group:
                clips.append(_make_clip(source_id, group, mode, project))

    elif mode == "montage":
        group = _fit_budget(cands, max_s)
        if group:
            clips.append(_make_clip(source_id, group, mode, project))

    elif mode == "supercut":
        phrase, hits = _top_phrase(cands)
        if not hits:
            raise SelectError(
                "No phrase repeats across two or more candidates — nothing to supercut",
                'Add more candidates containing the repeated phrase, or use mode="montage".',
            )
        group = _fit_budget(hits, max_s)
        clip = _make_clip(source_id, group, mode, project)
        clip["phrase"] = phrase
        clips.append(clip)

    if not clips:
        raise SelectError(
            f"mode={mode!r} produced no clips within the {max_s:.0f}s budget",
            'Every candidate is longer than the clip budget. Tighten spans, or use mode="auto".',
        )

    # Replanning replaces the definitions, but never an already-rendered clip.mp4 —
    # a published artifact is immutable, and a re-render produces a new clip_id.
    clear_clips(source_id)
    for clip in clips:
        save_clip(clip)
    return clips


def get_clip(clip_id: str) -> dict[str, Any]:
    """Load a clip definition (clip.yaml) with its member candidates resolved."""
    return load_clip(clip_id)
