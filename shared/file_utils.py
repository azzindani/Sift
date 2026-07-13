"""Path resolution and safe filesystem primitives.

Every path that reaches a subprocess, an open(), or an unlink() passes through
``resolve_path()`` exactly once. It resolves symlinks, then confirms the result
lies inside an allowed root — so a caller-supplied ``../../etc/passwd`` or a
symlink pointing out of the tree is rejected before anything touches it.

Allowed roots are the user's home tree plus any root explicitly registered by
the engine at import time (the data dir, the served dir, the system temp dir).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

__all__ = [
    "PathError",
    "register_root",
    "allowed_roots",
    "resolve_path",
    "safe_mkdir",
    "sweep_dir",
    "free_bytes",
    "dir_size",
]

# Roots registered by the engine (data dir, served dir). Home + system temp are
# always allowed. Kept as resolved Paths so containment checks are symlink-safe.
_EXTRA_ROOTS: list[Path] = []


class PathError(ValueError):
    """A path was rejected. Carries an actionable ``hint`` for the error dict."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def register_root(path: str | os.PathLike[str]) -> Path:
    """Register an additional allowed root (idempotent). Returns the resolved root."""
    root = Path(path).expanduser().resolve()
    if root not in _EXTRA_ROOTS:
        _EXTRA_ROOTS.append(root)
    return root


def allowed_roots() -> list[Path]:
    """Every root a resolved path is permitted to live under."""
    roots = [Path.home().resolve(), Path(tempfile.gettempdir()).resolve()]
    roots.extend(_EXTRA_ROOTS)
    return roots


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_path(
    path: str | os.PathLike[str],
    *,
    allowed_exts: tuple[str, ...] = (),
    must_exist: bool = False,
) -> Path:
    """Resolve to an absolute path inside an allowed root, or raise PathError.

    ``allowed_exts`` (lowercase, dot-prefixed) restricts the suffix when given.
    Resolution happens before the containment check, so symlink escapes fail.
    """
    if path is None or str(path).strip() == "":
        raise PathError("Empty path", "Pass a non-empty filesystem path.")

    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:  # pragma: no cover - exotic FS errors
        raise PathError(
            f"Cannot resolve path: {path} ({exc})", "Pass a valid filesystem path."
        ) from exc

    roots = allowed_roots()
    if not any(_is_within(resolved, root) for root in roots):
        raise PathError(
            f"Path outside allowed roots: {resolved}",
            f"Pass a path under one of: {', '.join(str(r) for r in roots)}",
        )

    if allowed_exts and resolved.suffix.lower() not in allowed_exts:
        raise PathError(
            f"Disallowed extension {resolved.suffix or '(none)'}: {resolved}",
            f"Use a file with one of these extensions: {' '.join(allowed_exts)}",
        )

    if must_exist and not resolved.exists():
        raise PathError(f"Path not found: {resolved}", "Check the path exists and is readable.")

    return resolved


def safe_mkdir(path: str | os.PathLike[str]) -> Path:
    """Resolve, create (parents, exist_ok), and return a directory path."""
    resolved = resolve_path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def sweep_dir(path: str | os.PathLike[str]) -> bool:
    """Delete a directory tree. Returns True if something was removed. Never raises."""
    try:
        resolved = resolve_path(path)
    except PathError:
        return False
    if not resolved.is_dir():
        return False
    shutil.rmtree(resolved, ignore_errors=True)
    return not resolved.exists()


def free_bytes(path: str | os.PathLike[str]) -> int:
    """Free space on the filesystem holding ``path`` (walks up to an existing parent)."""
    probe = Path(path).expanduser().resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def dir_size(path: str | os.PathLike[str]) -> int:
    """Total bytes of every regular file under ``path``. Never raises."""
    total = 0
    root = Path(path)
    if not root.exists():
        return 0
    for entry in root.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total
