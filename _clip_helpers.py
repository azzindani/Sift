"""Shared constants, config, the response contract, and the SQLite schema.

Everything downstream imports from here. Three things live in this module and
nowhere else:

1. **Config.** Read from the environment *at call time*, never at import time, so
   tests can monkeypatch and a post-startup change is honoured.
2. **The response contract.** ``ok_result`` / ``error_result`` are the only two
   ways a tool result is constructed, which is what guarantees every tool returns
   a dict with ``success`` first, a ``progress`` array, and a ``token_estimate``.
3. **State.** The SQLite schema and connection factory. SQLite is the single
   source of runtime truth — there is no source file to snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.file_utils import register_root, safe_mkdir

log = logging.getLogger("clipper")

REPO_ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Config — always read at call time (STANDARDS: never at import time)
# --------------------------------------------------------------------------


def constrained() -> bool:
    """True when running under VPS resource limits (2 vCPU / 4 GB)."""
    return os.environ.get("MCP_CONSTRAINED_MODE", "").strip() in {"1", "true", "yes", "on"}


def data_dir() -> Path:
    """Root for runtime state: the job DB and working temp dirs. Not the library."""
    configured = os.environ.get("SIFT_DATA_DIR", "").strip()
    return safe_mkdir(Path(configured) if configured else REPO_ROOT / ".sift")


def projects_dir() -> Path:
    """The library root — ``~/.sift/projects`` by default, ``/home/sift/projects`` in Docker.

    This is the durable half of the system: YAML records and rendered clips. Everything
    under ``data_dir()`` is rebuildable; everything under here is not.
    """
    configured = os.environ.get("SIFT_PROJECTS_DIR", "").strip()
    return safe_mkdir(Path(configured) if configured else Path.home() / ".sift" / "projects")


def db_path() -> Path:
    """The job queue + the rebuildable index. Delete it and it repopulates from the files."""
    return data_dir() / "sift.db"


def tmp_dir() -> Path:
    """Working scratch. Job temp dirs live here; all of it is disposable."""
    return safe_mkdir(data_dir() / "tmp")


def jobs_dir() -> Path:
    return safe_mkdir(tmp_dir() / "jobs")


def serve_dir() -> Path:
    """Published output, served over HTTP. A *view* onto the library, not the record."""
    configured = os.environ.get("SIFT_SERVE_DIR", "").strip()
    return safe_mkdir(Path(configured) if configured else data_dir() / "served")


def receipt_path() -> Path:
    return data_dir() / "sift.mcp_receipt.jsonl"


def base_url() -> str:
    """Public base URL for published links. Trailing slash stripped."""
    raw = os.environ.get("SIFT_BASE_URL", "").strip() or "http://localhost:8765/clips"
    return raw.rstrip("/")


def ttl_hours_default() -> int:
    try:
        return max(1, int(os.environ.get("SIFT_TTL_HOURS", "168")))
    except ValueError:
        return 168


def cookies_path_default() -> str:
    return os.environ.get("SIFT_COOKIES_PATH", "").strip()


def proxy_default() -> str:
    return os.environ.get("SIFT_PROXY", "").strip()


def assets_font_dir() -> Path:
    """Bundled caption fonts. Never resolved from the system (CLAUDE.md §6.5)."""
    return REPO_ROOT / "assets" / "fonts"


def skills_dir() -> Path:
    return REPO_ROOT / "skills"


# --------------------------------------------------------------------------
# Budget guards — every limit tightens under MCP_CONSTRAINED_MODE
# --------------------------------------------------------------------------

MAX_OPS_PER_CALL = 50  # op-array ceiling (STANDARDS §13)
MIN_CANDIDATE_S = 3.0
MAX_CANDIDATE_S = 180.0
MIN_CLIP_S = 5.0
FETCH_SOURCE_TTL_HOURS = 24  # a source video is swept this long after fetch


def get_window_s() -> float:
    """Transcript read window (seconds)."""
    return 300.0 if constrained() else 600.0


def get_overlap_s() -> float:
    """Look-back overlap so a moment straddling a boundary is never missed."""
    return 120.0


def get_max_segments() -> int:
    """Cap on transcript segments returned by one read."""
    return 120 if constrained() else 240


def get_max_height() -> int:
    """Source download resolution cap."""
    return 480 if constrained() else 720


def get_frame_budget() -> int:
    """Hard per-source ceiling on frames handed to the agent's vision pass."""
    return 24 if constrained() else 60


def get_max_frames_per_call() -> int:
    return 8 if constrained() else 20


def get_max_clip_s() -> float:
    """Longest publishable clip. Short-form ceiling."""
    return 60.0


def get_encode_threads() -> int:
    return 2


def get_max_candidates_returned() -> int:
    return 30 if constrained() else 100


# --------------------------------------------------------------------------
# Label registry — a label is the routing key that pulls a skill's assembly params
# --------------------------------------------------------------------------

