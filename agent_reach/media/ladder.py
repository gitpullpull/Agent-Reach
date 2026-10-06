# -*- coding: utf-8 -*-
"""Ladder execution — runs the rung it is told to run.

Control flow only. There is no automatic descent, no stopping heuristic, and
no estimate of which interval is worth looking at. The agent has read tier 0
and tier 1 output, knows what it is hunting for, and decides; this module
executes and reports what that decision cost (fork SPEC §5.9).

Every claim is anchored to a timestamp and a deeplink, so the caller can
re-fetch any interval and check it. That is also why frames are described one
at a time rather than in a batch: a batched reply can silently attribute text
from one frame to another's timestamp, and an unverifiable citation is worse
than no citation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import extract, frames as frames_mod
from .frames import Frame
from ..vlm.client import (
    VIDEO_TIMEOUT,
    VLMError,
    image_part,
    request_marker,
    text_part,
    video_part,
)
from ..vlm.roles import ResolvedRole

#: Deliberately mechanical. Prompt design is the agent's territory (fork SPEC
#: §2); this asks only for the two typed fields the output contract defines.
_FRAME_PROMPT = (
    "Transcribe every piece of text visible in this image, exactly as shown. "
    "Then describe the frame in one short phrase. "
    'Reply with JSON only: {"visible_text": ["..."], "scene": "..."}'
)

#: The clip equivalent. A model that reads video can report *when* things
#: happen, so ask for a series rather than one summary — otherwise a whole
#: window collapses into a single citation and the ladder loses the property
#: that makes it checkable.
_CLIP_PROMPT = (
    "This clip is {seconds:.0f} seconds long. Report it as a JSON array, one "
    "entry per distinct moment, in order. Each entry: "
    '{{"t": <seconds from the START of this clip>, "visible_text": ["..."], '
    '"speech": "...", "scene": "..."}}. '
    "visible_text: transcribe on-screen text exactly as shown. "
    "speech: transcribe what is said, empty string if nothing is said. "
    "scene: one short phrase. JSON only."
)


def deeplink(url: str, t: float) -> str:
    """``url`` with a ``t=<seconds>`` query parameter.

    Built by URL manipulation rather than per-site templates: a branch on the
    site name here would be the same design failure as one in the fetch path.
    Sites that use a different time parameter simply get a link to the video.
    """
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "t"]
    query.append(("t", str(int(t))))
    return urlunparse(parts._replace(query=urlencode(query)))


def result(
    *,
    source: Dict[str, Any],
    tier: float,
    backend: Dict[str, Any],
    cost: Dict[str, Any],
    segments: List[Dict[str, Any]],
    warnings: Sequence[str] = (),
) -> Dict[str, Any]:
    """The output contract (fork SPEC §6). Fixed fields, no prose summary."""
    return {
        "source": source,
        "tier": tier,
        "backend": backend,
        "cost": cost,
        "segments": segments,
        "warnings": list(warnings),
    }


def _source(meta: Dict[str, Any], url: str) -> Dict[str, Any]:
    return {
        "extractor": meta.get("extractor"),
        "id": meta.get("id"),
        "url": meta.get("webpage_url") or url,
        "title": meta.get("title"),
        "duration": meta.get("duration"),
    }


def _parse_frame_reply(text: str) -> Dict[str, Any]:
    """Read the model's JSON, tolerating fences and surrounding prose.

    A reply that cannot be parsed is kept verbatim under ``scene`` rather than
    discarded — losing an observation silently would misreport what the frame
    actually cost.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("```")[1] if len(body.split("```")) > 1 else body
        body = body.lstrip("json").strip()
    start, end = body.find("{"), body.rfind("}")
    if start != -1 and end > start:
        try:
            payload = json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            visible = payload.get("visible_text")
            if isinstance(visible, str):
                visible = [visible]
            return {
                "visible_text": [str(v) for v in (visible or [])],
                "scene": str(payload.get("scene") or ""),
            }
    # Unparseable: keep the text so nothing is lost, but mark it, so a reader
    # cannot mistake a failed parse for an observation about the frame.
    return {"visible_text": [], "scene": "", "unparsed": body[:400]}


def _asr_at(cues: Sequence[Dict[str, Any]], t_start: float, t_end: float) -> str:
    """Subtitle text overlapping an interval.

    Kept in its own field: ``asr`` and ``visible_text`` come from different
    sources with different reliability, and merging them would erase the
    distinction the ladder exists to make (fork SPEC §6).
    """
    parts = [
        c["text"]
        for c in cues
        if c["t_end"] > t_start and c["t_start"] < t_end
    ]
    return " ".join(parts).strip()


