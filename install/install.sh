#!/bin/sh
# Clipper installer — POSIX sh, no bashisms.
#
# Clones (or updates) the server into ~/.mcp_servers/Clipper, syncs deps with uv,
# and prints the mcp.json block to paste into your client.
#
# The clone-guard matters: if the target exists but is not a git checkout, it is
# removed rather than fetched into, so a half-written directory can never poison
# a later `git reset --hard`.

set -eu

REPO="${CLIPPER_REPO:-https://github.com/azzindani/Sift.git}"
DEST="${CLIPPER_HOME:-$HOME/.mcp_servers/Clipper}"
PORT="${CLIPPER_PORT:-8765}"

info() { printf '  %s\n' "$1"; }
die() { printf 'error: %s\n' "$1" >&2; exit 1; }

printf '\nClipper installer\n\n'

# ---- prerequisites -------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is not installed."

if ! command -v uv >/dev/null 2>&1; then
    info "uv not found — installing it"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC2086
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi
command -v uv >/dev/null 2>&1 || die "uv install failed. See https://docs.astral.sh/uv/"

if ! command -v ffmpeg >/dev/null 2>&1; then
    die "ffmpeg is not installed. Install it first:
      Debian/Ubuntu:  sudo apt install ffmpeg
      Fedora:         sudo dnf install ffmpeg
      macOS:          brew install ffmpeg
    Clipper needs ffmpeg with libx264 and libass."
fi

if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264; then
    die "ffmpeg is installed but has no libx264 encoder. Install a full ffmpeg build."
fi
info "ffmpeg with libx264: ok"

# ---- clone or update -----------------------------------------------------
if [ -d "$DEST/.git" ]; then
    info "updating $DEST"
    cd "$DEST"
    git fetch origin --quiet
    git reset --hard FETCH_HEAD --quiet
else
    info "cloning into $DEST"
    rm -rf "$DEST"                       # clone-guard: not a git checkout, so replace it
    mkdir -p "$(dirname "$DEST")"
    git clone "$REPO" "$DEST" --quiet
    cd "$DEST"
fi

# ---- dependencies --------------------------------------------------------
info "syncing dependencies"
uv sync --quiet

# Face-follow reframing is a ~300 MB optional extra. Skip it on a small box: without
# it, reframe="speaker" degrades to a centred crop and everything else still works.
if [ "${CLIPPER_VISION:-0}" = "1" ]; then
    info "installing the vision extra (MediaPipe face-follow)"
    uv sync --extra vision --quiet
else
    info "skipping MediaPipe (set CLIPPER_VISION=1 to enable face-follow reframing)"
fi

# ---- constrained mode ----------------------------------------------------
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
CONSTRAINED=0
if [ "$MEM_KB" -gt 0 ] && [ "$MEM_KB" -lt 6291456 ]; then   # < 6 GB
    CONSTRAINED=1
    info "small box detected (<6 GB RAM) — enabling MCP_CONSTRAINED_MODE"
fi

printf '\nInstalled to %s\n\n' "$DEST"
printf 'Add this to your MCP client config:\n\n'
cat <<JSON
{
  "mcpServers": {
    "clipper": {
      "command": "sh",
      "args": [
        "-c",
        "d=\"\$HOME/.mcp_servers/Clipper\"; if [ ! -d \"\$d/.git\" ]; then rm -rf \"\$d\"; git clone $REPO \"\$d\" --quiet; else cd \"\$d\" && git fetch origin --quiet && git reset --hard FETCH_HEAD --quiet; fi; cd \"\$d\" && uv sync --quiet && exec uv run python server.py --transport stdio"
      ],
      "env": {
        "MCP_CONSTRAINED_MODE": "$CONSTRAINED",
        "CLIPPER_BASE_URL": "http://localhost:$PORT/clips"
      },
      "timeout": 600000
    }
  }
}
JSON

printf '\nOr run it over HTTP on a VPS:\n'
printf '  cd %s && MCP_CONSTRAINED_MODE=%s uv run python server.py --transport http --port %s\n\n' \
    "$DEST" "$CONSTRAINED" "$PORT"
printf 'Set CLIPPER_BASE_URL to your public URL so published links resolve.\n'
printf 'If yt-dlp hits bot challenges on a VPS IP, set CLIPPER_COOKIES_PATH or CLIPPER_PROXY.\n\n'
