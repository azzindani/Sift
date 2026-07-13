"""Token auth for the HTTP transport — the same contract as Folio's ``src/mcp/auth.ts``.

Four modes, evaluated in this priority order, so one deployment can be a shared VPS
endpoint and another can be a laptop with no config at all:

1. ``SIFT_TOKENS_FILE=/path/tokens.json``  →  ``{"claude": "sk-...", "hermes": "sk-..."}``
2. ``SIFT_TOKENS="claude:sk-aaa,hermes:sk-bbb"``  (inline, good for compose env files)
3. ``SIFT_API_KEY="sk-..."``  (single shared bearer, registered as "default")
4. (none) → **open**. Localhost / private network only; the startup banner says so loudly.

Wire format is ``Authorization: Bearer <token>`` — identical to Folio, so a client
configured for one works against the other.

Named tokens exist so the audit log can say *who* called a tool, not just *that* someone
did. A revoked name is one line out of a JSON file.

**Fail closed.** Mode 4 is reachable only when *nothing* is configured. If a source IS
configured and cannot be used — file missing, unreadable, malformed, empty — this raises
``AuthConfigError`` and the server refuses to boot. It must never degrade to open: the
first real deployment mounted ``tokens.json`` as ``600 root:root`` while the container
runs as uid 999, and the old code logged a warning and served the whole tool surface
unauthenticated. A misconfigured lock has to be a locked door, not an open one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("sift.auth")

OPEN_PRINCIPAL = "__open__"


class AuthConfigError(RuntimeError):
    """An auth source was configured but is unusable. Startup aborts; never fall back."""


@dataclass(frozen=True)
class TokenRegistry:
    """Bearer value → human-readable name. Built once from the environment."""

    mode: str  # "open" | "single" | "multi"
    tokens: dict[str, str] = field(default_factory=dict)


_registry: TokenRegistry | None = None
_lock = threading.Lock()


def load_tokens(force: bool = False) -> TokenRegistry:
    """Build and cache the registry from env. ``force=True`` re-reads it (tests)."""
    global _registry
    with _lock:
        if _registry is not None and not force:
            return _registry

        tokens: dict[str, str] = {}

        path = os.environ.get("SIFT_TOKENS_FILE", "").strip()
        if path:
            try:
                parsed = json.loads(Path(path).read_text(encoding="utf-8"))
            except OSError as exc:
                raise AuthConfigError(
                    f"SIFT_TOKENS_FILE={path} could not be read: {exc}. "
                    f"In Docker the container runs as uid 999 — the file must be "
                    f"readable by it (chown 999:999 tokens.json). Refusing to start "
                    f"unauthenticated."
                ) from exc
            except (json.JSONDecodeError, AttributeError) as exc:
                raise AuthConfigError(
                    f"SIFT_TOKENS_FILE={path} is not valid JSON: {exc}. Expected "
                    f'{{"name": "sk-..."}}. Refusing to start unauthenticated.'
                ) from exc

            for name, value in parsed.items():
                if isinstance(value, str) and value:
                    tokens[value] = str(name)
            if not tokens:
                raise AuthConfigError(
                    f"SIFT_TOKENS_FILE={path} has no usable entries. Expected "
                    f'{{"name": "sk-..."}}. Refusing to start unauthenticated.'
                )
            _registry = TokenRegistry("multi", tokens)
            return _registry

        inline = os.environ.get("SIFT_TOKENS", "").strip()
        if inline:
            for pair in inline.split(","):
                name, _, value = pair.partition(":")
                if name.strip() and value.strip():
                    tokens[value.strip()] = name.strip()
            if not tokens:
                raise AuthConfigError(
                    'SIFT_TOKENS is set but unusable (expected "name:value,name:value"). '
                    "Refusing to start unauthenticated."
                )
            _registry = TokenRegistry("multi", tokens)
            return _registry

        single = os.environ.get("SIFT_API_KEY", "").strip()
        if single:
            _registry = TokenRegistry("single", {single: "default"})
            return _registry

        _registry = TokenRegistry("open", {})
        return _registry


def authorize(header: str | None) -> str | None:
    """Return the token's name, ``"__open__"`` when unauthenticated mode, or None.

    Comparison is constant-time-ish via a dict lookup on the full value; we never
    compare prefixes, so a partial token can't be probed byte by byte.
    """
    registry = load_tokens()
    if registry.mode == "open":
        return OPEN_PRINCIPAL

    if not header or not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer ") :].strip()
    if not presented:
        return None
    return registry.tokens.get(presented)


def describe_auth() -> str:
    """One line for the startup banner. Says plainly when the server is wide open."""
    registry = load_tokens()
    if registry.mode == "open":
        return "UNAUTHENTICATED — no SIFT_TOKENS_FILE / SIFT_TOKENS / SIFT_API_KEY set"
    if registry.mode == "single":
        return 'single shared bearer (SIFT_API_KEY) registered as "default"'
    return f"{len(registry.tokens)} named token(s): {', '.join(sorted(registry.tokens.values()))}"


def reset_for_tests() -> None:
    """Drop the cached registry so a fresh environment is re-read."""
    global _registry
    with _lock:
        _registry = None


# --------------------------------------------------------------------------
# Rate limiting — a token bucket per (token, client IP)
# --------------------------------------------------------------------------


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    """Token bucket keyed on (principal, IP). Set burst or rate to 0 to disable.

    Keyed on *both* so one leaked token can't be used from a hundred hosts to bypass the
    limit, and one shared NAT egress IP can't starve every other client behind it.
    """

    def __init__(self, burst: int, per_sec: float) -> None:
        self.burst = max(0, burst)
        self.per_sec = max(0.0, per_sec)
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.burst > 0 and self.per_sec > 0

    def allow(self, principal: str, ip: str) -> bool:
        if not self.enabled:
            return True

        key = (principal, ip)
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = _Bucket(tokens=float(self.burst) - 1.0, last=now)
                return True

            bucket.tokens = min(
                float(self.burst), bucket.tokens + (now - bucket.last) * self.per_sec
            )
            bucket.last = now
            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True


def rate_limiter_from_env() -> RateLimiter:
    """Build the limiter from SIFT_RATE_BURST / SIFT_RATE_PER_SEC (0 disables)."""

    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except ValueError:
            return default

    return RateLimiter(burst=_int("SIFT_RATE_BURST", 40), per_sec=_int("SIFT_RATE_PER_SEC", 10))
