"""Progress-array helpers (STANDARDS: never hand-build progress entries).

All user-facing step output travels in a tool response's ``progress`` list. These
helpers are the only sanctioned way to append to it, so the shape stays uniform
across every tool. Nothing here touches stdout.
"""

from __future__ import annotations

__all__ = ["ok", "fail", "info", "warn", "undo", "step"]


def _entry(marker: str, message: str) -> str:
    return f"{marker} {message}"


def ok(progress: list[str], message: str) -> list[str]:
    """Record a completed step."""
    progress.append(_entry("[ok]", message))
    return progress


def fail(progress: list[str], message: str) -> list[str]:
    """Record a failed step."""
    progress.append(_entry("[fail]", message))
    return progress


def info(progress: list[str], message: str) -> list[str]:
    """Record a neutral observation."""
    progress.append(_entry("[info]", message))
    return progress


def warn(progress: list[str], message: str) -> list[str]:
    """Record a non-fatal problem the caller should know about."""
    progress.append(_entry("[warn]", message))
    return progress


def undo(progress: list[str], message: str) -> list[str]:
    """Record a rollback / cleanup action."""
    progress.append(_entry("[undo]", message))
    return progress


def step(progress: list[str], message: str) -> list[str]:
    """Record an in-flight step (used by the render worker's job progress)."""
    progress.append(_entry("[step]", message))
    return progress
