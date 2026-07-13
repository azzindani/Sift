"""Tests for the pipeline. The render tests run real ffmpeg — nothing here is mocked.

The integration test at the bottom is the one that matters: it drives fetch-through-
publish on a synthetic source and asserts on the *actual encoded bytes* (dimensions,
duration, audio presence), because every interesting bug in this codebase lives in the
filtergraph, not in the Python around it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import engine
from _clip_helpers import connect, error_result, ok_result
from _clip_publish import source_deep_link
from _clip_queue import get_job as queue_get_job
from _clip_queue import reconcile, wait_for
from _clip_reframe import crop_x_expr
from _clip_render import (
    Segment,
    Timeline,
    build_timeline,
    detect_silences,
    keep_segments,
)
from _clip_select import cold_open_warning, merge_all, overlap_ratio
from _clip_transcript import (
    build_ass_word_pop,
    chunk_bounds,
    chunk_count,
    parse_json3,
    parse_vtt,
    slice_segments,
)
from shared.file_utils import PathError, resolve_path
from shared.platform_utils import ffprobe_bin, run
from tests.conftest import SILENCE_S, SPEECH_S

# --------------------------------------------------------------------------
# The response contract
# --------------------------------------------------------------------------


def test_every_result_has_success_progress_and_token_estimate():
    good = ok_result("demo", ["[ok] done"], value=1)
    assert list(good)[0] == "success"
    assert good["success"] is True
    assert good["progress"] == ["[ok] done"]
    assert good["token_estimate"] > 0

    bad = error_result("it broke", "do this instead")
    assert list(bad)[0] == "success"
    assert bad["success"] is False
    assert bad["error"] and bad["hint"]
    assert bad["token_estimate"] > 0


def test_no_tool_raises_on_bad_input():
    """The error contract: every failure is a dict with a hint, never an exception."""
    calls = [
        engine.fetch_source("gopher://nope"),
        engine.read_transcript_chunk("s_missing", 0),
        engine.sample_frames("s_missing", 0, 10, 1.0),
        engine.get_job("j_missing"),
        engine.add_candidates("s_missing", []),
        engine.plan_clips("s_missing", "auto"),
        engine.render_clip("c_missing"),
        engine.publish_outputs(["j_missing"]),
    ]
    for result in calls:
        assert result["success"] is False
        assert result["error"] and result["hint"]
        assert "token_estimate" in result and "progress" in result


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------


def test_json3_carries_word_timing(json3_text):
    parsed = parse_json3(json3_text)
    assert parsed["kind"] == "json3"
    assert parsed["has_words"] is True

    words = parsed["words"]
    assert [w["w"] for w in words[:4]] == ["The", "thing", "nobody", "tells"]
    assert words[0]["s"] == pytest.approx(0.5, abs=0.01)
    assert words[1]["s"] == pytest.approx(0.82, abs=0.01)  # 500ms + 320ms offset

    # Words are ordered and each has positive duration.
    for word in words:
        assert word["e"] > word["s"]
    assert all(a["s"] <= b["s"] for a, b in zip(words, words[1:], strict=False))


def test_json3_skips_window_events_and_blank_segs(json3_text):
    parsed = parse_json3(json3_text)
    # The first event is a window definition with no segs, and there are whitespace-only
    # joiner segs. Neither may become a "word".
    assert all(w["w"].strip() for w in parsed["words"])


def test_vtt_is_cue_level(vtt_text):
    parsed = parse_vtt(vtt_text)
    assert parsed["kind"] == "vtt"
    assert parsed["has_words"] is False  # cue-level only: no fake word timing
    assert parsed["segments"][0]["text"].startswith("The thing nobody tells you")
    assert parsed["segments"][0]["start"] == pytest.approx(0.5)


def test_vtt_cue_without_a_preceding_blank_line_is_not_swallowed():
    """Real VTT omits the blank-line separator. TED's own captions do it 35 times in 415.

    A block-splitting parser merges such a cue into the previous one and leaks the raw
    "00:00:28.230 --> 00:00:30.097" timestamp straight into the caption text.
    """
    raw = (Path(__file__).parent / "fixtures" / "no_blank_lines.vtt").read_text(encoding="utf-8")
    parsed = parse_vtt(raw)
    texts = [seg["text"] for seg in parsed["segments"]]

    assert not any("-->" in t for t in texts), f"raw timestamp leaked into caption: {texts}"
    assert "the same agencies," in texts
    assert "the same consultants, the same media." in texts  # the cue with no blank line before it

    # Each cue keeps its own timing rather than inheriting the previous cue's.
    consultants = next(
        s for s in parsed["segments"] if s["text"].startswith("the same consultants")
    )
    assert consultants["start"] == pytest.approx(28.23)


def test_vtt_dedup_does_not_merge_a_genuinely_repeated_line():
    """ "Exactly." said twice, a minute apart, is two lines — not a rolling-caption repeat."""
    raw = (Path(__file__).parent / "fixtures" / "no_blank_lines.vtt").read_text(encoding="utf-8")
    parsed = parse_vtt(raw)
    assert [s["text"] for s in parsed["segments"]].count("Exactly.") == 2


def test_vtt_reads_inline_word_timing_and_dedups_rolling_cues(rolling_vtt_text):
    parsed = parse_vtt(rolling_vtt_text)
    assert parsed["has_words"] is True  # YouTube's auto-VTT smuggles word timestamps

    words = [w["w"] for w in parsed["words"]]
    # The rolling repeats ("the thing nobody tells you" re-emitted verbatim) must collapse.
    assert words.count("nobody") == 1
    assert words.count("funding") == 1
    assert "simple" in words


# --------------------------------------------------------------------------
# Chunking — the overlap is the whole point
# --------------------------------------------------------------------------


def test_chunks_overlap_by_the_lookback():
    # 10-minute window, 2-minute look-back => 0-10, 8-18, 16-26 ...
    assert chunk_bounds(0, 600, 120) == (0.0, 600.0)
    assert chunk_bounds(1, 600, 120) == (480.0, 1080.0)
    assert chunk_bounds(2, 600, 120) == (960.0, 1560.0)

    # Consecutive windows overlap by exactly the look-back.
    _, first_end = chunk_bounds(0, 600, 120)
    second_start, _ = chunk_bounds(1, 600, 120)
    assert first_end - second_start == pytest.approx(120.0)


def test_chunk_count_covers_the_whole_source():
    assert chunk_count(300, 600, 120) == 1
    assert chunk_count(600, 600, 120) == 1
    assert chunk_count(601, 600, 120) == 2

    # Every second of a 2-hour source falls inside some window.
    duration = 7200.0
    total = chunk_count(duration, 600, 120)
    assert chunk_bounds(total - 1, 600, 120)[1] >= duration


def test_a_moment_straddling_a_boundary_survives_whole_in_one_window():
    """The reason the overlap exists: a segment across a cut must appear intact somewhere."""
    segments = [{"start": 595.0, "end": 605.0, "text": "straddles the 10-minute boundary"}]
    first = slice_segments(segments, *chunk_bounds(0, 600, 120))
    second = slice_segments(segments, *chunk_bounds(1, 600, 120))
    assert first and second  # visible in both, and fully inside the second's lead-in
    window_start, window_end = chunk_bounds(1, 600, 120)
    assert window_start <= segments[0]["start"] and segments[0]["end"] <= window_end


# --------------------------------------------------------------------------
# Candidate merge
# --------------------------------------------------------------------------


def test_overlap_ratio_is_over_the_shorter_span():
    a = {"start": 0.0, "end": 100.0}
    b = {"start": 90.0, "end": 100.0}  # fully inside a
    assert overlap_ratio(a, b) == pytest.approx(1.0)

    c = {"start": 0.0, "end": 10.0}
    d = {"start": 9.0, "end": 20.0}
    assert overlap_ratio(c, d) == pytest.approx(0.1)


def test_merge_keeps_union_boundaries_and_max_score():
    """A merge must never lose the wider framing or the better score."""
    merged, fusions = merge_all(
        [
            {
                "start": 10.0,
                "end": 40.0,
                "label": "quote",
                "score": 6,
                "reason": "narrow",
                "cues": {"text": True},
            },
            {
                "start": 5.0,
                "end": 38.0,
                "label": "quote",
                "score": 9,
                "reason": "wide",
                "cues": {"audio_spike": True},
            },
        ]
    )
    assert fusions == 1
    assert len(merged) == 1
    assert merged[0]["start"] == 5.0  # union: earliest start
    assert merged[0]["end"] == 40.0  # union: latest end
    assert merged[0]["score"] == 9  # max score
    assert merged[0]["reason"] == "wide"  # the higher-scoring framing wins
    assert merged[0]["cues"] == {"audio_spike": True, "text": True}  # cues union


def test_barely_overlapping_candidates_stay_separate():
    merged, fusions = merge_all(
        [
            {"start": 0.0, "end": 30.0, "label": "quote", "score": 7, "reason": "", "cues": {}},
            {"start": 29.0, "end": 60.0, "label": "joke", "score": 8, "reason": "", "cues": {}},
        ]
    )
    assert fusions == 0 and len(merged) == 2  # 1s / 30s overlap is not the same moment


def test_merge_is_transitive_and_converges():
    """A chain of >50% overlaps collapses to one span rather than oscillating.

    C only overlaps B, and only becomes adjacent to A once A and B have fused — so
    the merge has to keep re-examining the growing span, not make one pass and stop.
    """
    merged, fusions = merge_all(
        [
            {"start": 0.0, "end": 20.0, "label": "quote", "score": 5, "reason": "a", "cues": {}},
            {"start": 10.0, "end": 28.0, "label": "quote", "score": 7, "reason": "b", "cues": {}},
            {"start": 20.0, "end": 30.0, "label": "quote", "score": 6, "reason": "c", "cues": {}},
        ]
    )
    assert len(merged) == 1
    assert (merged[0]["start"], merged[0]["end"]) == (0.0, 30.0)
    assert merged[0]["score"] == 7
    assert fusions == 2


def test_partial_overlap_below_the_threshold_is_left_alone():
    """25% overlap is two different moments that happen to touch — merging them is a bug."""
    merged, fusions = merge_all(
        [
            {"start": 0.0, "end": 20.0, "label": "quote", "score": 5, "reason": "", "cues": {}},
            {"start": 15.0, "end": 35.0, "label": "quote", "score": 7, "reason": "", "cues": {}},
            {"start": 30.0, "end": 50.0, "label": "quote", "score": 6, "reason": "", "cues": {}},
        ]
    )
    assert fusions == 0 and len(merged) == 3


def test_cold_open_pronoun_is_soft_flagged():
    assert cold_open_warning("That is exactly why he left.", 120.0)
    assert cold_open_warning("He never told anyone.", 10.0)
    assert not cold_open_warning("Funding is a trap for founders.", 10.0)


def test_a_span_starting_in_the_title_card_is_soft_flagged():
    """The transcript starts before the picture does.

    TED's first spoken word lands at 0.4s — but the first 3.3s of *video* are a logo sting
    and a full second of black. The first live batch shipped a clip that opened exactly
    there: the entire hook was someone else's branding, with a caption over it.
    """
    warning = cold_open_warning("Teenagers today are amazing.", 0.4)
    assert warning
    assert "title card" in warning
    assert "sample_frames" in warning  # the hint names the tool that can actually look

    # Well past the intro, a clean opening line is not flagged at all.
    assert not cold_open_warning("Teenagers today are amazing.", 600.0)


# --------------------------------------------------------------------------
# Silence trim
# --------------------------------------------------------------------------


def test_keep_segments_drops_long_silence_but_keeps_natural_pauses():
    silences = [(12.0, 12.4), (20.0, 23.0)]  # a 0.4s beat, and 3s of dead air
    kept = keep_segments(10.0, 30.0, silences, drop_threshold_s=0.5, pad_s=0.15)

    # The short pause survives (cutting it would make the edit sound frantic).
    assert any(s <= 12.0 and e >= 12.4 for s, e in kept)
    # The long silence is dropped, with padding left on each side.
    assert len(kept) == 2
    assert kept[0][1] == pytest.approx(20.15)
    assert kept[1][0] == pytest.approx(22.85)


def test_keep_segments_snaps_boundaries_off_silence():
    """A span starting mid-silence must snap forward to speech, not open on dead air."""
    silences = [(10.0, 14.0), (28.0, 32.0)]
    kept = keep_segments(11.0, 30.0, silences, drop_threshold_s=0.5, pad_s=0.15)
    assert kept[0][0] == pytest.approx(13.85)  # snapped to the end of the leading silence
    assert kept[-1][1] == pytest.approx(28.15)  # snapped back off the trailing silence


def test_keep_segments_never_returns_nothing():
    """An all-silent span degrades to the raw span rather than producing an empty clip."""
    kept = keep_segments(0.0, 10.0, [(0.0, 10.0)], drop_threshold_s=0.5, pad_s=0.15)
    assert kept == [(0.0, 10.0)]


def test_a_cut_that_would_save_almost_nothing_is_not_made():
    """Padding can shrink a drop to a rounding error, and the jump cut still costs full price.

    A 0.62s silence with 250ms of pad on each side removes 0.12s of audio — and leaves a
    hard visual discontinuity to save a tenth of a second. Not worth it.
    """
    kept = keep_segments(0.0, 30.0, [(10.0, 10.62)], drop_threshold_s=0.5, pad_s=0.25)
    assert kept == [(0.0, 30.0)], "the pause should have been left alone"


def test_the_trim_thresholds_do_not_cut_breath_pauses():
    """The first live batch put TEN hard cuts in a 37s clip to remove 4.9s of "dead air".

    Some of what it cut was 0.35s long — a breath between clauses. Every drop is a visual
    jump cut, so a threshold that fires on a breath buys a fraction of a second and pays
    for it with a discontinuity the viewer sees. Speech pauses run to ~0.7s.
    """
    from _clip_helpers import TRIM_THRESHOLDS

    assert min(TRIM_THRESHOLDS.values()) >= 0.6, (
        "a threshold under 0.6s cuts natural speech pauses, not dead air"
    )

    # Replay the joke clip's real silences through the tightest preset.
    breaths = [(5.0, 5.43), (9.0, 9.46), (14.0, 14.35)]  # 0.43s, 0.46s, 0.35s — all breaths
    dead_air = [(20.0, 22.43)]  # 2.4s — a real pause
    kept = keep_segments(
        0.0, 30.0, breaths + dead_air, drop_threshold_s=TRIM_THRESHOLDS["very_tight"], pad_s=0.12
    )

    assert len(kept) == 2, f"only the dead air should have been cut, got {len(kept) - 1} cuts"
    assert kept[0][1] == pytest.approx(20.12)
    assert kept[1][0] == pytest.approx(22.31)


def test_detect_silences_finds_the_real_gaps(source_video):
    """Against real audio: the 6s-on/4s-off tone must yield silences at the right places."""
    silences = detect_silences(source_video, 0.0, 30.0, threshold_db=-30, min_silence_s=0.3)
    assert silences, "silencedetect found no silence in a track that is 40% silent"

    # The first gap starts around SPEECH_S and runs about SILENCE_S.
    first = silences[0]
    assert first[0] == pytest.approx(SPEECH_S, abs=0.5)
    assert (first[1] - first[0]) == pytest.approx(SILENCE_S, abs=0.6)


def test_detect_silences_reports_absolute_source_time(source_video):
    """Input seeking rebases timestamps to zero — they must be shifted back, or cuts land wrong."""
    silences = detect_silences(source_video, 20.0, 40.0, threshold_db=-30, min_silence_s=0.3)
    assert silences
    for start, end in silences:
        assert 20.0 <= start <= 40.0, f"silence at {start} is outside the requested span"
        assert 20.0 <= end <= 40.0


# --------------------------------------------------------------------------
# Timeline mapping — captions desync if this is wrong
# --------------------------------------------------------------------------


def test_timeline_maps_words_across_dropped_air():
    timeline = Timeline(
        segments=[
            Segment(src_start=10.0, src_end=20.0, out_start=0.0, member=0),
            Segment(src_start=30.0, src_end=40.0, out_start=10.0, member=0),  # 10s dropped
        ],
        duration=20.0,
    )
    # A word before the second segment's source start still lands right after the first.
    mapped = timeline.map_word({"w": "hello", "s": 30.5, "e": 31.0})
    assert mapped["s"] == pytest.approx(10.5)  # 30.5 - 30.0 + 10.0

    # A word inside the dropped air has nowhere to go, and must not be faked.
    assert timeline.map_word({"w": "gone", "s": 25.0, "e": 25.5}) is None


def test_timeline_crossfade_overlaps_members(source_video):
    """Members join with a crossfade, so member 2 starts CROSSFADE_S earlier than a naive sum."""
    from _clip_render import CROSSFADE_S

    members = [
        {"start": 0.0, "end": 6.0},  # tone, no internal silence
        {"start": 10.0, "end": 16.0},
    ]
    spec = {
        "trim_aggressiveness": "gentle",
        "pad_ms": 100,
        "silence_threshold_db": -30,
        "max_duration_s": 60,
    }
    timeline = build_timeline(source_video, members, spec)

    second = next(seg for seg in timeline.segments if seg.member == 1)
    first_total = sum(seg.duration for seg in timeline.segments if seg.member == 0)
    assert second.out_start == pytest.approx(first_total - CROSSFADE_S, abs=0.05)


def test_timeline_respects_the_duration_budget(source_video):
    members = [
        {"start": 0.0, "end": 6.0},
        {"start": 10.0, "end": 16.0},
        {"start": 20.0, "end": 26.0},
    ]
    spec = {
        "trim_aggressiveness": "gentle",
        "pad_ms": 100,
        "silence_threshold_db": -30,
        "max_duration_s": 8,
    }
    timeline = build_timeline(source_video, members, spec, max_duration_s=8.0)
    assert timeline.duration <= 8.5  # budget honoured (crossfade shortens it slightly)


# --------------------------------------------------------------------------
# Crop expression
# --------------------------------------------------------------------------


def test_crop_expr_gates_are_half_open_so_they_never_double_count():
    """Overlapping gates at a keypoint boundary would double the crop x and fling the frame."""
    expr = crop_x_expr([(0.0, 100.0), (5.0, 200.0), (10.0, 300.0)])
    assert "gte(t,0.000)*lt(t,5.000)" in expr
    assert "gte(t,5.000)*lt(t,10.000)" in expr
    assert "between(" not in expr  # between() is inclusive on both ends: it would double-count
    assert expr.startswith("clip(") and "in_w-out_w" in expr


def test_crop_expr_degrades_to_centre_without_keypoints():
    assert crop_x_expr([]) == "(in_w-out_w)/2"


# --------------------------------------------------------------------------
# The follow/fit layout switch
# --------------------------------------------------------------------------


def _track(hits, sampled):
    from _clip_reframe import FaceTrack

    return FaceTrack(hits=hits, sampled=sampled)


def test_a_faceless_stretch_becomes_a_fit_span():
    """Sampled-and-empty is the signal. Where nobody is on screen, do not crop into it."""
    from _clip_reframe import fit_spans

    timeline = Timeline(segments=[Segment(0.0, 20.0, 0.0, 0)], duration=20.0)
    sampled = [i * 0.5 for i in range(40)]  # 0.0 .. 19.5, every 500ms
    # A face for the first 5s and the last 5s; nothing in between (a wide shot, or a slide).
    hits = [(t, 0.5) for t in sampled if t < 5.0 or t >= 15.0]

    spans = fit_spans(_track(hits, sampled), timeline)

    assert len(spans) == 1
    start, end = spans[0]
    assert 4.9 <= start <= 5.1
    assert 14.9 <= end <= 15.1


def test_a_single_missed_detection_does_not_flip_the_layout():
    """A blink is not a shot change. Flipping the whole frame for 500ms would read as a glitch."""
    from _clip_reframe import fit_spans

    timeline = Timeline(segments=[Segment(0.0, 20.0, 0.0, 0)], duration=20.0)
    sampled = [i * 0.5 for i in range(40)]
    hits = [(t, 0.5) for t in sampled if t not in (7.0,)]  # exactly one empty frame

    assert fit_spans(_track(hits, sampled), timeline) == []


def test_the_fit_layout_keeps_the_whole_frame_and_never_crops():
    """The whole point: nothing is cropped away, so a full-width chart survives intact."""
    from _clip_reframe import fit_chain

    chain = ";".join(fit_chain("[in]", "[out]"))

    assert "crop=270:480" in chain  # the *blurred fill* is cropped — that is fine, it is a backdrop
    assert "scale=1080:-2" in chain  # ...but the visible frame is only ever SCALED
    assert "overlay=" in chain
    # No 9:16 column crop anywhere in the visible path.
    assert "crop=w=" not in chain


def test_the_graph_switches_layouts_with_a_gate_not_by_cutting_the_timeline():
    """Audio must never learn that the video layout moved — so no extra concat joins."""
    from _clip_render import build_filtergraph

    timeline = Timeline(segments=[Segment(0.0, 20.0, 0.0, 0)], duration=20.0)
    graph = build_filtergraph(
        timeline,
        src_w=1920,
        src_h=1080,
        reframe="speaker",
        keypoints=[(0.0, 400.0), (20.0, 400.0)],
        columns=None,
        ass_file="",
        fit_spans=[(5.0, 15.0)],
    )

    assert "overlay=0:0:enable='between(t,5.000,15.000)'" in graph
    assert "crop=w=606" in graph  # the follow branch still exists for the rest of the clip
    assert "gblur" in graph  # ...and so does the fit branch
    assert "concat=" not in graph  # one segment in, one segment out: nothing was re-cut


def test_enable_expr_terms_never_overlap():
    """Summed gates: two overlapping betweens would make enable==2, which is still true, but the
    spans are merged upstream precisely so the expression stays a clean boolean."""
    from _clip_reframe import enable_expr

    assert enable_expr([]) == "0"
    assert enable_expr([(1.0, 2.0), (5.0, 6.5)]) == "between(t,1.000,2.000)+between(t,5.000,6.500)"


def test_smooth_centers_caps_pan_speed_within_a_shot():
    """A speaker pacing the stage must not make the crop lurch after them."""
    from _clip_reframe import MAX_PAN_PX_PER_S, smooth_centers

    timeline = Timeline(
        segments=[Segment(src_start=0.0, src_end=10.0, out_start=0.0, member=0)], duration=10.0
    )
    # Drifting across the frame, but by less per sample than a camera cut would.
    samples = [(0.0, 0.40), (0.5, 0.50), (1.0, 0.60), (1.5, 0.66), (2.0, 0.70)]
    keypoints = smooth_centers(samples, src_w=1280, crop_w=404, timeline=timeline)

    for (t0, x0), (t1, x1) in zip(keypoints, keypoints[1:], strict=False):
        speed = abs(x1 - x0) / max(t1 - t0, 1e-3)
        assert speed <= MAX_PAN_PX_PER_S + 1.0, f"crop lurched at {speed:.0f} px/s"

    for _, x in keypoints:
        assert 0.0 <= x <= 1280 - 404


def test_a_shot_cut_snaps_instead_of_crawling_after_the_new_framing():
    """The bug this replaced: the crop panned ACROSS a cut at 90 px/s.

    Real footage cuts between a wide shot and a close-up, and the subject's position jumps
    hundreds of pixels in one frame. The old code smoothed straight through that — the
    moving average blended the two shots, and the pan cap then crawled toward the new
    position. On a TED talk that left the speaker's face sliced by the frame edge for a
    full second after the cut. A person cannot cross the frame in 500 ms; a camera can.
    """
    from _clip_reframe import SNAP_S, smooth_centers

    timeline = Timeline(
        segments=[Segment(src_start=0.0, src_end=10.0, out_start=0.0, member=0)], duration=10.0
    )
    # Far left, then a hard cut to far right. This is a camera change, not a sprint.
    samples = [(0.0, 0.1), (0.5, 0.1), (1.0, 0.9), (1.5, 0.9), (2.0, 0.9)]
    keypoints = smooth_centers(samples, src_w=1280, crop_w=404, timeline=timeline)

    def crop_at(t: float) -> float:
        prev = keypoints[0]
        for kp in keypoints:
            if kp[0] >= t:
                if kp[0] == prev[0]:
                    return kp[1]
                span = (t - prev[0]) / (kp[0] - prev[0])
                return prev[1] + span * (kp[1] - prev[1])
            prev = kp
        return keypoints[-1][1]

    # Where the face is after the cut, clamped to the frame: the crop cannot hang off the
    # right edge, so the furthest right it can sit is src_w - crop_w.
    target_after = min(0.9 * 1280 - 404 / 2, 1280 - 404)
    before = crop_at(1.0 - SNAP_S - 0.01)
    after = crop_at(1.0 + 0.01)

    # It held the old framing right up to the cut, then jumped — not crawled.
    assert before < 300, f"crop drifted before the cut: {before:.0f}"
    assert abs(after - target_after) < 60, (
        f"crop is at {after:.0f} one frame after the cut but the face is at "
        f"{target_after:.0f} — it is crawling, and the speaker is out of frame"
    )

    for _, x in keypoints:
        assert 0.0 <= x <= 1280 - 404


def test_smooth_centers_maps_onto_output_time_not_source_time():
    """Keypoints feed a crop expression evaluated in output time — a source-time bug desyncs it."""
    from _clip_reframe import smooth_centers

    timeline = Timeline(
        segments=[Segment(src_start=100.0, src_end=110.0, out_start=0.0, member=0)], duration=10.0
    )
    keypoints = smooth_centers([(100.0, 0.5), (105.0, 0.5)], 1280, 404, timeline)
    assert keypoints[0][0] == pytest.approx(0.0)  # source 100s is output 0s
    assert max(t for t, _ in keypoints) <= 10.0


def test_crop_expr_is_evaluated_correctly_by_ffmpeg(source_video, tmp_path):
    """The expression is only useful if ffmpeg actually accepts it — so ask ffmpeg."""
    from shared.platform_utils import ffmpeg_bin

    expr = crop_x_expr([(0.0, 0.0), (2.0, 400.0), (4.0, 100.0)])
    out = tmp_path / "cropped.mp4"
    result = run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            "0",
            "-t",
            "4",
            "-i",
            str(source_video),
            "-vf",
            f"crop=w=404:h=720:x='{expr}':y=0,scale=1080:1920",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-an",
            str(out),
        ],
        timeout=120.0,
    )
    assert result.ok, f"ffmpeg rejected the crop expression:\n{result.stderr[-800:]}"
    assert out.is_file() and out.stat().st_size > 0


# --------------------------------------------------------------------------
# ASS captions
# --------------------------------------------------------------------------


def test_word_pop_highlights_exactly_one_word_per_event():
    words = [
        {"w": "Funding", "s": 0.0, "e": 0.5},
        {"w": "buys", "s": 0.5, "e": 0.9},
        {"w": "time.", "s": 0.9, "e": 1.4},
    ]
    ass = build_ass_word_pop(words, caption_style="key_phrase")
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 3  # one event per word: the highlight moves, the line stays

    for event in events:
        assert event.count("\\c&H0000D7FF&") == 1  # exactly one gold word
        assert "Funding" in event and "buys" in event  # the whole line is always on screen


def test_word_pop_events_are_ordered_and_non_overlapping():
    words = [{"w": f"w{i}", "s": i * 0.4, "e": i * 0.4 + 0.35} for i in range(12)]
    ass = build_ass_word_pop(words)
    times = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            _, start, end, *_ = line.split(",", 3)[0:1] + line.split(",")[1:3] + [""]
            times.append((start, end))
    assert times == sorted(times)  # monotonic: captions never jump backwards


def test_ass_escapes_braces_that_would_break_the_tag_parser():
    ass = build_ass_word_pop([{"w": "{drop}", "s": 0.0, "e": 0.4}])
    event = next(ln for ln in ass.splitlines() if ln.startswith("Dialogue:"))
    assert "{drop}" not in event  # a literal brace would be read as an override tag
    assert "(drop)" in event


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------


def test_resolve_path_rejects_traversal_outside_allowed_roots():
    with pytest.raises(PathError):
        resolve_path("/etc/passwd")
    with pytest.raises(PathError):
        resolve_path("~/../../etc/shadow")


def test_resolve_path_enforces_extension_allowlist(tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# netscape", encoding="utf-8")
    assert resolve_path(cookie, allowed_exts=(".txt",)) == cookie.resolve()

    evil = tmp_path / "cookies.sh"
    evil.write_text("rm -rf /", encoding="utf-8")
    with pytest.raises(PathError):
        resolve_path(evil, allowed_exts=(".txt",))


# --------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------


def test_source_deep_link_lands_on_the_exact_moment():
    assert source_deep_link("https://www.youtube.com/watch?v=abc", 2710.4) == (
        "https://www.youtube.com/watch?v=abc&t=2710s"
    )
    assert source_deep_link("https://youtu.be/abc", 90.0) == "https://youtu.be/abc?t=90s"
    assert "t=45" in source_deep_link("https://example.com/video", 45.9)


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------


def test_reconcile_fails_interrupted_jobs_and_never_leaves_them_running(
    registered_source, tmp_path
):
    """A 'running' row after a restart is a lie — nothing is running it."""
    stale = tmp_path / "stale_job"
    stale.mkdir()
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, clip_id, source_id, status, temp_dir, created_at)
               VALUES ('j_x', 'c_x', ?, 'running', ?, 0)""",
            (registered_source["source_id"], str(stale)),
        )

    assert reconcile() == 1
    job = queue_get_job("j_x")
    assert job["status"] == "failed"
    assert "restart" in job["error"].lower()
    assert job["hint"]
    assert not stale.exists()  # the temp dir is swept, not leaked


