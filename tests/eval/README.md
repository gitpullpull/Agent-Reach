# Ladder evaluation

## Status: harness complete, dataset not populated

`run_eval.py` works and is exercised by the one entry in `dataset.jsonl`. That
entry is a **smoke test**, not a benchmark item — it proves the harness runs
end to end against a real video.

The 12 items the fork spec calls for (4 types × 3 videos) are **not** included.
Populating them requires watching videos and confirming, by eye, both the
answer and the tier at which it first becomes available. Inventing plausible
video IDs and answers would produce a green report that measures nothing, and
a regression suite that cannot fail is worse than an empty one.

## Adding an item

One JSON object per line:

```json
{"video": "<youtube_id>", "at": 412, "window": 30,
 "q": "What is the p99 latency?", "answer": "48ms",
 "answer_any": ["48ms", "48 ms"],
 "required_tier": 2, "type": "slide"}
```

| field | meaning |
|---|---|
| `video` | id, or use `url` for non-YouTube sources |
| `at` / `window` | interval for tier 2. Required when `required_tier >= 2` |
| `answer` | ground truth, matched as a normalised substring |
| `answer_any` | accepted variants (spacing, units, synonyms) |
| `required_tier` | shallowest tier at which you confirmed it is answerable |
| `type` | `slide` \| `terminal` \| `ui` \| `talk` |
| `method` | optional: `interval` for material scene detection cannot cut |
| `interval_sec`, `max_frames`, `scene_threshold`, `role` | optional, and worth pinning |

**Pin the sampling parameters.** They decide which instants are looked at, so
an item that leaves them to defaults is not reproducible. The seed item is a
live demonstration: at `interval_sec: 4` the answer is found, and at the
default `5` the frame containing it is never sampled.

Verify `required_tier` by hand before adding: read the tier-1 subtitles and
confirm the answer really is absent from them. A mislabelled item makes the
report meaningless in the direction that matters — it would show the ladder
passing while never testing a descent.

Videos are not redistributed; only ids, questions, answers and tiers.

## Running

```bash
python tests/eval/run_eval.py --dry-run     # how many downloads this costs
python tests/eval/run_eval.py --tiers 1,2   # skip the tier 0.5 sieve
python tests/eval/run_eval.py --type slide
```

Each tier-2 item spends one download from the daily budget (default 30), so
check `reach-media doctor` before a full run.

## What the numbers mean

`run_eval.py` reports **reachability**: the shallowest tier at which the
answer is actually present in the CLI output.

- `reachable_deeper_than_required` — a real regression. Frame selection, OCR
  quality or prompt behaviour got worse and a tier that used to answer no
  longer does.
- `reachable_shallower_than_required` — a mislabelled item, not a fork bug.
- `glance_recall` — how often the tier-0.5 thumbnail sieve would have kept a
  video that genuinely needed tier 2.

`under_descent` and `over_descent` are reported as `null`. They describe an
**agent's** descent decisions, and this harness has no agent in the loop — it
walks every tier unconditionally. Scoring them requires a transcript from an
agent given only `references/media-ladder.md`.

Per the fork spec: when under-descent is eventually measured and comes out
bad, the fix belongs in the skill reference or in what the CLI reports — not
in heuristics added to the fork. `read` telling an agent "subtitles are silent
for N seconds here" is a correct response; the fork guessing "look here" is
not.
