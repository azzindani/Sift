"""Tests for the project library and the HTTP token auth.

The library's central claim is that **the files are the record**: delete the database,
hand-edit a YAML file, drop a project directory in from a backup — the server picks it
up. These tests hold that claim to account rather than taking it on faith.
"""

from __future__ import annotations

import json

import pytest

import engine
from _clip_helpers import connect
from _clip_library import (
    LibraryError,
    ensure_project,
    list_projects,
    load_candidates,
    load_clip,
    project_dir,
    read_yaml,
    rebuild_index,
    validate_project,
)
from shared.auth import (
    OPEN_PRINCIPAL,
    RateLimiter,
    authorize,
    describe_auth,
    load_tokens,
    reset_for_tests,
)

# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


def test_project_scaffolds_the_folio_layout():
    ensure_project("ep42")
    root = project_dir("ep42")
    for sub in ("sources", "candidates", "clips", "exports"):
        assert (root / sub).is_dir()

    record = read_yaml(root / "project.yaml")
    assert record["_protocol"] == "sift/project/v1"
    assert record["name"] == "ep42"


def test_project_name_cannot_escape_the_library_root():
    """A project name is a path component. Traversal here would write anywhere on disk."""
    for evil in ("../../etc", "/etc/passwd", "..", "a/b"):
        with pytest.raises(LibraryError):
            validate_project(evil)


def test_list_library_reports_projects_and_contents(registered_source):
    listing = engine.list_library()
    assert listing["success"] is True
    names = {p["project"] for p in listing["projects"]}
    assert "test-project" in names

    detail = engine.list_library("test-project")
    assert detail["success"] is True
    assert len(detail["sources"]) == 1
    assert detail["sources"][0]["source_id"] == registered_source["source_id"]
    assert detail["sources"][0]["video_on_disk"] is True


# --------------------------------------------------------------------------
# Files are the record
# --------------------------------------------------------------------------


def test_candidates_land_in_hand_editable_yaml(registered_source):
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id,
        [{"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "the thesis"}],
    )

    path = project_dir("test-project") / "candidates" / f"{source_id}.yaml"
    assert path.is_file()

    record = read_yaml(path)
    assert record["_protocol"] == "sift/candidates/v1"
    assert record["candidates"][0]["reason"] == "the thesis"
    assert record["candidates"][0]["label"] == "quote"


def test_a_hand_edit_to_the_yaml_is_what_the_next_plan_reads(registered_source):
    """The whole point of a file-backed record: you can fix a boundary in an editor."""
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id,
        [{"start": 5.0, "end": 12.0, "label": "quote", "score": 9, "reason": "too tight"}],
    )

    # Widen the span by hand, exactly as a human reviewing the pick would.
    path = project_dir("test-project") / "candidates" / f"{source_id}.yaml"
    record = read_yaml(path)
    record["candidates"][0]["start"] = 0.5
    record["candidates"][0]["reason"] = "widened by hand"
    path.write_text(__import__("yaml").safe_dump(record, sort_keys=False), encoding="utf-8")

    stored = load_candidates(source_id)
    assert stored[0]["start"] == 0.5

    planned = engine.plan_clips(source_id, mode="auto")
    assert planned["success"] is True
    assert planned["clips"][0]["source_start"] == 0.5  # the edit reached the plan
    assert planned["clips"][0]["reason"] == "widened by hand"


def test_clip_definition_is_yaml_with_its_members(registered_source):
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id,
        [
            {"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "a"},
            {"start": 32.0, "end": 38.0, "label": "quote", "score": 8, "reason": "b"},
        ],
    )
    clip_id = engine.plan_clips(source_id, mode="by_label")["clips"][0]["clip_id"]

    path = project_dir("test-project") / "clips" / clip_id / "clip.yaml"
    assert path.is_file()
    record = read_yaml(path)
    assert record["_protocol"] == "sift/clip/v1"
    assert len(record["members"]) == 2
    assert record["spec"]["reframe"] == "speaker"

    # Loading resolves the member candidate ids back into full records.
    loaded = load_clip(clip_id)
    assert len(loaded["member_rows"]) == 2


