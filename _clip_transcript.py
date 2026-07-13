"""Transcript parsing, overlapping chunk windows, and ASS caption generation.

Three jobs, all pure functions of their inputs — no model, no network, no state:

1. **Parse.** yt-dlp's ``json3`` carries per-word timing; ``vtt`` is cue-level
   (though YouTube's auto-VTT smuggles inline word timestamps, which we read when
   present). Both normalize to one shape so downstream stages never branch on format.
2. **Chunk.** Fixed windows with a look-back overlap, so a moment straddling a
   boundary appears whole in at least one window.
3. **ASS.** The words and their timings already exist — captions are a mechanical
   reformat of the same data, never a re-transcription. Word-level input yields
   word-pop captions; cue-level input yields styled lines, because that is all the
   data supports.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from _clip_helpers import assets_font_dir, get_overlap_s, get_window_s

log = logging.getLogger("clipper.transcript")

# Caption geometry, in the 1080x1920 output space.
PLAY_RES_X = 1080
PLAY_RES_Y = 1920
FONT_NAME = "Liberation Sans"
FONT_FILE = "LiberationSans-Bold.ttf"

COLOR_BASE = "&H00FFFFFF"  # white          (ASS colours are &HAABBGGRR)
COLOR_POP = "&H0000D7FF"  # gold highlight
COLOR_OUTLINE = "&H00101010"
COLOR_SHADOW = "&HA0000000"

# Words per caption line, by the caption_style a label's skill asks for.
WORDS_PER_LINE = {
    "key_phrase": 4,
    "punchline": 4,
    "dual_speaker": 5,
    "minimal": 3,
    "sparse": 3,
}
LINE_MAX_CHARS = 30
LINE_MAX_S = 3.5
LINE_GAP_S = 0.7


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_json3(raw: str) -> dict[str, Any]:
    """Parse yt-dlp ``json3`` subtitles into words + segments (word-level timing)."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"json3 subtitle file is not valid JSON: {exc}") from exc

    words: list[dict[str, Any]] = []
    for event in doc.get("events") or []:
        segs = event.get("segs")
        if not segs:
            continue  # window/pen definition event, carries no text
        base_ms = int(event.get("tStartMs") or 0)
        dur_ms = int(event.get("dDurationMs") or 0)
        event_end = (base_ms + dur_ms) / 1000.0

        # Offsets are relative to the event start; the last word runs to event end.
        starts: list[float] = []
        texts: list[str] = []
        for seg in segs:
            text = (seg.get("utf8") or "").strip()
            if not text:
                continue  # whitespace-only joiner seg
            starts.append((base_ms + int(seg.get("tOffsetMs") or 0)) / 1000.0)
            texts.append(text)

        for i, (start, text) in enumerate(zip(starts, texts, strict=True)):
            end = starts[i + 1] if i + 1 < len(starts) else event_end
            if end <= start:
                end = start + 0.12
            words.append({"w": text, "s": round(start, 3), "e": round(end, 3)})

    words = _dedup_words(words)
    return {
        "kind": "json3",
        "has_words": bool(words),
        "words": words,
        "segments": group_words_into_segments(words),
    }


_VTT_CUE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
_VTT_INLINE_TS = re.compile(r"<(\d{2}:\d{2}:\d{2}[.,]\d{3})>")
_VTT_TAG = re.compile(r"</?[cvbiu][^>]*>|</?\d{2}:\d{2}:\d{2}[.,]\d{3}>")