def describe_frames(
    role: ResolvedRole,
    url: str,
    frames: Sequence[Frame],
    cues: Sequence[Dict[str, Any]],
    prompt: str = _FRAME_PROMPT,
    t_limit: Optional[float] = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """One VLM call per frame, so every observation keeps its own timestamp.

    A frame stands for the span up to the next one; the last frame runs to
    ``t_limit``, the end of the requested window. Without it the final segment
    would claim zero duration and understate what was actually observed.
    """
    segments: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_failures: set = set()
    for i, frame in enumerate(frames):
        if i + 1 < len(frames):
            t_end = frames[i + 1].t
        else:
            t_end = max(frame.t, t_limit) if t_limit is not None else frame.t
        def _describe(binding, frame=frame):
            client = binding.client()
            return client.chat(
                binding.effective_model(client),
                [text_part(f"{prompt} [{request_marker()}]"),
                 image_part(frame.read_bytes(), "image/jpeg")],
                max_tokens=role.max_tokens,
            )

        try:
            # Per frame, not per call: a rate limit that rejects one frame must
            # not cost the rest of the interval.
            reply, failures = role.attempt(_describe)
            parsed = _parse_frame_reply(reply.text)
            if parsed.get("unparsed"):
                warnings.append(
                    f"frame at t={frame.t:.1f}s: reply was not JSON, kept "
                    f"verbatim: {parsed['unparsed'][:100]}"
                )
            for failure in failures:
                if failure.split(":")[0] not in seen_failures:
                    seen_failures.add(failure.split(":")[0])
                    warnings.append(f"fell through to another endpoint: {failure[:160]}")
        except VLMError as exc:
            warnings.append(f"frame at t={frame.t:.1f}s failed: {exc}")
            continue
        segments.append(
            {
                "t_start": round(frame.t, 2),
                "t_end": round(t_end, 2),
                "asr": _asr_at(cues, frame.t, max(t_end, frame.t + 1)),
                "visible_text": parsed["visible_text"],
                "scene": parsed["scene"],
                "deeplink": deeplink(url, frame.t),
            }
        )
    return segments, warnings


def _parse_clip_reply(text: str) -> Optional[List[Dict[str, Any]]]:
    """Read a JSON array of timestamped observations, or None."""
    body = text.strip()
    if body.startswith("```"):
        parts = body.split("```")
        body = (parts[1] if len(parts) > 1 else body).lstrip("json").strip()
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    out = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        visible = entry.get("visible_text")
        if isinstance(visible, str):
            visible = [visible]
        try:
            t = float(entry.get("t"))
        except (TypeError, ValueError):
            continue
        out.append({
            "t": t,
            "visible_text": [str(v) for v in (visible or [])],
            "speech": str(entry.get("speech") or ""),
            "scene": str(entry.get("scene") or ""),
        })
    return out or None


def describe_clip(
    role: ResolvedRole,
    url: str,
    clip: Path,
    t_start: float,
    t_end: float,
    cues: Sequence[Dict[str, Any]],
    prompt: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Send the clip itself, when the endpoint was probed to accept video.

    One request instead of one per frame: cheaper, immune to per-request rate
    limits, and it keeps the temporal continuity that separate frames throw
    away — a model that sees the clip reports transitions a sampled frame set
    misses entirely.

    The timestamps come back from the model. That is a weaker provenance than
    the frame path, where each time is ffmpeg's own ``pts_time``, and the
    difference is stated in ``warnings`` rather than hidden: a deeplink is
    still good enough to go and check, which is what it is for, but it should
    not be read as a measurement.
    """
    seconds = max(0.0, t_end - t_start)
    payload = clip.read_bytes()
    text = (prompt or _CLIP_PROMPT.format(seconds=seconds)) + f" [{request_marker()}]"

    def _describe(binding):
        client = binding.client()
        return client.chat(
            binding.effective_model(client),
            [text_part(text),
             video_part(payload, shape=binding.resolved_video_shape())],
            max_tokens=role.max_tokens,
            timeout=VIDEO_TIMEOUT,
        )

    try:
        reply, failures = role.attempt(_describe)
    except VLMError as exc:
        return [], [
            f"no endpoint could read this clip: {exc}. "
            f"Run `reach-media doctor`, or use --input image."
        ]

    warnings = [f"fell through to another endpoint: {f[:160]}" for f in failures]
    warnings.append(
        "timestamps here are reported by the model, not measured from the "
        "stream as they are with --input image; treat them as approximate "
        "pointers, good enough to go and check"
    )

    observations = _parse_clip_reply(reply.text)
    if observations is None:
        parsed = _parse_frame_reply(reply.text)
        warnings.append(
            "the model did not return a timestamped series, so this is one "
            "segment covering the whole window"
        )
        return (
            [{
                "t_start": round(t_start, 2), "t_end": round(t_end, 2),
                "asr": _asr_at(cues, t_start, t_end),
                "visible_text": parsed["visible_text"], "scene": parsed["scene"],
                "deeplink": deeplink(url, t_start),
            }],
            warnings,
        )

    segments = []
    for i, obs in enumerate(observations):
        # Clip-relative to absolute, clamped: a model that overshoots the clip
        # length must not produce a deeplink outside the interval it was given.
        abs_t = min(max(t_start + obs["t"], t_start), t_end)
        abs_end = (min(t_start + observations[i + 1]["t"], t_end)
                   if i + 1 < len(observations) else t_end)
        # Measured: these models expand a clip into frames and timestamps and
        # do not read audio, so `speech` normally comes back empty. It is still
        # read rather than dropped — an endpoint that does hear should not have
        # its answer discarded — but the subtitle track is what fills `asr`.
        heard = obs.get("speech") or ""
        segments.append({
            "t_start": round(abs_t, 2),
            "t_end": round(max(abs_end, abs_t), 2),
            "asr": heard or _asr_at(cues, abs_t, max(abs_end, abs_t + 1)),
            "visible_text": obs["visible_text"],
            "scene": obs["scene"],
            "deeplink": deeplink(url, abs_t),
        })
    return segments, warnings