def test_deleting_the_database_loses_nothing(registered_source):
    """The DB is a rebuildable index. The library is the record — prove it."""
    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id, [{"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "x"}]
    )

    with connect() as conn:
        conn.execute("DELETE FROM library_index")  # simulate a lost / fresh database

    # A cold index still finds the source, because the files still know where it is.
    assert rebuild_index() >= 1
    assert engine.read_transcript_chunk(source_id, 0)["success"] is True
    assert load_candidates(source_id)[0]["reason"] == "x"


def test_projects_are_isolated_from_each_other():
    ensure_project("alpha")
    ensure_project("beta")
    names = {p["project"] for p in list_projects()}
    assert {"alpha", "beta"} <= names
    assert (project_dir("alpha") / "sources").is_dir()
    assert not list((project_dir("beta") / "sources").glob("*"))


# --------------------------------------------------------------------------
# Token auth — the same contract as Folio
# --------------------------------------------------------------------------


def test_no_config_means_open_and_says_so(monkeypatch):
    reset_for_tests()
    assert load_tokens().mode == "open"
    assert authorize(None) == OPEN_PRINCIPAL
    assert "UNAUTHENTICATED" in describe_auth()


def test_single_shared_key(monkeypatch):
    monkeypatch.setenv("SIFT_API_KEY", "sk-single")
    reset_for_tests()

    assert authorize("Bearer sk-single") == "default"
    assert authorize("Bearer wrong") is None
    assert authorize(None) is None
    assert authorize("sk-single") is None  # bare token, no "Bearer " prefix


def test_inline_named_tokens(monkeypatch):
    monkeypatch.setenv("SIFT_TOKENS", "claude:sk-aaa,hermes:sk-bbb")
    reset_for_tests()

    assert authorize("Bearer sk-aaa") == "claude"
    assert authorize("Bearer sk-bbb") == "hermes"
    assert authorize("Bearer sk-ccc") is None
    assert "claude" in describe_auth() and "hermes" in describe_auth()


def test_tokens_file_wins_over_the_other_modes(monkeypatch, tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"claude-code": "sk-file"}), encoding="utf-8")

    monkeypatch.setenv("SIFT_TOKENS_FILE", str(path))
    monkeypatch.setenv("SIFT_TOKENS", "other:sk-inline")
    monkeypatch.setenv("SIFT_API_KEY", "sk-single")
    reset_for_tests()

    assert authorize("Bearer sk-file") == "claude-code"
    assert authorize("Bearer sk-inline") is None  # the file has priority, exclusively
    assert authorize("Bearer sk-single") is None


