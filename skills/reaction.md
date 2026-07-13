# Skill — `reaction`

**What it captures:** laughter, a facial reaction, a beat that lives in the video rather than
the words. This is the **only cue-first label** — every other label is text-first.

## The ordering rule is different here

Text moments go `read chunk → label → skill`. A reaction has little or no text, so the
transcript alone will never surface it. The order is:

```
cheap cue flags a region → vision confirms something is there → label → skill
```

## Procedural prompt

1. **Find the cue, don't guess.** Two cheap cues must agree before you spend a frame:
   - a **transcript marker** — `[laughter]`, `[applause]`, `[crosstalk]`
   - an **audio energy spike** — a sustained burst above the span's speech baseline

   `sample_frames(source_id, start, end)` returns both, plus `two_cue_agreement`. **One cue
   alone is a mic bump or a music sting.** This rule exists because it kills false positives
   for free, before you pay for vision.
2. **Then confirm with vision.** Look at the returned frames. Is something visibly happening —
   a real laugh, a face doing something? If the frames show nothing, the moment isn't there.
   Do not submit it on the strength of the audio alone.
3. **Respect the frame budget.** The vision budget is capped per source (see `budget_total` in
   the response) and it is capped on purpose: the whole point of the funnel is that free cues
   decide *where to look* so vision is never paid for by the hour. Spend it on flagged spans
   only.
4. **Cut tight around the beat.** Start ~2s before the trigger (the viewer needs to see what
   caused it), end when the laugh subsides. `pad_ms` is 250 here — wider than other labels —
   because clipping the front of a laugh sounds broken.
5. **Length.** 8–30s. Reactions are short.
6. **Mark your cues honestly.** Set
   `cues: {"text": false, "audio_spike": true, "vision_confirmed": true}` to record what
   actually fired. This is the audit trail for a pick that no transcript can justify.

## The honest constraint

Label quality is bounded by signal. "Funny" is subjective enough that no model scores it
perfectly — commercial tools struggle here too. Our edge is not a better funniness detector;
it is the **cheap-signal-then-expensive-verify funnel**, which keeps visual-moment detection
inside the VPS budget instead of paying to watch four hours of footage. Score conservatively.

## Score guidance

- **9–10** — both cues fired, vision confirms it, and it reads without the words.
- **7–8** — clear reaction, needs a second of context to land.
- **5–6** — something happened, but it's mild.
- **≤4** — one cue only, or vision showed nothing. Don't submit it.

## Assembly params (consumed by the engine)

```json
{
  "reframe": "speaker",
  "trim_aggressiveness": "tight",
  "caption_style": "sparse",
  "silence_threshold_db": -28,
  "pad_ms": 250,
  "max_duration_s": 30
}
```