# --------------------------------------------------------------------------
# Selection + planning against a real source
# --------------------------------------------------------------------------


def test_add_candidates_validates_before_writing_anything(registered_source):
    source_id = registered_source["source_id"]
    result = engine.add_candidates(
        source_id,
        [
            {"start": 0.5, "end": 10.0, "label": "quote", "score": 8, "reason": "good"},
            {"start": 30.0, "end": 20.0, "label": "quote", "score": 8, "reason": "inverted"},
        ],
    )
    assert result["success"] is False
    assert "inverted" in result["error"]

    # The whole array is validated before a single record is written.
    from _clip_library import load_candidates

    assert load_candidates(source_id) == []


def test_add_candidates_rejects_unknown_label_and_oversized_batch(registered_source):
    source_id = registered_source["source_id"]

    bad_label = engine.add_candidates(
        source_id, [{"start": 0.0, "end": 10.0, "label": "vibes", "score": 8}]
    )
    assert bad_label["success"] is False and "vibes" in bad_label["error"]
    assert "quote" in bad_label["hint"]

    too_many = engine.add_candidates(
        source_id,
        [{"start": float(i), "end": i + 5.0, "label": "quote", "score": 5} for i in range(51)],
    )
    assert too_many["success"] is False and "50" in too_many["hint"]