LABEL_REGISTRY: dict[str, dict[str, Any]] = {
    "quote": {
        "reframe": "speaker",
        "trim_aggressiveness": "tight",
        "caption_style": "key_phrase",
        "silence_threshold_db": -30,
        "pad_ms": 150,
        "max_duration_s": 60,
    },
    "joke": {
        "reframe": "speaker",
        "trim_aggressiveness": "very_tight",
        "caption_style": "punchline",
        "silence_threshold_db": -30,
        "pad_ms": 120,
        "max_duration_s": 45,
    },
    "story": {
        "reframe": "speaker",
        "trim_aggressiveness": "gentle",
        "caption_style": "minimal",
        "silence_threshold_db": -32,
        "pad_ms": 200,
        "max_duration_s": 60,
    },
    "argument": {
        "reframe": "stacked",
        "trim_aggressiveness": "medium",
        "caption_style": "dual_speaker",
        "silence_threshold_db": -30,
        "pad_ms": 150,
        "max_duration_s": 60,
    },
    "hot_take": {
        "reframe": "speaker",
        "trim_aggressiveness": "tight",
        "caption_style": "key_phrase",
        "silence_threshold_db": -30,
        "pad_ms": 150,
        "max_duration_s": 50,
    },
    "reaction": {
        "reframe": "speaker",
        "trim_aggressiveness": "tight",
        "caption_style": "sparse",
        "silence_threshold_db": -28,
        "pad_ms": 250,
        "max_duration_s": 30,
    },
}

LABELS = tuple(LABEL_REGISTRY)
REFRAME_MODES = ("speaker", "center", "stacked")
PLAN_MODES = ("auto", "by_label", "by_topic", "montage", "supercut")

# How much silence to drop, per label's trim aggressiveness (seconds).
TRIM_THRESHOLDS = {
    "very_tight": 0.35,
    "tight": 0.50,
    "medium": 0.70,
    "gentle": 1.20,
}


def assembly_params(label: str) -> dict[str, Any]:
    """Deterministic assembly params for a label. Falls back to `quote`."""
    return dict(LABEL_REGISTRY.get(label, LABEL_REGISTRY["quote"]))


# --------------------------------------------------------------------------
# The response contract — the only two ways a tool result is built
# --------------------------------------------------------------------------


def token_estimate(payload: Any) -> int:
    """Response size in tokens ≈ serialized bytes ÷ 4."""
    try:
        return max(1, len(json.dumps(payload, default=str)) // 4)
    except (TypeError, ValueError):
        return 1


def ok_result(op: str, progress: list[str] | None = None, **fields: Any) -> dict[str, Any]:
    """Build a success dict: ``success`` first, ``progress`` and ``token_estimate`` always."""
    result: dict[str, Any] = {"success": True, "op": op}
    result.update(fields)
    result["progress"] = progress or []
    result["token_estimate"] = 0
    result["token_estimate"] = token_estimate(result)
    return result


def error_result(
    error: str,
    hint: str,
    progress: list[str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a failure dict. No exception ever reaches the caller (STANDARDS §17).

    ``error`` states the fact plus the observed value; ``hint`` completes the
    sentence "to fix this, ..." and names a concrete tool or value.
    """
    result: dict[str, Any] = {"success": False, "error": error, "hint": hint}
    result.update(fields)
    result["progress"] = progress or []
    result["token_estimate"] = 0
    result["token_estimate"] = token_estimate(result)
    return result


def now_iso() -> str:
    """Current UTC time, ISO-8601 with a Z suffix."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_ts() -> float:
    return datetime.now(UTC).timestamp()


def new_id(prefix: str) -> str:
    """Unguessable id: ``<prefix>_<8 hex>``. Used for served paths too."""
    return f"{prefix}_{secrets.token_hex(4)}"


def hhmmss(seconds: float) -> str:
    """Format seconds as H:MM:SS (or M:SS under an hour) for human-readable summaries."""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str, fallback: str = "file") -> str:
    """Strip a string down to a filesystem-safe stem."""
    cleaned = _SAFE_NAME.sub("_", text).strip("._-")
    return cleaned[:80] or fallback


# --------------------------------------------------------------------------
# State — SQLite holds the job queue and a *rebuildable index*, nothing else
# --------------------------------------------------------------------------
#
# The library (YAML under projects_dir()) is the record. SQLite keeps only the two
# things files are genuinely bad at:
#
#   jobs           — atomic claim, one-encode-at-a-time, restart reconciliation.
#                    A worker thread racing YAML files would corrupt them.
#   library_index  — entity_id -> project, so a tool that is handed a bare
#                    source_id knows where to look. Derived, never authoritative:
#                    delete sift.db and it repopulates from the files on startup.

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'render',
    clip_id     TEXT NOT NULL DEFAULT '',
    source_id   TEXT NOT NULL DEFAULT '',
    project     TEXT NOT NULL DEFAULT 'default',
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    TEXT NOT NULL DEFAULT '[]',
    temp_dir    TEXT NOT NULL DEFAULT '',
    output_path TEXT NOT NULL DEFAULT '',
    thumb_path  TEXT NOT NULL DEFAULT '',
    duration    REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    hint        TEXT NOT NULL DEFAULT '',
    options     TEXT NOT NULL DEFAULT '{}',
    segments    TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS library_index (
    entity_id TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    project   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_index_project ON library_index(project);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    if path in _initialized:
        return
    with _init_lock:
        if path in _initialized:
            return
        conn.executescript(SCHEMA)
        conn.commit()
        _initialized.add(path)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with WAL + row access by name, and commit on exit.

    A fresh connection per operation: the render worker runs on its own thread and
    SQLite connections are not safe to share across threads. WAL lets the worker
    write while the router reads.
    """
    path = str(db_path())
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(conn, path)
        yield conn
    finally:
        conn.close()


def init_state() -> None:
    """Create the roots, register them as allowed paths, open the DB. Idempotent."""
    register_root(data_dir())
    register_root(serve_dir())
    register_root(projects_dir())
    tmp_dir()
    jobs_dir()
    with connect():
        pass


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def load_json(raw: str, fallback: Any) -> Any:
    """Parse a JSON column, falling back rather than raising on corruption."""
    try:
        return json.loads(raw) if raw else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback
