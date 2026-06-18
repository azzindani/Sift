# LABELS_AND_SKILLS.md — Clipper

This is the heart of "not just quotes." A small **label registry** plus **injected skills**
turns one pipeline into a general clip engine. Adding a new clip type is a data change (a
label + a skill file), never an engine change.

---

## 1. Two layers of intent

**Layer 1 — user intent (what output they want).** "Funny moments", "a montage of every
money mention", "the best arguments". This is a job-level parameter: stated by the user or
classified by the agent into a `mode`. It selects which skills load and which scoring rubric
the agent uses. This is routing — trivial.

**Layer 2 — content intent (what's happening in a span).** Each candidate is tagged with a
`label`: `quote`, `joke`, `story`, `argument`, `hot_take`, `reaction`. This is reliable for
**text-carried** intent (the agent reads it and tags it). For **non-text** intent (visual
laughter, a reaction with no words) the label can only be assigned *after* the cheap-cue →
vision funnel confirms the moment exists.

**Ordering rule:**
- Text moment: `read chunk → label → skill` (label-first).
- Non-text moment: `cheap cue flags region → vision confirms → label → skill` (cue-first).

---

## 2. The label is the routing key

A `label` is not just a tag — it is the key that pulls a **skill**. A skill is a procedural
prompt plus a set of deterministic assembly parameters. The label drives boundary refinement,
reframe strategy, caption style, and trim aggressiveness.

```
label  ──►  skill { procedural_prompt, assembly_params }  ──►  shapes how the clip is built
```

Skills live as files the agent reads when a candidate of that label moves to refinement —
the same procedural-injection pattern used elsewhere in the ecosystem (predefined system
prompts, Folio templates). The engine never interprets the skill; it consumes only the
deterministic `assembly_params` the agent passes through.

---

## 3. Label registry (v1)

| label | what it captures | detection | reframe default | trim | caption emphasis |
|---|---|---|---|---|---|
| `quote` | a sharp, self-contained line | text | speaker | tight | key phrase highlight |
| `joke` | setup + punchline | text | speaker | very tight | land on punchline |
| `story` | a short narrative w/ payoff | text | speaker | gentle (preserve setup) | minimal |
| `argument` | a clean disagreement | text | stacked (two-shot) | medium | both speakers |
| `hot_take` | a controversial claim | text | speaker | tight | claim emphasized |
| `reaction` | laughter / facial reaction | cue → vision | speaker (or follow) | tight around the beat | sparse |

`mode` values map to label sets and grouping: `auto` (all), `by_label`, `by_topic`,
`montage` (cross-span theme group), `supercut` (repeated phrase/entity).

---

## 4. Skill anatomy

Each skill file (e.g. `skills/argument.md`) contains:

**Procedural prompt (for the agent):**
- The **cold-open test** (mandatory in every skill): the clip's first sentence must be
  intelligible with zero prior context; if it opens on a dangling pronoun/reference, extend
  `start` to include the antecedent. The 2-min overlap window exists so the agent can see it.
- Type-specific timing guidance: e.g. `joke` → cut in tight, land hard on the punchline, hard
  out; `story` → preserve the setup even if it costs a few seconds; `argument` → start at the
  point of disagreement, keep both voices.
- A self-containment / payoff check appropriate to the type.

**Assembly params (deterministic, passed to the engine):**
```json
{
  "reframe": "stacked",
  "trim_aggressiveness": "medium",
  "caption_style": "dual_speaker",
  "silence_threshold_db": -30,
  "pad_ms": 150,
  "max_duration_s": 60
}
```

The agent applies the procedural prompt while choosing boundaries and submits the candidate
with its `label`; `plan_clips`/`render_clip` consume the matching `assembly_params`. Brain in
the agent, determinism in the engine.

---

## 5. Montages and supercuts (assembly, not selection)

A montage is *grouping + multi-cut*, reusing everything else:

1. Selection already tagged every candidate with `label` + topic.
2. `plan_clips(mode="montage")` clusters candidates — by topic ("every money mention"), by
   label ("all the laughs"), or by entity ("every Elon mention"). Mostly a `GROUP BY` over the
   candidate table plus an agent grouping pass for fuzzy topics.
3. `render_clip` assembles the group with `xfade`/`acrossfade` joins, optional beat-synced cuts.

**Supercut** (rapid repeats of one phrase) is the *easiest* montage: grouping is an exact
phrase match, then concat the hits.

The only genuinely new logic for montages is the grouping pass; the cut/reframe/caption path
is identical to single clips.

---

## 6. Adding a new clip type

1. Add a row to the label registry (a new `label`).
2. Write `skills/<label>.md` (procedural prompt + assembly_params).
3. Done. No engine change, no new tool. The pipeline (fetch → read → label → plan → render →
   publish) is fixed; behavior is data-driven by the label/skill registry.

This is what makes the goal — *don't build this only for quotes* — actually true.

---

## 7. Honest constraint

Label quality is bounded by signal. Text-intent labels are dependable. Intent that lives
only in the video (visual humor, energy, vibe) needs the cue→vision funnel to even produce a
candidate before a label can attach — and "funny" is subjective enough that no model scores
it perfectly (commercial tools struggle too). The edge is the **cheap-signal-then-expensive-
verify** funnel: free audio/text cues decide *where to look*, vision confirms only there, so
visual-moment detection stays within the VPS budget instead of paying to watch four hours of
footage.
