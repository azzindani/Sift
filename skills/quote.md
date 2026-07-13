# Skill — `quote`

**What it captures:** one sharp, self-contained line that lands on its own. Not a good
paragraph; a good *sentence*.

## Procedural prompt

1. **Cold-open test (mandatory).** Read the first sentence as if you know nothing else. If
   it opens on "that", "he", "it", "which" — anything pointing at something the viewer never
   saw — extend `start` backwards to include the antecedent. The 2-minute chunk overlap is
   there so you can find it.
2. **One idea.** A quote is a single claim, not a chain. If the speaker keeps qualifying
   ("...but also...", "...although..."), cut before the qualification or pick a different
   moment. A quote that needs a caveat is not a quote.
3. **Cut in late, out early.** Start on the first word of the sentence that carries the idea,
   not the throat-clear before it ("So, um, I think what I'd say is..."). End on the last word
   of the payoff. Silence trimming will tighten the pauses; it will not fix a bad boundary.
4. **Self-containment check.** Would this line make sense to someone scrolling who has never
   heard of the speaker or the topic? If it needs the previous 30 seconds, it isn't a quote —
   consider `story` or `hot_take` instead.
5. **Length.** 10–45s. If your span is over 45s you have selected an argument or a story.

## Score guidance

- **9–10** — quotable verbatim, would survive as a text post with no video.
- **7–8** — sharp and self-contained, but needs the delivery to land.
- **5–6** — interesting, not memorable. Only worth clipping if the source is thin.
- **≤4** — don't submit it.

## Assembly params (consumed by the engine)

```json
{
  "reframe": "speaker",
  "trim_aggressiveness": "tight",
  "caption_style": "key_phrase",
  "silence_threshold_db": -30,
  "pad_ms": 150,
  "max_duration_s": 60
}
```
