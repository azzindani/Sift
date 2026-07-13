# Skill — `hot_take`

**What it captures:** a controversial claim, stated plainly, that a stranger would argue with.

## Procedural prompt

1. **Cold-open test (mandatory).** The claim must stand without setup. If it only reads as
   controversial *given* the previous ten minutes, it is not a hot take — it is a conclusion,
   and conclusions don't travel.
2. **Lead with the claim.** Start on the sentence that makes the assertion, not the wind-up.
   "Here's the thing, and I've said this before, and people hate it, but —" is all fat. Cut to
   the claim.
3. **Include the *minimum* justification.** A bare provocation reads as a cheap dunk. One or
   two sentences of reasoning after the claim makes it defensible. Do not include the full
   argument — that's a `story` or an `argument`.
4. **Do not editorialize in `reason`.** Your `reason` field is a note to the human reviewer
   about *why this clips well*, not whether you agree. "Contradicts the mainstream view on X,
   stated flatly, no hedging" — that's a reason. "This is wrong" is not.
5. **Length.** 15–50s.

## A note on judgement

You are selecting for *clippability*, not endorsing. But clippability and inflammatory are not
the same thing: a claim that is merely offensive, with no argument behind it, is a bad clip —
it invites a pile-on rather than a watch. Prefer takes that are **surprising and defended**
over takes that are **shocking and bare**. If the only reason a line travels is that it will
make people angry, score it low.

## Score guidance

- **9–10** — surprising, plainly stated, briefly defended.
- **7–8** — a real position most listeners would push back on.
- **5–6** — mildly contrarian.
- **≤4** — consensus opinion, or bare provocation with no reasoning. Don't submit it.

## Assembly params (consumed by the engine)

```json
{
  "reframe": "speaker",
  "trim_aggressiveness": "tight",
  "caption_style": "key_phrase",
  "silence_threshold_db": -30,
  "pad_ms": 150,
  "max_duration_s": 50
}
```
