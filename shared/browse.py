"""A read-only web view of the library — the durable record, made lookable-at.

Gated by the *same* token as everything else. Folio's ``/files`` route once sat behind
basic auth and its own comment explains why that was dropped: a browser username/password
popup "could never be satisfied by an access token anyway". Handing a user a second
credential to look at their own library defeats the point of one key everywhere.

So: paste the key once as ``?token=sk-sift-…``, get an HttpOnly cookie back, browse for 30
days. ``Authorization: Bearer`` works too, for scripts. The cookie holds a *minted session
token*, never the API key itself — see ``shared.oauth.mint_session``.

Rendering, not routing, lives here; ``server.py`` keeps the thin route bodies.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Rendered inline in the browser rather than downloaded — the whole point is to *read*
# the record without a round trip through a text editor.
INLINE_TYPES = {
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".ass": "text/plain; charset=utf-8",
    ".vtt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


@dataclass(frozen=True)
class Entry:
    name: str
    href: str
    is_dir: bool
    size: int
    modified: float


def resolve(root: Path, relative: str) -> Path | None:
    """Map a URL path to a file under ``root``. None if it escapes, or does not exist.

    ``Path.resolve()`` collapses ``..`` *and* follows symlinks, so the containment check
    happens on the fully-resolved path — a symlink inside the library pointing at /etc
    cannot be read through this.
    """
    try:
        target = (root / relative.lstrip("/")).resolve()
        target.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return target if target.exists() else None


def listing(target: Path, url_path: str) -> list[Entry]:
    """Directories first, then files; both alphabetical."""
    base = url_path.rstrip("/")
    entries: list[Entry] = []
    for child in target.iterdir():
        if child.name.startswith("."):
            continue  # .oauth-state and friends are machinery, not the record
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            Entry(
                name=child.name,
                href=f"{base}/{child.name}" + ("/" if child.is_dir() else ""),
                is_dir=child.is_dir(),
                size=stat.st_size,
                modified=stat.st_mtime,
            )
        )
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))


def _human(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"


def _crumbs(url_path: str) -> str:
    parts = [p for p in url_path.strip("/").split("/") if p and p != "library"]
    out = ['<a href="/library/">library</a>']
    acc = "/library"
    for p in parts:
        acc += f"/{p}"
        out.append(f'<a href="{html.escape(acc)}/">{html.escape(p)}</a>')
    return '<span class="sep">/</span>'.join(out)


def render(url_path: str, entries: list[Entry], empty_hint: str = "") -> str:
    """The listing page. Plain, fast, and readable on a phone."""
    if entries:
        rows = "\n".join(
            f'<tr><td><a href="{html.escape(e.href)}">'
            f"{'📁 ' if e.is_dir else '📄 '}{html.escape(e.name)}</a></td>"
            f'<td class="n">{"—" if e.is_dir else _human(e.size)}</td>'
            f'<td class="n">{datetime.fromtimestamp(e.modified, UTC):%Y-%m-%d %H:%M}</td></tr>'
            for e in entries
        )
        body = f'<table><tr><th>name</th><th class="n">size</th><th class="n">modified</th></tr>{rows}</table>'
    else:
        body = f'<p class="empty">{html.escape(empty_hint or "Empty.")}</p>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sift · library · {html.escape(url_path)}</title>
<style>
  :root{{color-scheme:dark}}
  body{{font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:#0e0e16;color:#e8e8f0;
       margin:0;padding:32px 20px;max-width:900px;margin-inline:auto}}
  h1{{font-size:14px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#8892A4;margin:0 0 4px}}
  .crumbs{{font-size:16px;margin:0 0 24px;word-break:break-all}}
  .crumbs a{{color:#E94560;text-decoration:none}}
  .crumbs a:hover{{text-decoration:underline}}
  .sep{{color:#3a3a5a;padding:0 6px}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;font-weight:500;color:#8892A4;font-size:12px;text-transform:uppercase;
     letter-spacing:.06em;border-bottom:1px solid #2a2a4a;padding:0 8px 8px}}
  td{{padding:7px 8px;border-bottom:1px solid #1a1a2e}}
  td a{{color:#e8e8f0;text-decoration:none}}
  td a:hover{{color:#E94560}}
  .n{{text-align:right;color:#8892A4;font-size:13px;white-space:nowrap}}
  .empty{{color:#8892A4}}
  footer{{margin-top:28px;color:#4a4a6a;font-size:12px}}
</style></head>
<body>
<h1>Sift library</h1>
<div class="crumbs">{_crumbs(url_path)}</div>
{body}
<footer>Read-only. The record is YAML on disk — edit it there and the next plan_clips reads your edit.
Source video is absent by design: downloaded, cut, deleted.</footer>
</body></html>"""


def gate_page(base: str) -> str:
    """Shown when there is no token and no session. Tells you how to get in."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sift · library</title>
<style>
  :root{{color-scheme:dark}}
  body{{font:15px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;background:#0e0e16;color:#e8e8f0;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}}
  .card{{background:#16182a;border:1px solid #2a2a4a;border-radius:12px;padding:32px;max-width:520px}}
  h1{{margin:0 0 12px;font-size:18px}}
  p{{color:#8892A4;margin:0 0 16px}}
  code{{background:#0e0e16;border:1px solid #2a2a4a;border-radius:4px;padding:2px 6px;
        color:#e8e8f0;word-break:break-all}}
  input{{width:100%;padding:10px 12px;background:#0e0e16;border:1px solid #2a2a4a;border-radius:6px;
        color:#e8e8f0;font:14px ui-monospace,monospace;box-sizing:border-box}}
  button{{margin-top:12px;width:100%;padding:10px;background:#E94560;color:#fff;border:0;
         border-radius:6px;font-weight:600;cursor:pointer;font-size:14px}}
</style></head>
<body><div class="card">
<h1>Access token required</h1>
<p>The library uses the <strong>same key</strong> as the MCP endpoint — no separate login.
Paste any value from <code>tokens.json</code>. It becomes a 30-day session cookie; the key
itself is never stored in the browser.</p>
<form method="GET" action="{html.escape(base)}">
<input name="token" type="password" placeholder="sk-sift-…" autocomplete="off" required autofocus>
<button type="submit">Open library</button>
</form>
</div></body></html>"""