def test_selection_to_plan_flow(registered_source):
    source_id = registered_source["source_id"]

    added = engine.add_candidates(
        source_id,
        [
            {"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "funding line"},
            {"start": 12.0, "end": 17.0, "label": "quote", "score": 6, "reason": "walked away"},
            {"start": 32.0, "end": 38.0, "label": "argument", "score": 8, "reason": "disagreement"},
        ],
    )
    assert added["success"] is True
    assert added["stored"] == 3

    # The 12.0 candidate opens on "And that is why he..." — a bare reference.
    assert any("bare reference" in w for w in added["warnings"])

    planned = engine.plan_clips(source_id, mode="auto")
    assert planned["success"] is True
    assert planned["clip_count"] == 3
    assert planned["clips"][0]["score"] == 9  # highest score first

    # by_label collapses the two quotes into one multi-cut clip.
    by_label = engine.plan_clips(source_id, mode="by_label")
    assert by_label["success"] is True
    labels = {clip["label"] for clip in by_label["clips"]}
    assert labels == {"quote", "argument"}
    quote_clip = next(c for c in by_label["clips"] if c["label"] == "quote")
    assert quote_clip["members"] == 2
    assert quote_clip["reframe"] == "speaker"

    argument_clip = next(c for c in by_label["clips"] if c["label"] == "argument")
    assert argument_clip["reframe"] == "stacked"  # the label's skill drives the layout


def test_supercut_groups_a_repeated_phrase(registered_source):
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id,
        [
            {"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "a"},
            {"start": 32.0, "end": 38.0, "label": "quote", "score": 8, "reason": "b"},
        ],
    )
    result = engine.plan_clips(source_id, mode="supercut")
    assert result["success"] is True
    assert "funding" in result["clips"][0]["phrase"]  # both candidates say "funding"


def test_frame_budget_is_enforced(registered_source, monkeypatch):
    source_id = registered_source["source_id"]
    monkeypatch.setattr("_clip_select.get_frame_budget", lambda: 4)
    monkeypatch.setattr("_clip_select.get_max_frames_per_call", lambda: 4)

    first = engine.sample_frames(source_id, 0.0, 20.0, fps=1.0)
    assert first["success"] is True
    assert first["returned"] <= 4
    assert first["truncated"] is True  # 20 wanted, 4 allowed
    assert first["budget_used"] <= 4

    second = engine.sample_frames(source_id, 20.0, 40.0, fps=1.0)
    assert second["success"] is False
    assert "budget exhausted" in second["error"]
    assert second["hint"]


def test_sample_frames_returns_cues_for_the_two_cue_rule(registered_source):
    source_id = registered_source["source_id"]
    result = engine.sample_frames(source_id, 20.0, 30.0, fps=0.5)
    assert result["success"] is True
    assert result["frames"] and Path(result["frames"][0]["path"]).is_file()

    cues = result["cues"]
    assert "[laughter]" in cues["transcript_markers"]  # the fixture has a marker at 22s
    assert "audio_spikes" in cues and "two_cue_agreement" in cues


# --------------------------------------------------------------------------
# End to end: plan -> render -> publish, asserting on the real encoded bytes
# --------------------------------------------------------------------------


def _probe(path: Path) -> dict:
    result = run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=60.0,
    )
    assert result.ok, result.stderr
    return json.loads(result.stdout)


