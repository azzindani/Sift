# Skills — the procedural half of a label

A **label** is a routing key. It pulls a **skill**: a procedural prompt (for you, the
agent) plus a set of deterministic `assembly_params` (for the engine).

```
label ──► skill { procedural_prompt, assembly_params } ──► shapes how the clip is built
```

**The engine never reads these files.** It consumes only the `assembly_params`, which live
in `LABEL_REGISTRY` in `_clip_helpers.py` and are looked up by label. These files exist for
*you* — read `skills/<label>.md` before choosing boundaries for a candidate of that label.

## The cold-open test — in every skill, non-negotiable

A clip's first sentence must be intelligible with **zero prior context**. If it opens on a
dangling pronoun or an unexplained reference ("*that's* exactly why *he* did it"), extend
`start` backwards until it isn't. The 2-minute look-back overlap on every transcript chunk
exists precisely so you can see the antecedent.

`add_candidates` soft-flags a candidate whose first word is a bare pronoun, but it cannot
catch a reference that is semantically dangling while syntactically fine. That judgement is
yours — the engine does not read for meaning.

## Adding a new clip type

1. Add a row to `LABEL_REGISTRY` in `_clip_helpers.py` (the label + its `assembly_params`).
2. Write `skills/<label>.md` — procedural prompt, using the ones here as the template.
3. Done. No engine change, no new tool.

## The registry

| label | captures | detection | reframe | trim | caption emphasis |
|---|---|---|---|---|---|
| [`quote`](quote.md) | a sharp, self-contained line | text | speaker | tight | key phrase |
| [`joke`](joke.md) | setup + punchline | text | speaker | very tight | land on punchline |
| [`story`](story.md) | a short narrative with a payoff | text | speaker | gentle | minimal |
| [`argument`](argument.md) | a clean disagreement | text | stacked | medium | both speakers |
| [`hot_take`](hot_take.md) | a controversial claim | text | speaker | tight | claim emphasized |
| [`reaction`](reaction.md) | laughter / facial reaction | cue → vision | speaker | tight | sparse |