def _vtt_time(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    seconds = float(rest.replace(",", "."))
    return int(hours) * 3600 + int(minutes) * 60 + seconds


def parse_vtt(raw: str) -> dict[str, Any]:
    """Parse WebVTT into segments. Reads inline word timestamps when present.

    Scans line by line rather than splitting on blank lines. Real-world VTT does not
    honour the blank-line separator — TED's own captions omit it before 35 of 415 cues
    — and a block-splitting parser silently swallows those cues into the previous one,
    leaking raw timestamps into the caption text. A timing line always starts a new cue,
    blank line or not.
    """
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []

    open_cue: dict[str, Any] | None = None

    def close(cue: dict[str, Any] | None) -> None:
        if not cue:
            return
        body = "\n".join(cue["body"])
        words.extend(_vtt_inline_words(body, cue["start"], cue["end"]))
        text = re.sub(r"\s+", " ", html.unescape(_VTT_TAG.sub("", body))).strip()
        if text:
            segments.append(
                {"start": round(cue["start"], 3), "end": round(cue["end"], 3), "text": text}
            )

    for line in raw.replace("\r\n", "\n").split("\n"):
        match = _VTT_CUE.search(line)
        if match:
            if open_cue and open_cue["body"] and open_cue["body"][-1].strip().isdigit():
                open_cue["body"].pop()  # a bare cue identifier, not speech
            close(open_cue)
            open_cue = {
                "start": _vtt_time(match.group("start")),
                "end": _vtt_time(match.group("end")),
                "body": [],
            }
            continue

        if open_cue is None:
            continue  # WEBVTT header, NOTE, STYLE, or a cue id before its timing line
        if not line.strip():
            close(open_cue)
            open_cue = None
            continue
        open_cue["body"].append(line)

    close(open_cue)

    segments = _dedup_segments(segments)
    words = _dedup_words(words)
    return {
        "kind": "vtt",
        "has_words": bool(words),
        "words": words,
        "segments": group_words_into_segments(words) if words else segments,
    }


def _vtt_inline_words(body: str, cue_start: float, cue_end: float) -> list[dict[str, Any]]:
    """Extract ``<00:00:01.234><c> word</c>`` word timings, if this cue carries them.

    A rolling cue repeats the previous line above the new one as carry-over context.
    Only the line bearing inline timestamps is new speech — taking the carry-over at
    face value would emit every word twice.
    """
    timed_lines = [line for line in body.split("\n") if _VTT_INLINE_TS.search(line)]
    if not timed_lines:
        return []

    parts = _VTT_INLINE_TS.split("\n".join(timed_lines))
    # parts = [text_before, ts1, text1, ts2, text2, ...]
    out: list[dict[str, Any]] = []
    pending: list[tuple[float, str]] = []

    leading = html.unescape(_VTT_TAG.sub("", parts[0])).strip()
    if leading:
        pending.append((cue_start, leading))
    for i in range(1, len(parts) - 1, 2):
        stamp = _vtt_time(parts[i])
        text = html.unescape(_VTT_TAG.sub("", parts[i + 1])).strip()
        if text:
            pending.append((stamp, text))

    for i, (start, chunk) in enumerate(pending):
        end = pending[i + 1][0] if i + 1 < len(pending) else cue_end
        tokens = chunk.split()
        if not tokens:
            continue
        # A chunk is usually one word; if several, spread them evenly across the span.
        span = max(end - start, 0.12) / len(tokens)
        for j, token in enumerate(tokens):
            out.append(
                {
                    "w": token,
                    "s": round(start + j * span, 3),
                    "e": round(start + (j + 1) * span, 3),
                }
            )
    return out


def _dedup_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the repeats YouTube's rolling captions emit; keep chronological order."""
    words.sort(key=lambda w: (w["s"], w["e"]))
    out: list[dict[str, Any]] = []
    for word in words:
        if out:
            prev = out[-1]
            same_word = prev["w"] == word["w"]
            if same_word and abs(prev["s"] - word["s"]) < 0.05:
                continue
        out.append(word)
    return out


def _dedup_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the repeats rolling captions emit — but only the *contiguous* ones.

    A rolling caption re-emits the same line immediately, so the repeat butts right up
    against the original. A speaker genuinely saying "Exactly." twice, a minute apart, is
    not a repeat — merging those would silently rewrite the transcript, so contiguity is
    required, not just equal text.
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        if not out:
            out.append(seg)
            continue

        prev = out[-1]
        contiguous = seg["start"] - prev["end"] < 0.5

        if contiguous and prev["text"] == seg["text"]:
            prev["end"] = max(prev["end"], seg["end"])
            continue
        if contiguous and seg["text"].startswith(prev["text"]) and seg["start"] - prev["start"] < 2:
            out[-1] = seg  # the later cue is a superset of the earlier partial line
            continue
        out.append(seg)
    return out


def group_words_into_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group words into readable, sentence-ish segments for transcript reads."""
    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        segments.append(
            {
                "start": round(current[0]["s"], 3),
                "end": round(current[-1]["e"], 3),
                "text": " ".join(w["w"] for w in current),
            }
        )
        current.clear()

    for word in words:
        if current:
            gap = word["s"] - current[-1]["e"]
            span = word["e"] - current[0]["s"]
            ends_sentence = current[-1]["w"].endswith((".", "?", "!"))
            if gap > 1.2 or span > 12.0 or len(current) >= 30 or (ends_sentence and span > 4.0):
                flush()
        current.append(word)
    flush()
    return segments


def parse_transcript(path: str | Path, kind: str) -> dict[str, Any]:
    """Parse a subtitle file into the normalized transcript shape."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    parsed = parse_json3(raw) if kind == "json3" else parse_vtt(raw)
    segments = parsed["segments"]
    parsed["duration"] = round(segments[-1]["end"], 3) if segments else 0.0
    return parsed


# --------------------------------------------------------------------------
# Chunking — overlapping windows
# --------------------------------------------------------------------------


def chunk_bounds(
    index: int, window_s: float | None = None, overlap_s: float | None = None
) -> tuple[float, float]:
    """Time bounds of chunk ``index``. Stride = window - overlap: 0-10, 8-18, 16-26…"""
    window = window_s if window_s is not None else get_window_s()
    overlap = overlap_s if overlap_s is not None else get_overlap_s()
    stride = max(window - overlap, 1.0)
    start = index * stride
    return start, start + window


def chunk_count(
    duration: float, window_s: float | None = None, overlap_s: float | None = None
) -> int:
    """How many windows cover ``duration``. Always at least 1."""
    window = window_s if window_s is not None else get_window_s()
    overlap = overlap_s if overlap_s is not None else get_overlap_s()
    stride = max(window - overlap, 1.0)
    if duration <= window:
        return 1
    return int(-(-(duration - window) // stride)) + 1


def slice_segments(
    segments: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """Segments that overlap the window at all — a segment straddling the edge is kept."""
    return [seg for seg in segments if seg["end"] > start and seg["start"] < end]


def words_in_span(words: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    """Words whose midpoint falls inside the span (avoids duplicating edge words)."""
    out = []
    for word in words:
        mid = (word["s"] + word["e"]) / 2
        if start <= mid < end:
            out.append(word)
    return out


def span_text(segments: list[dict[str, Any]], start: float, end: float) -> str:
    """Flat text of a span — used to give a stored candidate its quotable text."""
    return " ".join(seg["text"] for seg in slice_segments(segments, start, end)).strip()


# --------------------------------------------------------------------------
# ASS caption generation
# --------------------------------------------------------------------------


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _header(font_size: int, margin_v: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Clip,{FONT_NAME},{font_size},{COLOR_BASE},{COLOR_POP},{COLOR_OUTLINE},{COLOR_SHADOW},-1,0,0,0,100,100,0,0,1,4,2,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_caption_lines(
    words: list[dict[str, Any]], caption_style: str = "key_phrase"
) -> list[list[dict[str, Any]]]:
    """Group words into short caption lines, breaking on gaps, length, and punctuation."""
    per_line = WORDS_PER_LINE.get(caption_style, 4)
    lines: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    for word in words:
        if current:
            gap = word["s"] - current[-1]["e"]
            span = word["e"] - current[0]["s"]
            chars = len(" ".join(w["w"] for w in current)) + 1 + len(word["w"])
            ends_sentence = current[-1]["w"].endswith((".", "?", "!", ","))
            if (
                len(current) >= per_line
                or gap > LINE_GAP_S
                or span > LINE_MAX_S
                or chars > LINE_MAX_CHARS
                or (ends_sentence and len(current) >= 2)
            ):
                lines.append(current)
                current = []
        current.append(word)
    if current:
        lines.append(current)
    return lines


def build_ass_word_pop(
    words: list[dict[str, Any]],
    caption_style: str = "key_phrase",
    font_size: int = 76,
    margin_v: int = 420,
) -> str:
    """Word-pop ASS: the whole line stays up, the spoken word is highlighted.

    ``words`` must already be in *output* time (the render stage remaps them
    through the trim/concat timeline before calling this).
    """
    parts = [_header(font_size, margin_v)]

    for line in build_caption_lines(words, caption_style):
        line_start = line[0]["s"]
        line_end = max(line[-1]["e"], line_start + 0.3)
        tokens = [_ass_escape(w["w"]) for w in line]

        for i, word in enumerate(line):
            start = line_start if i == 0 else max(word["s"], line_start)
            end = line[i + 1]["s"] if i + 1 < len(line) else line_end
            end = max(end, start + 0.05)
            if end > line_end:
                end = line_end

            rendered = []
            for j, token in enumerate(tokens):
                if j == i:
                    # Inline colour overrides take a trailing '&'; the Style line does not.
                    rendered.append(
                        f"{{\\c{COLOR_POP}&\\fscx108\\fscy108}}{token}"
                        f"{{\\c{COLOR_BASE}&\\fscx100\\fscy100}}"
                    )
                else:
                    rendered.append(token)
            text = " ".join(rendered)
            parts.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Clip,,0,0,0,,"
                f"{{\\c{COLOR_BASE}&}}{text}\n"
            )

    return "".join(parts)


def build_ass_lines(
    segments: list[dict[str, Any]],
    font_size: int = 68,
    margin_v: int = 420,
) -> str:
    """Cue-level ASS: styled lines, no word pop — the timing data doesn't support it."""
    parts = [_header(font_size, margin_v)]
    for seg in segments:
        start = seg["start"]
        end = max(seg["end"], start + 0.3)
        text = _ass_escape(seg["text"])
        words = text.split()
        if len(words) > 6:  # wrap to two balanced lines
            mid = (len(words) + 1) // 2
            text = " ".join(words[:mid]) + "\\N" + " ".join(words[mid:])
        parts.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Clip,,0,0,0,,{text}\n")
    return "".join(parts)


def font_path() -> Path:
    """Absolute path to the bundled caption font. Never a system font lookup."""
    return assets_font_dir() / FONT_FILE
