"""Binary discovery and the one sanctioned subprocess entry point.

Every external process (ffmpeg, ffprobe, yt-dlp) is launched through ``run()``:
an argument list, ``shell=False``, an explicit timeout, captured output. There is
no other way to spawn a process in this codebase, so there is no place for a
shell injection to hide.

yt-dlp is a Python dependency, so it runs as ``python -m yt_dlp`` rather than a
PATH lookup — the version is then pinned by ``uv.lock`` instead of by whatever
happens to be installed on the host.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

__all__ = [
    "BinaryMissing",
    "CommandResult",
    "run",
    "ffmpeg_bin",
    "ffprobe_bin",
    "ytdlp_cmd",
    "cpu_count",
    "check_toolchain",
]


class BinaryMissing(RuntimeError):
    """A required external binary is not installed. Carries an actionable hint."""

    def __init__(self, name: str, hint: str) -> None:
        super().__init__(f"Required binary not found: {name}")
        self.name = name
        self.hint = hint


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a subprocess run. ``timed_out`` distinguishes a kill from a failure."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    args: list[str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run(
    args: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> CommandResult:
    """Run a subprocess safely. Never raises on non-zero exit or timeout.

    Enforces the STANDARDS §18 contract: argument list, shell=False, explicit
    timeout, captured output. A timeout returns ``timed_out=True`` rather than
    propagating, so callers can turn it into an error dict with a hint.
    """
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise TypeError("run() takes a list[str] argv — never a shell string")

    merged_env = {**os.environ, **(env or {})}
    log.debug("exec: %s", " ".join(args[:6]))
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False, timeout set
            args,
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=merged_env,
            input=stdin_text,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=-1,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            timed_out=True,
            args=args,
        )
    except FileNotFoundError as exc:
        raise BinaryMissing(args[0], f"Install {args[0]} and ensure it is on PATH.") from exc

    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        timed_out=False,
        args=args,
    )


def _which(name: str, env_override: str) -> str:
    override = os.environ.get(env_override, "").strip()
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise BinaryMissing(
            name,
            f"Install {name} (e.g. `apt install ffmpeg`) or set {env_override} to its path.",
        )
    return found


def ffmpeg_bin() -> str:
    """Absolute path to ffmpeg. Raises BinaryMissing with an install hint."""
    return _which("ffmpeg", "CLIPPER_FFMPEG")


def ffprobe_bin() -> str:
    """Absolute path to ffprobe. Raises BinaryMissing with an install hint."""
    return _which("ffprobe", "CLIPPER_FFPROBE")


def ytdlp_cmd() -> list[str]:
    """Argv prefix for yt-dlp — the pinned Python module, not a PATH binary."""
    return [sys.executable, "-m", "yt_dlp"]


def cpu_count() -> int:
    """Usable CPU count, floored at 1."""
    return max(1, os.cpu_count() or 1)


def check_toolchain() -> dict[str, str]:
    """Report which external binaries resolve. Used by the server at startup."""
    report: dict[str, str] = {}
    for name, getter in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
        try:
            report[name] = getter()
        except BinaryMissing as exc:
            report[name] = f"MISSING — {exc.hint}"
    report["yt_dlp"] = " ".join(ytdlp_cmd())
    return report
