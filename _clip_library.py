"""The project library — Folio-style, file-backed, human-editable.

**Files are the record.** A project is a directory of YAML you can read, diff, and
hand-edit; SQLite is demoted to two jobs it is actually good at: the render queue
(atomic claim, restart reconciliation) and a *rebuildable index* from entity id to
project. Delete `sift.db` and it repopulates from the files on next call — the
database can never disagree with the library, because the library wins.

```
~/.sift/projects/<project>/
├── project.yaml            # index: sources, clips, exports
├── sources/<source_id>/
│   ├── source.yaml         # url, title, duration, transcript_kind
│   ├── transcript.json     # durable — outlives the video (~400 KB for 3h)
│   └── video.mp4           # EPHEMERAL — deleted at publish
├── candidates/<source_id>.yaml   # the agent's picks — edit these by hand
├── clips/<clip_id>/
│   ├── clip.yaml           # members, label, assembly spec
│   ├── clip.mp4            # durable artifact
│   └── clip.jpg
└── exports/<batch_id>/     # manifest.json, index.html
```

**The source video is the one thing that stays disposable.** Folio's library items
are kilobytes of YAML; ours would be gigabytes of video — a 3-hour source at 720p is
~2.7 GB, so twenty of them fill the disk this is meant to run on. Clips (~10 MB),
transcripts (~400 KB), and candidates are durable. The video is not.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from _clip_helpers import connect, new_id, now_iso, now_ts, projects_dir
from shared.file_utils import resolve_path, safe_mkdir
from shared.version_control import atomic_write_text

log = logging.getLogger("clipper.library")

DEFAULT_PROJECT = "default"
_SAFE_PROJECT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$", re.I)


class LibraryError(Exception):
    """A library operation failed. Carries an actionable hint."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------
# YAML helpers — atomic writes, never a half-written record
# --------------------------------------------------------------------------


def _dump(payload: Any) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)


def write_yaml(path: Path, payload: Any) -> Path:
    """Serialize to YAML and write atomically (temp file + fsync + rename)."""
    return atomic_write_text(path, _dump(payload))


def read_yaml(path: Path, fallback: Any = None) -> Any:
    """Read a YAML record. Returns ``fallback`` if absent; raises on malformed YAML."""
    if not path.is_file():
        return fallback if fallback is not None else {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or (
            fallback if fallback is not None else {}
        )
    except yaml.YAMLError as exc:
        raise LibraryError(
            f"Malformed YAML in {path}: {exc}",
            "Fix the file by hand, or delete it and re-run the step that created it.",
        ) from exc


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


def validate_project(name: str) -> str:
    """Reject a project name that would escape the library root."""
    cleaned = (name or "").strip() or DEFAULT_PROJECT
    if not _SAFE_PROJECT.match(cleaned):
        raise LibraryError(
            f"Invalid project name: {cleaned!r}",
            "Use letters, digits, dot, dash, underscore — e.g. 'podcast-ep42'.",
        )
    return cleaned


def project_dir(name: str) -> Path:
    """Absolute path to a project, creating it if needed. Always inside the library root."""
    project = validate_project(name)
    root = safe_mkdir(projects_dir() / project)
    resolve_path(root)  # containment check against the allowed roots
    return root


def ensure_project(name: str) -> dict[str, Any]:
    """Scaffold a project directory and its project.yaml. Idempotent."""
    project = validate_project(name)
    root = project_dir(project)
    for sub in ("sources", "candidates", "clips", "exports"):
        safe_mkdir(root / sub)

    manifest_path = root / "project.yaml"
    record = read_yaml(manifest_path, {})
    if not record:
        record = {
            "_protocol": "sift/project/v1",
            "name": project,
            "created": now_iso(),
            "modified": now_iso(),
            "sources": [],
            "clips": [],
            "exports": [],
        }
        write_yaml(manifest_path, record)
        log.info("scaffolded project %s at %s", project, root)
    return record


def touch_project(name: str, **updates: Any) -> dict[str, Any]:
    """Update project.yaml's index lists and modified stamp."""
    root = project_dir(name)
    record = ensure_project(name)
    for key, value in updates.items():
        existing = record.get(key) or []
        if isinstance(existing, list) and value not in existing:
            existing.append(value)
            record[key] = existing
    record["modified"] = now_iso()
    write_yaml(root / "project.yaml", record)
    return record


def list_projects() -> list[dict[str, Any]]:
    """Every project in the library, with counts. Reads the files, not the DB."""
    root = projects_dir()
    out: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if not entry.is_dir() or not (entry / "project.yaml").is_file():
            continue
        record = read_yaml(entry / "project.yaml", {})
        out.append(
            {
                "project": entry.name,
                "created": record.get("created", ""),
                "modified": record.get("modified", ""),
                "sources": len(list((entry / "sources").glob("*/source.yaml"))),
                "clips": len(list((entry / "clips").glob("*/clip.yaml"))),
                "exports": len(list((entry / "exports").glob("*/manifest.json"))),
            }
        )
    return out


# --------------------------------------------------------------------------
# The index — derived from the files, never the source of truth
# --------------------------------------------------------------------------


def index_put(entity_id: str, kind: str, project: str) -> None:
    """Record which project an id lives in. Rebuildable, so a lost row is not fatal."""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO library_index (entity_id, kind, project) VALUES (?, ?, ?)",
            (entity_id, kind, project),
        )


