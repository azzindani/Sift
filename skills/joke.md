# Skill — `joke`

**What it captures:** a setup and a punchline. Both are required. A punchline without its
setup is noise; a setup without its punchline is a betrayal.

## Procedural prompt

1. **Cold-open test (mandatory).** The setup must be intelligible with zero prior context.
   A callback to a bit from 40 minutes earlier is *not* clippable no matter how funny it was
   in the room — the viewer wasn't in the room.
2. **Find the punchline first, then walk backwards.** Locate the laugh, then scan back for the
   earliest line the joke actually needs. That is your `start`. Everything before it is fat.
3. **Cut in tight, land hard, out fast.** End 0.5–1.5s *after* the punchline lands — enough to
   let the laugh begin, not enough to sit in dead air. Do not include the speaker explaining
   the joke afterwards. Explanation kills it.
4. **The laugh is the proof.** If you can, confirm the beat with `sample_frames` + the audio
   spike cue. Two cues agreeing (audio spike + a `[laughter]` transcript marker) is real. One
   cue alone is a mic bump.
5. **Length.** 10–40s. Jokes are the shortest clip type; if it takes 50s it is a `story`.

## Score guidance

- **9–10** — the laugh is audible and the setup fits in the clip.
- **7–8** — genuinely funny, but the delivery carries more than the words.
- **5–6** — a chuckle. Only if the source is thin.
- **≤4** — in-joke, callback, or "you had to be there". Don't submit it.

## Assembly params (consumed by the engine)

```json
{
  "reframe": "speaker",
  "trim_aggressiveness": "very_tight",
  "caption_style": "punchline",
  "silence_threshold_db": -30,
  "pad_ms": 120,
  "max_duration_s": 45
}
```

`very_tight` trimming drops any silence over 0.35s. That is deliberate: comic timing dies in
dead air. It also means a *deliberate* comedic pause longer than 0.35s will be cut — if the
pause **is** the joke, submit it as a `reaction` instead, which trims gently around the beat.