def test_a_broken_tokens_file_falls_through_rather_than_locking_you_out(monkeypatch, tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{ not json", encoding="utf-8")

    monkeypatch.setenv("SIFT_TOKENS_FILE", str(path))
    monkeypatch.setenv("SIFT_API_KEY", "sk-fallback")
    reset_for_tests()

    assert authorize("Bearer sk-fallback") == "default"


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_rate_limiter_allows_the_burst_then_throttles():
    limiter = RateLimiter(burst=3, per_sec=1000000)  # refill so fast it never blocks
    assert all(limiter.allow("claude", "1.2.3.4") for _ in range(3))

    slow = RateLimiter(burst=3, per_sec=0.0001)  # effectively no refill
    assert [slow.allow("claude", "1.2.3.4") for _ in range(5)] == [True, True, True, False, False]


def test_rate_limiter_keys_on_token_and_ip_together():
    """One leaked token used from many hosts must not multiply its allowance."""
    limiter = RateLimiter(burst=1, per_sec=0.0001)
    assert limiter.allow("claude", "1.1.1.1") is True
    assert limiter.allow("claude", "1.1.1.1") is False  # same pair: throttled
    assert limiter.allow("claude", "2.2.2.2") is True  # different IP: own bucket
    assert limiter.allow("hermes", "1.1.1.1") is True  # different token: own bucket


def test_rate_limiter_disabled_when_zero():
    assert RateLimiter(burst=0, per_sec=10).enabled is False
    assert RateLimiter(burst=40, per_sec=0).enabled is False
    disabled = RateLimiter(burst=0, per_sec=0)
    assert all(disabled.allow("x", "y") for _ in range(1000))


# --------------------------------------------------------------------------
# Async fetch — the reason fetch_source is split in two
# --------------------------------------------------------------------------


def test_fetch_returns_before_the_video_lands_and_render_queues_behind_it(registered_source):
    """A 3-hour download takes minutes; an MCP client times out at ~30s.

    So `fetch_source` returns as soon as the transcript is readable and queues the video
    on the same worker that does the encoding. A `render_clip` issued while the download
    is still in flight must *queue*, not fail — the single worker drains in order, so the
    video is on disk by the time the render runs.
    """
    from _clip_library import update_source
    from _clip_queue import enqueue_fetch, pending_fetch, stop_worker

    stop_worker(timeout=2.0)  # freeze the queue so we can inspect it mid-flight

    source_id = registered_source["source_id"]
    engine.add_candidates(
        source_id, [{"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "x"}]
    )
    clip_id = engine.plan_clips(source_id, mode="auto")["clips"][0]["clip_id"]

    # Simulate the state right after fetch_source: transcript on disk, video not yet.
    update_source(source_id, local_path="")
    job = enqueue_fetch(source_id, "test-project")
    assert pending_fetch(source_id) == job

    queued = engine.render_clip(clip_id, reframe="center")
    assert queued["success"] is True, queued.get("error")
    assert queued["status"] == "queued"

    # And with no download pending and no video, it fails with an actionable hint.
    from _clip_helpers import connect

    with connect() as conn:
        conn.execute("UPDATE jobs SET status='failed' WHERE kind='fetch'")

    refused = engine.render_clip(clip_id, reframe="center")
    assert refused["success"] is False
    assert "not on disk" in refused["error"]
    assert "fetch_source" in refused["hint"]


def test_ytdlp_is_pinned_to_ipv4_by_default(monkeypatch):
    """A Docker bridge has no IPv6 route, but DNS still returns AAAA records.

    yt-dlp then picks IPv6 at random and dies with "Network is unreachable" — it works,
    then it doesn't, on the same URL. Pinning to IPv4 makes the failure deterministic
    (i.e. absent). This bit us in the container and cost real time to find.
    """
    from _clip_fetch import _ytdlp_args, force_ipv4

    monkeypatch.delenv("SIFT_FORCE_IPV4", raising=False)
    assert force_ipv4() is True
    assert "--force-ipv4" in _ytdlp_args()

    monkeypatch.setenv("SIFT_FORCE_IPV4", "0")
    assert force_ipv4() is False
    assert "--force-ipv4" not in _ytdlp_args()


def test_an_empty_caption_list_is_re_probed_before_we_believe_it(monkeypatch):
    """A transient caption-endpoint failure must not become "this source has no captions".

    yt-dlp fetches the caption list from a separate endpoint. When that hiccups it returns
    good metadata with an empty `subtitles` dict and no error. Believing it first time sends
    the agent to find a different source — a dead end, not a retry. So we ask again.
    """
    import _clip_fetch

    calls: list[int] = []

    def flaky_probe(url: str, cookies_path: str = "") -> dict:
        calls.append(1)
        if len(calls) < 3:
            return {"title": "Talk", "duration": 100.0, "subtitles": {}}  # the hiccup
        return {"title": "Talk", "duration": 100.0, "subtitles": {"en": [{}]}}

    monkeypatch.setattr(_clip_fetch, "probe", flaky_probe)
    monkeypatch.setattr(_clip_fetch.time, "sleep", lambda _s: None)

    info, langs = _clip_fetch.probe_with_captions("https://example.com/v")
    assert langs == {"en": "manual"}
    assert len(calls) == 3  # it kept asking rather than accepting the first empty answer


def test_a_source_with_genuinely_no_captions_still_fails_fast(monkeypatch):
    """The retry must not turn a real "no captions" into an infinite hopeful loop."""
    import _clip_fetch

    calls: list[int] = []

    def no_captions(url: str, cookies_path: str = "") -> dict:
        calls.append(1)
        return {"title": "Silent film", "duration": 100.0, "subtitles": {}}

    monkeypatch.setattr(_clip_fetch, "probe", no_captions)
    monkeypatch.setattr(_clip_fetch.time, "sleep", lambda _s: None)

    _info, langs = _clip_fetch.probe_with_captions("https://example.com/v", attempts=3)
    assert langs == {}
    assert len(calls) == 3  # bounded


def test_a_render_blocked_by_a_failed_download_says_why(registered_source):
    """ "Source video vanished mid-queue: ." is true and useless. Report the real cause."""
    from _clip_helpers import connect
    from _clip_library import update_source
    from _clip_queue import stop_worker, wait_for

    stop_worker(timeout=2.0)
    source_id = registered_source["source_id"]

    engine.add_candidates(
        source_id, [{"start": 0.5, "end": 10.0, "label": "quote", "score": 9, "reason": "x"}]
    )
    clip_id = engine.plan_clips(source_id, mode="auto")["clips"][0]["clip_id"]

    update_source(source_id, local_path="")  # the download never produced a file
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, kind, source_id, status, error, created_at, finished_at)
               VALUES ('j_dl', 'fetch', ?, 'failed', 'Download failed: the host served an empty stream', 1, 2)""",
            (source_id,),
        )
        conn.execute(
            """INSERT INTO jobs (job_id, kind, clip_id, source_id, status, options, created_at)
               VALUES ('j_r', 'render', ?, ?, 'queued', '{}', 3)""",
            (clip_id, source_id),
        )

    from _clip_queue import ensure_worker

    ensure_worker()
    job = wait_for("j_r", timeout=60)

    assert job["status"] == "failed"
    assert "never downloaded" in job["error"]
    assert "empty stream" in job["error"]  # the *actual* upstream reason, surfaced
    assert "fetch_source" in job["hint"]
