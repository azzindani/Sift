# Skill — `story`

**What it captures:** a short narrative with a payoff. Something happened, and it resolved.

## Procedural prompt

1. **Cold-open test (mandatory).** A story that opens mid-anecdote ("...and *then* he tells me
   the whole thing was fake") strands the viewer. Extend `start` to the beginning of the
   narrative, even if that costs seconds.
2. **Preserve the setup.** This is the one clip type where you spend time on the front. A story
   with a truncated setup has no payoff, only a punchline nobody understands. `story` uses
   `gentle` trimming (only silences over 1.2s are dropped) precisely so the natural pacing of
   an anecdote survives the edit.
3. **The payoff is the exit.** End on the resolution — the reveal, the consequence, the line
   that makes it a story rather than an anecdote. Do not run into the next topic.
4. **Reject stories that don't fit.** A story needing more than 60s to land is not a short-form
   clip. Do not truncate it into incoherence: either find a self-contained beat inside it (and
   label *that* a `quote`), or skip it. A half-told story is worse than no clip.
5. **Length.** 30–60s, and it will use most of that.

## Score guidance

- **9–10** — complete arc, lands inside 60s, needs no outside context.
- **7–8** — good story, tight fit in the budget.
- **5–6** — interesting but the payoff is weak.
- **≤4** — doesn't resolve, or doesn't fit. Skip it.

## Assembly params (consumed by the engine)

```json
{
  "reframe": "speaker",
  "trim_aggressiveness": "gentle",
  "caption_style": "minimal",
  "silence_threshold_db": -32,
  "pad_ms": 200,
  "max_duration_s": 60
}
```
