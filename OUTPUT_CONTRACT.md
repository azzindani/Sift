# OUTPUT_CONTRACT.md — Clipper

The final deliverable: **multiple links plus a verifiable summary** that maps every clip back
to its source. This doc defines the manifest schema, the summary the agent returns, and the
link lifecycle. Inspired by how Folio projects emit output, adapted for a remote VPS.

---

## 1. What `publish_outputs` returns

For a set of finished render jobs, `publish_outputs`:

1. Moves each clip + thumbnail into the **served directory** under an unguessable path.
2. Writes a `manifest.json` for the batch.
3. Returns a dict containing the **links** and a **summary** the agent can present verbatim.

Every clip in the output carries its **source mapping** so the user can double-check the
selection against the original video — this is a first-class requirement, not optional.

---

## 2. Manifest schema (`manifest.json`)

```json
{
  "batch_id": "b_8f2a...",
  "source": {
    "source_id": "s_19c4...",
    "url": "https://www.youtube.com/watch?v=...",
    "title": "3hr Podcast with ...",
    "duration_s": 11820.0
  },
  "created_at": "2026-06-18T03:10:00Z",
  "ttl_hours": 168,
  "expires_at": "2026-06-25T03:10:00Z",
  "clips": [
    {
      "clip_id": "c_2b71...",
      "label": "argument",
      "duration_s": 47.3,
      "link": "https://clips.casava.space/b_8f2a/c_2b71.mp4",
      "thumbnail": "https://clips.casava.space/b_8f2a/c_2b71.jpg",
      "source_url": "https://www.youtube.com/watch?v=...",
      "source_start": 2710.4,
      "source_end": 2757.7,
      "source_link": "https://www.youtube.com/watch?v=...&t=2710s",
      "built_from": [
        {"candidate_id": "cand_44", "start": 2710.4, "end": 2735.0},
        {"candidate_id": "cand_45", "start": 2740.2, "end": 2757.7}
      ],
      "reason": "clean disagreement, lands without setup"
    }
  ]
}
```

Key fields for verification:
- **`source_link`** — a deep link to the exact source timestamp (`&t=<start>s`) so the user
  jumps straight to the moment in the original.
- **`built_from`** — the candidate spans the clip was assembled from (shows dead-air trims and
  any multi-cut composition).
- **`reason`** — the agent's stated rationale for the pick.

---

## 3. The summary (returned to the agent / user)

A compact, human-checkable block the agent can present directly. Example shape:

```
Produced 5 clips from "3hr Podcast with ..." (3:17:00)

1. [argument · 47s]  clips.casava.space/b_8f2a/c_2b71.mp4
   source 45:10–45:57 · youtube.com/watch?v=...&t=2710s
   "clean disagreement, lands without setup"

2. [joke · 22s]      clips.casava.space/b_8f2a/c_9d04.mp4
   source 1:12:30–1:12:52 · youtube.com/watch?v=...&t=4350s
   "self-contained bit, punchline intact"
...
Links expire in 7 days.
```

Each line gives the link, the **source timestamp range**, a **deep link to the original**,
and the rationale — everything needed to double-check a selection at a glance.

---

## 4. Link lifecycle and retention

- **Served directory**, not the repo dir and not system temp (STANDARDS §26 spirit, adapted
  for HTTP delivery). Served by Caddy/static handler.
- **Unguessable paths** (`batch_id`/`clip_id` with random components). No directory listing.
- **TTL retention.** Default 7 days (`ttl_hours=168`). A cleanup sweep deletes expired batches
  and their manifests so storage never grows unbounded on the small VPS.
- **Immutable artifacts.** Once published, clips are not edited in place; a re-render produces
  a new `clip_id`.
- **Disposable source.** The source video was already deleted after the cut step; only clips,
  thumbnails, and the manifest persist.

---

## 5. Optional HTML index

`publish_outputs` may also emit an `index.html` in the batch dir — a simple gallery of the
clips with inline players, thumbnails, source deep-links, and rationale. Same data as the
manifest, rendered for human review. Themed consistently (one stylesheet), responsive, no
remote CDNs in production (bundle or self-host any player assets).

---

## 6. Verification flow (the user's double-check)

1. User opens the summary.
2. For any clip, clicks the **source deep link** → lands at the exact moment in the original.
3. Compares the clip against the source span shown in `built_from`.
4. If a pick is wrong, the agent can re-run `plan_clips`/`render_clip` with adjusted
   boundaries — the manifest's `built_from` makes it obvious what to change.

This closes the loop: the engine does the heavy lifting, and the output is fully auditable
back to the source.