def index_get(entity_id: str) -> str:
    """Which project holds this id. Falls back to a filesystem scan, then repairs the index."""
    with connect() as conn:
        row = conn.execute(
            "SELECT project FROM library_index WHERE entity_id = ?", (entity_id,)
        ).fetchone()
    if row:
        return str(row["project"])

    # The index is a cache. If it is cold (fresh DB, restored backup, someone
    # dropped a project in by hand), the files still know the answer.
    for kind, pattern in (("source", "sources/*/source.yaml"), ("clip", "clips/*/clip.yaml")):
        for project in list_projects():
            root = project_dir(project["project"])
            for path in root.glob(pattern):
                if path.parent.name == entity_id:
                    index_put(entity_id, kind, project["project"])
                    return project["project"]
    return ""


def rebuild_index() -> int:
    """Re-derive the whole index from the files. Called at startup; cheap and idempotent."""
    count = 0
    with connect() as conn:
        conn.execute("DELETE FROM library_index")
        for project in list_projects():
            root = project_dir(project["project"])
            for path in root.glob("sources/*/source.yaml"):
                conn.execute(
                    "INSERT OR REPLACE INTO library_index VALUES (?, ?, ?)",
                    (path.parent.name, "source", project["project"]),
                )
                count += 1
            for path in root.glob("clips/*/clip.yaml"):
                conn.execute(
                    "INSERT OR REPLACE INTO library_index VALUES (?, ?, ?)",
                    (path.parent.name, "clip", project["project"]),
                )
                count += 1
    return count


def _require_project(entity_id: str, kind: str) -> str:
    project = index_get(entity_id)
    if not project:
        raise LibraryError(
            f"Unknown {kind}: {entity_id}",
            f"Call list_library() to see what exists, or fetch_source(url) to create a {kind}.",
        )
    return project


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def source_dir(source_id: str, project: str = "") -> Path:
    project = project or _require_project(source_id, "source")
    return project_dir(project) / "sources" / source_id


def save_source(record: dict[str, Any]) -> dict[str, Any]:
    """Write source.yaml and index it. The record is the file, not a DB row."""
    project = record["project"]
    ensure_project(project)
    root = safe_mkdir(project_dir(project) / "sources" / record["source_id"])
    write_yaml(root / "source.yaml", record)
    index_put(record["source_id"], "source", project)
    touch_project(project, sources=record["source_id"])
    return record


def load_source(source_id: str) -> dict[str, Any]:
    """Read source.yaml. {} if unknown."""
    project = index_get(source_id)
    if not project:
        return {}
    record = read_yaml(project_dir(project) / "sources" / source_id / "source.yaml", {})
    return record if isinstance(record, dict) else {}


def update_source(source_id: str, **fields: Any) -> dict[str, Any]:
    """Patch a source record in place."""
    record = load_source(source_id)
    if not record:
        raise LibraryError(f"Unknown source_id: {source_id}", "Call fetch_source(url) first.")
    record.update(fields)
    return save_source(record)


def find_source_by_url(url: str, project: str) -> dict[str, Any]:
    """A prior source for this URL in this project, video on disk or not.

    It used to require the video to still be present, and that was a real bug: the video
    is *deliberately* deleted at publish, so every re-fetch of a published URL minted a
    NEW source_id — duplicating the transcript, orphaning the candidates, and leaving the
    existing clips pointing at a source whose video could never come back. Which made
    render_clip's own hint ("call fetch_source again") a lie.

    Now the record is the identity. The video is just a cache: absent, the caller
    re-downloads it into the same source_id and everything downstream still resolves.
    """
    root = project_dir(project) / "sources"
    if not root.is_dir():
        return {}
    for path in sorted(root.glob("*/source.yaml")):
        record = read_yaml(path, {})
        if record.get("url") == url:
            return record
    return {}


def source_video_present(record: dict[str, Any]) -> bool:
    """Is this source's video actually on disk right now?"""
    local = record.get("local_path") or ""
    return bool(local) and Path(local).is_file()


def load_transcript(source_id: str) -> dict[str, Any]:
    """The parsed transcript. Survives deletion of the video — that is the point."""
    record = load_source(source_id)
    path = record.get("transcript_path") or ""
    if not path or not Path(path).is_file():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def all_sources(project: str = "") -> list[dict[str, Any]]:
    """Every source record, in one project or across the library."""
    projects = [project] if project else [p["project"] for p in list_projects()]
    out: list[dict[str, Any]] = []
    for name in projects:
        root = project_dir(name) / "sources"
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/source.yaml")):
            record = read_yaml(path, {})
            if record:
                out.append(record)
    return out


