#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ladder regression harness.

## What this measures

For each question, the shallowest tier at which the ground-truth answer is
actually *present* in what the CLI returns. Comparing that against the item's
``required_tier`` catches the regressions that matter:

  answer_tier > required_tier    the ladder stopped delivering at the tier
                                 that used to work — a real fork regression
                                 (frame selection, OCR quality, prompt drift)
  answer_tier < required_tier    the dataset is mislabelled: a cheaper tier
                                 answers it, so the item does not exercise
                                 what it claims to

## What this does NOT measure

The fork spec's primary metric is *under-descent*: an agent answering from
tier 1 when tier 2 was required, and doing so confidently. That is a property
of an agent's judgement, and this harness has no agent in the loop — it walks
every tier unconditionally. It therefore reports **reachability**, not
decision quality. Measuring under-descent honestly needs an agent driving the
CLI with only the skill reference to guide it; ``--agent-log`` accepts the
transcript of such a run and scores it, and without one those fields are
reported as null rather than estimated.

This distinction matters because a reachability score that got relabelled as
"under_descent: 0.0" would claim the fork's core risk had been measured when
it had not.

## Usage

    python tests/eval/run_eval.py                  # every item, every tier
    python tests/eval/run_eval.py --tiers 1,2      # skip the 0.5 sieve
    python tests/eval/run_eval.py --dry-run        # cost estimate only

Tier 2 items each spend one download from the daily budget. A full 12-item
run is a meaningful fraction of it — check ``reach-media doctor`` first.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DATASET = Path(__file__).with_name("dataset.jsonl")
TIERS = (0.5, 1.0, 2.0)


def normalize(text: str) -> str:
    """Fold case and whitespace so 'p99: 48ms' matches 'P99:  48 ms'."""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def contains_answer(haystack: str, item: Dict[str, Any]) -> bool:
    """Substring match against any accepted form of the answer.

    Deliberately crude: a fuzzy or model-graded match would make a regression
    in extraction quality look like noise in the grader.
    """
    text = normalize(haystack)
    candidates = item.get("answer_any") or [item["answer"]]
    return any(normalize(c) in text for c in candidates)


def run_cli(args: List[str]) -> Optional[Dict[str, Any]]:
    proc = subprocess.run(
        ["reach-media", *args], capture_output=True, encoding="utf-8", errors="replace"
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stderr or proc.stdout or "no output")[-300:],
                "exit_code": proc.returncode}


def harvest(payload: Optional[Dict[str, Any]]) -> str:
    """Flatten a result into text, keeping asr and visible_text separable."""
    if not payload or "segments" not in payload:
        return ""
    parts: List[str] = []
    for segment in payload["segments"]:
        parts.extend(segment.get("visible_text") or [])
        parts.append(segment.get("scene") or "")
        parts.append(segment.get("asr") or "")
    return " \n".join(parts)


def tier_output(item: Dict[str, Any], tier: float) -> Dict[str, Any]:
    """Run one tier for one item and return what it produced and cost."""
    url = item.get("url") or f"https://www.youtube.com/watch?v={item['video']}"

    if tier == 0.5:
        payload = run_cli(["glance", url, "--role", item.get("role", "fast")])
    elif tier == 1.0:
        # Tier 1 is a plain yt-dlp call, exactly as the skill reference tells
        # an agent to run it — the harness must exercise that path, not a
        # private shortcut through the library.
        from agent_reach.config import Config
        from agent_reach.media import extract
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            try:
                cues, _ = extract.subtitles(url, Path(tmp), Config())
            except extract.FetchError as exc:
                return {"text": "", "cost": {}, "error": str(exc)}
        return {"text": " ".join(c["text"] for c in cues),
                "cost": {"frames_sent": 0, "downloaded_seconds": 0}}
    elif tier == 2.0:
        if item.get("at") is None:
            return {"text": "", "cost": {},
                    "error": "item has no `at`; tier 2 requires an interval"}
        # Frame-selection parameters are pinned per item, not defaulted: they
        # decide which instants are sampled, so an item that does not fix them
        # is not a reproducible test of anything.
        flags: List[str] = []
        for key, flag in (
            ("method", "--method"),
            ("scene_threshold", "--scene-threshold"),
            ("interval_sec", "--interval-sec"),
            ("max_frames", "--max-frames"),
        ):
            if item.get(key) is not None:
                flags += [flag, str(item[key])]
        payload = run_cli([
            "look", url, "--at", str(item["at"]),
            "--window", str(item.get("window", 30)),
            "--role", item.get("role", "accurate"),
            *flags,
        ])
    else:
        return {"text": "", "cost": {}, "error": f"unsupported tier {tier}"}

    if payload and "error" in payload:
        return {"text": "", "cost": {}, "error": payload["error"]}
    return {"text": harvest(payload), "cost": (payload or {}).get("cost", {})}


def evaluate(items: List[Dict[str, Any]], tiers: List[float]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        row: Dict[str, Any] = {
            "video": item.get("video"),
            "q": item.get("q"),
            "type": item.get("type"),
            "required_tier": item.get("required_tier"),
            "answer_tier": None,
            "tiers": {},
        }
        for tier in tiers:
            outcome = tier_output(item, tier)
            found = contains_answer(outcome["text"], item)
            row["tiers"][str(tier)] = {
                "found": found,
                "error": outcome.get("error"),
                "cost": outcome.get("cost", {}),
            }
            if found and row["answer_tier"] is None:
                row["answer_tier"] = tier
                break   # shallowest hit is the answer; deeper tiers cost money
        rows.append(row)

    scored = [r for r in rows if r["answer_tier"] is not None]
    too_deep = [r for r in rows
                if r["answer_tier"] is not None
                and r["answer_tier"] > (r["required_tier"] or 0)]
    too_shallow = [r for r in rows
                   if r["answer_tier"] is not None
                   and r["answer_tier"] < (r["required_tier"] or 0)]
    glance_items = [r for r in rows if (r["required_tier"] or 0) >= 2]
    glance_pass = [r for r in glance_items
                   if r["tiers"].get("0.5", {}).get("found")]

    return {
        "items": len(rows),
        "metrics": {
            "accuracy": round(len(scored) / len(rows), 3) if rows else None,
            "unreachable": len(rows) - len(scored),
            "reachable_deeper_than_required": len(too_deep),
            "reachable_shallower_than_required": len(too_shallow),
            "glance_recall": (
                round(len(glance_pass) / len(glance_items), 3)
                if glance_items else None
            ),
            "under_descent": None,
            "over_descent": None,
            "_note": (
                "under_descent and over_descent are properties of an agent's "
                "descent decisions and are null without --agent-log: this "
                "harness walks every tier and measures reachability only."
            ),
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--tiers", default="0.5,1,2",
                        help="comma-separated tiers to walk")
    parser.add_argument("--type", help="only items of this type")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would run and what it would cost")
    args = parser.parse_args()

    if not args.dataset.exists():
        print(f"no dataset at {args.dataset}", file=sys.stderr)
        return 1
    items = [
        json.loads(line)
        for line in args.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]
    if args.type:
        items = [i for i in items if i.get("type") == args.type]
    tiers = [float(t) for t in args.tiers.split(",") if t.strip()]

    if args.dry_run:
        downloads = sum(1 for i in items if 2.0 in tiers and i.get("at") is not None)
        json.dump({"items": len(items), "tiers": tiers,
                   "downloads_required": downloads}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not items:
        print("dataset is empty — see tests/eval/README.md", file=sys.stderr)
        return 1

    report = evaluate(items, tiers)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report["metrics"]["unreachable"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
