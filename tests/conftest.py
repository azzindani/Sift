"""Test fixtures: an isolated data dir per test, and a synthetic source video.

The synthetic video is built once per session by ffmpeg rather than committed as a
binary blob. Its audio is a tone that cycles 6s on / 4s off, so ``silencedetect``
has real dead air to find — the trim path is exercised for real, not mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"
VIDEO_DURATION = 60.0
SPEECH_S = 6.0  # tone on
SILENCE_S = 4.0  # tone off -> silencedetect finds this


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point every config path at a throwaway dir, and reset cached DB schema state."""
    import _clip_helpers

    monkeypatch.setenv("SIFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SIFT_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SIFT_SERVE_DIR", str(tmp_path / "served"))
    monkeypatch.setenv("SIFT_BASE_URL", "https://clips.example.test")
    monkeypatch.delenv("MCP_CONSTRAINED_MODE", raising=False)
    for var in ("SIFT_TOKENS_FILE", "SIFT_TOKENS", "SIFT_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    _clip_helpers._initialized.clear()
    _clip_helpers.init_state()

    from shared.auth import reset_for_tests

    reset_for_tests()
    yield tmp_path

    from _clip_queue import stop_worker

    stop_worker(timeout=2.0)


@pytest.fixture
def json3_text() -> str:
    return (FIXTURES / "sample.json3").read_text(encoding="utf-8")


@pytest.fixture
def vtt_text() -> str:
    return (FIXTURES / "sample.vtt").read_text(encoding="utf-8")


@pytest.fixture
def rolling_vtt_text() -> str:
    return (FIXTURES / "rolling.vtt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def source_video(tmp_path_factory) -> Path:
    """A 60s 1280x720 clip whose audio alternates 6s of tone with 4s of silence."""
    from shared.platform_utils import ffmpeg_bin, run

    out = tmp_path_factory.mktemp("media") / "source.mp4"
    tone = (
        f"aevalsrc='0.35*sin(440*2*PI*t)*between(mod(t\\,{SPEECH_S + SILENCE_S})\\,0\\,{SPEECH_S})'"
        f":s=44100:d={VIDEO_DURATION}"
    )
    result = run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1280x720:rate=30:duration={VIDEO_DURATION}",
            "-f",
            "lavfi",
            "-i",
            tone,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        timeout=180.0,
    )
    if not result.ok or not out.is_file():
        pytest.skip(f"ffmpeg could not build the test video: {result.stderr[-300:]}")
    return out


@pytest.fixture
def registered_source(source_video, json3_text):
    """Insert a source row + parsed transcript, as if fetch_source had run (no network).

    The video is *copied* into the per-test data dir: publish_outputs deletes the source
    it is given, and the session-scoped original has to survive for the next test.
    """
    import shutil

    from _clip_helpers import now_ts
    from _clip_library import ensure_project, new_source_id, project_dir, save_source
    from _clip_transcript import parse_json3
    from shared.file_utils import safe_mkdir
    from shared.version_control import atomic_write_json

    project = "test-project"
    ensure_project(project)
    source_id = new_source_id()
    work = safe_mkdir(project_dir(project) / "sources" / source_id)
    local_video = work / "source.mp4"
    shutil.copy2(source_video, local_video)

    parsed = parse_json3(json3_text)
    parsed["duration"] = VIDEO_DURATION
    transcript_path = work / "transcript.json"
    atomic_write_json(transcript_path, parsed)

    record = {
        "_protocol": "sift/source/v1",
        "source_id": source_id,
        "project": project,
        "url": "https://www.youtube.com/watch?v=TESTVIDEO",
        "title": "A Test Podcast",
        "duration": VIDEO_DURATION,
        "transcript_kind": "json3",
        "transcript_path": str(transcript_path),
        "local_path": str(local_video),
        "max_height": 720,
        "width": 1280,
        "height": 720,
        "frames_sampled": 0,
        "fetched_at": now_ts(),
    }
    save_source(record)
    return record