# --------------------------------------------------------------------------
# Candidates — one hand-editable YAML file per source
# --------------------------------------------------------------------------


def candidates_path(source_id: str) -> Path:
    project = _require_project(source_id, "source")
    return project_dir(project) / "candidates" / f"{source_id}.yaml"


def save_candidates(source_id: str, candidates: list[dict[str, Any]]) -> None:
    """Replace the candidate file for a source. Rewritten whole, atomically."""
    path = candidates_path(source_id)
    safe_mkdir(path.parent)
    write_yaml(
        path,
        {
            "_protocol": "sift/candidates/v1",
            "source_id": source_id,
            "modified": now_iso(),
            "candidates": candidates,
        },
    )


def load_candidates(source_id: str) -> list[dict[str, Any]]:
    """Candidates for a source, in timeline order. Reads the YAML the agent may have edited."""
    project = index_get(source_id)
    if not project:
        return []
    path = project_dir(project) / "candidates" / f"{source_id}.yaml"
    record = read_yaml(path, {})
    candidates = record.get("candidates") or [] if isinstance(record, dict) else []
    return sorted(candidates, key=lambda c: (float(c.get("start", 0)), float(c.get("end", 0))))


# --------------------------------------------------------------------------
# Clips
# --------------------------------------------------------------------------


def clip_dir(clip_id: str, project: str = "") -> Path:
    project = project or _require_project(clip_id, "clip")
    return project_dir(project) / "clips" / clip_id


def save_clip(record: dict[str, Any]) -> dict[str, Any]:
    """Write clip.yaml — the clip definition, editable by hand before a re-render."""
    project = record["project"]
    root = safe_mkdir(project_dir(project) / "clips" / record["clip_id"])
    write_yaml(root / "clip.yaml", record)
    index_put(record["clip_id"], "clip", project)
    touch_project(project, clips=record["clip_id"])
    return record


def load_clip(clip_id: str) -> dict[str, Any]:
    """Read clip.yaml, with its member candidates resolved from the candidate file."""
    project = index_get(clip_id)
    if not project:
        return {}
    record = read_yaml(project_dir(project) / "clips" / clip_id / "clip.yaml", {})
    if not record:
        return {}

    by_id = {c["candidate_id"]: c for c in load_candidates(record["source_id"])}
    members = [by_id[cid] for cid in record.get("members", []) if cid in by_id]
    record["member_rows"] = sorted(members, key=lambda m: float(m["start"]))
    return record


def clear_clips(source_id: str) -> None:
    """Drop a source's clip definitions. plan_clips replans from scratch each call."""
    project = index_get(source_id)
    if not project:
        return
    root = project_dir(project) / "clips"
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/clip.yaml")):
        record = read_yaml(path, {})
        if record.get("source_id") != source_id:
            continue
        # Only remove the definition; a rendered clip.mp4 already published is durable.
        if not (path.parent / "clip.mp4").is_file():
            path.unlink(missing_ok=True)
            with connect() as conn:
                conn.execute("DELETE FROM library_index WHERE entity_id = ?", (path.parent.name,))
            with contextlib.suppress(OSError):
                path.parent.rmdir()


def all_clips(project: str = "") -> list[dict[str, Any]]:
    """Every clip definition, in one project or across the library."""
    projects = [project] if project else [p["project"] for p in list_projects()]
    out: list[dict[str, Any]] = []
    for name in projects:
        root = project_dir(name) / "clips"
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/clip.yaml")):
            record = read_yaml(path, {})
            if not record:
                continue
            record["rendered"] = (path.parent / "clip.mp4").is_file()
            out.append(record)
    return out


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


def export_dir(project: str, batch_id: str) -> Path:
    return safe_mkdir(project_dir(project) / "exports" / batch_id)


def all_exports(project: str = "") -> list[dict[str, Any]]:
    """Every published batch manifest, in one project or across the library."""
    projects = [project] if project else [p["project"] for p in list_projects()]
    out: list[dict[str, Any]] = []
    for name in projects:
        root = project_dir(name) / "exports"
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/manifest.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            manifest["project"] = name
            manifest["manifest_path"] = str(path)
            out.append(manifest)
    return out


def expired_exports(now: float | None = None) -> list[dict[str, Any]]:
    """Batches past their TTL. Retention applies to *links*; the clips stay in the library."""
    cutoff = now if now is not None else now_ts()
    out = []
    for manifest in all_exports():
        expires = manifest.get("expires_ts") or 0
        if expires and float(expires) < cutoff:
            out.append(manifest)
    return out


def new_source_id() -> str:
    return new_id("s")


def new_clip_id() -> str:
    return new_id("c")


def new_batch_id() -> str:
    return new_id("b")
