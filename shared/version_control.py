"""Atomic writes and snapshots for persistent state.

Clipper's source video is disposable (CLAUDE.md §4, divergence 4) — there is no
source mutation to roll back, so the snapshot-before-write protocol applies only
to the durable state this server *does* own: manifests and parsed transcripts.

Every write goes through ``atomic_write_*``: temp file in the same directory,
fsync, then ``os.replace`` onto the final path. A half-written manifest can never
be observed, even if the box loses power mid-write.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.file_utils import resolve_path

__all__ = ["atomic_write_bytes", "atomic_write_text", "atomic_write_json", "snapshot"]

SNAPSHOT_DIR = ".mcp_versions"


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> Path:
    """Write bytes atomically: temp file in the same dir, fsync, then rename."""
    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return target


def atomic_write_text(path: str | os.PathLike[str], text: str) -> Path:
    """Write UTF-8 text atomically."""
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | os.PathLike[str], payload: Any, *, indent: int = 2) -> Path:
    """Serialize to JSON and write atomically."""
    return atomic_write_text(path, json.dumps(payload, indent=indent, ensure_ascii=False))


def snapshot(path: str | os.PathLike[str]) -> str:
    """Copy a file into a sibling ``.mcp_versions/`` before it is overwritten.

    Returns the snapshot path, or "" if the target does not exist yet (a first
    write has nothing to roll back to). Never raises.
    """
    try:
        target = resolve_path(path)
        if not target.is_file():
            return ""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        vault = target.parent / SNAPSHOT_DIR
        vault.mkdir(parents=True, exist_ok=True)
        backup = vault / f"{target.stem}_{stamp}{target.suffix}.bak"
        shutil.copy2(target, backup)
        return str(backup)
    except (OSError, ValueError):
        return ""
