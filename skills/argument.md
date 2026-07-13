# Skill — `argument`

**What it captures:** a clean disagreement. Two people, two positions, and the moment they
collide.

## Procedural prompt

1. **Cold-open test (mandatory).** Start at the **point of disagreement** — the first line
   where the positions visibly diverge. If that line references a claim made earlier
   ("*that's* just wrong"), extend `start` to include the claim being rejected. A viewer must
   be able to tell *what* is being argued about within the first sentence.
2. **Keep both voices.** An argument clipped to one person's side is not an argument, it is a
   `hot_take`. The clip must contain at least one exchange in each direction. If the other
   party only grunts, relabel it.
3. **Don't cut the interruption.** Overlapping speech is the texture of a real disagreement.
   `medium` trimming (drops silences over 0.7s) is chosen to preserve the rhythm of a
   crosstalk without leaving dead air.
4. **Stop before the de-escalation.** Arguments usually end in "...anyway, moving on". Cut
   before that. End on the last substantive line, not the social repair.
5. **Length.** 25–60s. Below 25s it is rarely a real exchange.

## Reframe — this is the one that uses `stacked`

`argument` defaults to `reframe="stacked"`: two 9:8 crops, one per speaker, stacked into the
9:16 frame — so you never lose the reaction shot of the person being disagreed with, which is
usually where the moment actually lives.

The engine will only produce a stacked layout if MediaPipe finds **two distinct face
clusters** on screen. If the source cuts between single-speaker shots rather than holding a
two-shot, it falls back to speaker-follow and says so in the job progress. That's expected —
don't fight it.

## Score guidance

- **9–10** — a real, substantive collision with both positions legible.
- **7–8** — a clear disagreement, one side dominant.
- **5–6** — mild friction.
- **≤4** — polite hedging. Not an argument. Don't submit it.

## Assembly params (consumed by the engine)

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
