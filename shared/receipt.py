"""Append-only receipt log (STANDARDS §25).

Every render and publish appends one line describing what happened. The log is
JSONL rather than a JSON array so an append is a single ``open(mode="a")`` write
and a crash mid-append can never corrupt earlier entries.

``append_receipt`` never raises. A failure to log must not fail a render.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["append_receipt", "read_receipts"]


def append_receipt(
    receipt_path: str | os.PathLike[str],
    tool: str,
    args: dict[str, Any],
    result: str,
    backup: str = "",
) -> None:
    """Append one receipt entry. Never raises; drops silently on failure."""
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "tool": tool,
            "args": args,
            "result": result,
            "backup": backup,
        }
        path = Path(receipt_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        log.warning("receipt append failed (ignored): %s", exc)


def read_receipts(receipt_path: str | os.PathLike[str], limit: int = 50) -> list[dict[str, Any]]:
    """Read the last ``limit`` receipts. Returns [] if absent or unreadable."""
    try:
        path = Path(receipt_path)
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except OSError:
        return []