@pytest.mark.timeout(600)
def test_end_to_end_render_and_publish(registered_source):
    """The real thing: candidates -> plan -> encode -> publish, checking the actual output."""
    source_id = registered_source["source_id"]

    added = engine.add_candidates(
        source_id,
        [
            # Spans the tone/silence cycle, so dead-air trimming has something to remove.
            {
                "start": 0.5,
                "end": 16.0,
                "label": "quote",
                "score": 9,
                "reason": "funding buys time",
            },
            {"start": 32.0, "end": 38.0, "label": "quote", "score": 7, "reason": "disagreement"},
        ],
    )
    assert added["success"] is True

    planned = engine.plan_clips(source_id, mode="auto")
    assert planned["success"] is True
    clip_id = planned["clips"][0]["clip_id"]

    queued = engine.render_clip(clip_id, reframe="center", captions=True)
    assert queued["success"] is True
    assert queued["status"] == "queued"
    job_id = queued["job_id"]

    job = wait_for(job_id, timeout=420)
    assert job["status"] == "done", f"render failed: {job.get('error')} / {job.get('hint')}"

    status = engine.get_job(job_id)
    assert status["success"] is True
    output = Path(status["output_path"])
    assert output.is_file()

    # The encoded bytes: vertical 1080x1920, with audio, and shorter than the raw span
    # because the dead air was trimmed out.
    probed = _probe(output)
    video = next(s for s in probed["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in probed["streams"]), "clip has no audio"

    duration = float(probed["format"]["duration"])
    assert 0 < duration < 15.5, f"expected the 15.5s span to be trimmed, got {duration}s"
    assert any("dead air" in s for s in status["steps"])

    published = engine.publish_outputs([job_id], ttl_hours=1)
    assert published["success"] is True

    clip = published["clips"][0]
    # No clip is ever returned without its source mapping — this is the output contract.
    assert clip["source_url"] == registered_source["url"]
    assert clip["source_start"] == pytest.approx(0.5, abs=0.6)
    assert clip["source_link"].endswith("&t=0s")
    assert clip["built_from"] and clip["kept_segments"]
    assert clip["reason"] == "funding buys time"

    # The export lives in the project library...
    export = Path(published["manifest_path"]).parent
    assert (export / "manifest.json").is_file()
    assert (export / "index.html").is_file()

    # ...the clip itself is durable in the library, under its own id...
    from _clip_library import clip_dir

    home = clip_dir(clip["clip_id"])
    assert (home / "clip.mp4").is_file()
    assert (home / "clip.jpg").is_file()  # thumbnail

    # ...and the served tree is a view onto it, which is what the links point at.
    from _clip_helpers import serve_dir

    served = serve_dir() / published["batch_id"]
    assert (served / f"{clip['clip_id']}.mp4").is_file()
    assert (served / "index.html").is_file()

    # The summary is the human-checkable artifact.
    summary = published["summary"]
    assert "source 0:00" in summary and clip["source_link"] in summary

    # Publishing is terminal for the source video: it is cut, so it is deleted.
    assert source_id in published["sources_deleted"]

    from _clip_library import load_source

    record = load_source(source_id)
    assert record["local_path"] == ""  # the video is gone
    assert Path(record["transcript_path"]).is_file()  # the transcript outlives it


def test_every_member_is_normalized_to_one_timebase_before_crossfade():
    """xfade rejects links whose timebases differ, and concat/fps produce different ones.

    A member built from several segments goes through `concat` (timebase 1/1000000); a
    single-segment member goes through `fps` (1/30). Mix the two shapes in one clip and
    ffmpeg dies with "First input link main timebase (1/30) do not match ... (1/1000000)".
    Every member must be forced to the same timebase before it reaches a crossfade.
    """
    from _clip_render import AUDIO_RATE, OUT_FPS, build_filtergraph

    timeline = Timeline(
        segments=[
            # member 0: one segment  -> the `fps` path
            Segment(src_start=0.0, src_end=6.0, out_start=0.0, member=0),
            # member 1: two segments -> the `concat` path (dead air was dropped inside it)
            Segment(src_start=20.0, src_end=23.0, out_start=5.85, member=1),
            Segment(src_start=25.0, src_end=28.0, out_start=8.85, member=1),
        ],
        duration=11.85,
        members=2,
    )
    graph = build_filtergraph(timeline, 854, 480, "center", [], None, "cap.ass")

    assert "concat=n=2" in graph  # the multi-segment member really does take the concat path
    for member in (0, 1):
        assert f"settb=1/{OUT_FPS},format=yuv420p,setsar=1[mv{member}]" in graph, (
            f"member {member} video reaches the crossfade without a pinned timebase"
        )
        assert f"asettb=1/{AUDIO_RATE}[ma{member}]" in graph, (
            f"member {member} audio reaches the crossfade without a pinned timebase"
        )
    assert "xfade" in graph and "acrossfade" in graph


def test_captions_point_at_a_dedicated_fonts_dir():
    """libass parses every file in fontsdir as a font — so it must not be the job temp dir."""
    from _clip_render import build_filtergraph

    timeline = Timeline(
        segments=[Segment(src_start=0.0, src_end=6.0, out_start=0.0, member=0)], duration=6.0
    )
    graph = build_filtergraph(timeline, 854, 480, "center", [], None, "cap.ass")
    assert "fontsdir=fonts" in graph
    assert "fontsdir=." not in graph  # would make libass try to load cap.ass as a font


@pytest.mark.timeout(900)
def test_mixed_shape_members_actually_encode(registered_source):
    """The regression, end to end: a trimmed member crossfaded with an untrimmed one."""
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id,
        [
            # 0.5-6.0 is pure tone: no internal silence -> single segment.
            {"start": 0.5, "end": 6.0, "label": "quote", "score": 9, "reason": "single-segment"},
            # 12.0-26.0 spans the tone/silence cycle -> gets split into several segments.
            {"start": 12.0, "end": 26.0, "label": "quote", "score": 8, "reason": "multi-segment"},
        ],
    )
    clip = engine.plan_clips(source_id, mode="by_label")["clips"][0]
    assert clip["members"] == 2

    job_id = engine.render_clip(clip["clip_id"], reframe="center", captions=True)["job_id"]
    job = wait_for(job_id, timeout=600)
    assert job["status"] == "done", f"mixed-shape crossfade failed: {job.get('error')}"

    probed = _probe(Path(job["output_path"]))
    assert any(s["codec_type"] == "audio" for s in probed["streams"])
    assert float(probed["format"]["duration"]) > 5.0


@pytest.mark.timeout(600)
def test_no_face_shows_the_whole_frame_instead_of_cropping_blind(registered_source):
    """No face on screen must fall back to the WHOLE FRAME, never to a centred crop.

    This used to assert the opposite, and the first live batch showed what that costs. A
    9:16 crop keeps a third of the width; where the detector finds nothing, that third is
    a guess. On the TED talk the guess framed a giant red letter "D" and the backs of the
    audience's heads while the speaker stood outside the frame — and on a chart slide it
    kept the middle third of a full-width graph, cutting the title to "o / e / ren".

    A frame with no face in it is precisely the frame you must not crop into.
    """
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id, [{"start": 0.5, "end": 8.0, "label": "quote", "score": 9, "reason": "x"}]
    )
    clip_id = engine.plan_clips(source_id, mode="auto")["clips"][0]["clip_id"]

    job_id = engine.render_clip(clip_id, reframe="speaker", captions=False)["job_id"]
    job = wait_for(job_id, timeout=420)
    assert job["status"] == "done", f"speaker reframe failed outright: {job.get('error')}"

    # The synthetic source has no face in it, so the fallback must have engaged loudly.
    assert any("whole frame" in s for s in job["progress"]), job["progress"]
    assert not any("centred crop" in s for s in job["progress"]), (
        "a centred crop on a faceless frame is the bug, not the fallback"
    )

    probed = _probe(Path(job["output_path"]))
    video = next(s for s in probed["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)  # still a valid vertical clip


@pytest.mark.timeout(600)
def test_multi_member_clip_crossfades_and_captions(registered_source):
    """A two-span clip must crossfade cleanly and keep its captions in sync."""
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id,
        [
            {"start": 0.5, "end": 6.0, "label": "quote", "score": 9, "reason": "one"},
            {"start": 32.0, "end": 37.5, "label": "quote", "score": 8, "reason": "two"},
        ],
    )
    planned = engine.plan_clips(source_id, mode="by_label")
    clip = planned["clips"][0]
    assert clip["members"] == 2

    job_id = engine.render_clip(clip["clip_id"], reframe="center", captions=True)["job_id"]
    job = wait_for(job_id, timeout=420)
    assert job["status"] == "done", f"render failed: {job.get('error')}"

    output = Path(job["output_path"])
    probed = _probe(output)
    duration = float(probed["format"]["duration"])
    # Two ~5.5s spans, joined by a 0.15s crossfade.
    assert 9.0 < duration < 12.0, f"unexpected assembled duration: {duration}s"

    ass = Path(job["temp_dir"]) / "cap.ass"
    assert ass.is_file()
    body = ass.read_text(encoding="utf-8")
    assert "Liberation Sans" in body  # the bundled font, never a system lookup
    assert body.count("Dialogue:") > 5
