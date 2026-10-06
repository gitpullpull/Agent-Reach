# -*- coding: utf-8 -*-
"""Frame extraction primitives — mechanical, never clever.

This module turns a clip into images using exactly the parameters it was
given. It holds no judgement of its own, by design (fork SPEC §5.6). It must
never grow:

  - classification of a video as slides / terminal / UI / talk
  - "scene detection is not working here, switch to fixed interval"
  - inference of interesting intervals from subtitle gaps or any other signal
  - any opinion about which part of a video is worth looking at

Those are the agent's decisions. It has already seen tier 0 and tier 1 output
and knows what it is looking for; this module knows neither. Upstream refuses
to read on the agent's behalf, and the same boundary applies one level down:
this fork refuses to *choose* on the agent's behalf. What it owes the agent
instead is an honest count of what the choice cost.

Scene detection genuinely fails on some material — a terminal recording
changes too few pixels to trip any threshold — which is exactly why both
methods are exposed as flags rather than resolved internally.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: showinfo writes one line per frame that survives the filter chain; the
#: Nth line corresponds to the Nth image ffmpeg wrote.
_PTS_RE = re.compile(r"pts_time:([0-9]+\.?[0-9]*)")

DEFAULTS: Dict[str, Any] = {
    "method": "scene",
    "scene_threshold": 0.3,
    "interval_sec": 5,
    "phash_distance": 8,
    "max_frames": 32,
}


class FrameError(RuntimeError):
    """Extraction could not run. Distinct from "ran and found nothing"."""


@dataclass
class Frame:
    """One extracted image and the source timestamp it came from."""

    path: Path
    t: float          # seconds into the original video, not into the clip
    index: int

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


def extraction_settings(config: Any = None, **overrides: Any) -> Dict[str, Any]:
    """Merge defaults, config, then CLI flags — flags always win.

    Defaults live in config so an operator can retune without touching code;
    overrides come from the command line so an agent can retune per call.
    """
    settings = dict(DEFAULTS)
    data = getattr(config, "data", None) if config is not None else None
    if isinstance(data, dict):
        configured = (data.get("media") or {}).get("extraction") or {}
        if isinstance(configured, dict):
            settings.update(
                {k: v for k, v in configured.items() if k in DEFAULTS}
            )
    settings.update({k: v for k, v in overrides.items() if v is not None})
    return settings


def _run_ffmpeg(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(
            ["ffmpeg", *args], capture_output=True, encoding="utf-8",
            errors="replace", timeout=1800,
        )
    except FileNotFoundError as exc:
        raise FrameError(
            "ffmpeg not found. Install it (the sandbox image already has it): "
            "apt-get install ffmpeg"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FrameError("ffmpeg timed out after 30 minutes") from exc
    if proc.returncode != 0:
        raise FrameError(f"ffmpeg failed: {(proc.stderr or '')[-500:]}")
    return proc.stderr or ""


def extract(
    video: Path,
    out_dir: Path,
    *,
    method: str = "scene",
    scene_threshold: float = 0.3,
    interval_sec: float = 5,
    t_offset: float = 0.0,
) -> List[Frame]:
    """Extract frames by one named method. No fallback between methods.

    If ``scene`` returns nothing, that is a real answer about the material and
    is reported as such — silently retrying with ``interval`` would hide the
    fact that the agent's parameter choice was wrong for this video.

    ``t_offset`` maps clip-relative timestamps back onto the original video,
    so a tier-2 result taken from a 30-second window still cites real
    timestamps and produces working deeplinks.
    """
    if method == "scene":
        vf = f"select='gt(scene,{scene_threshold})',showinfo"
    elif method == "interval":
        vf = f"fps=1/{interval_sec},showinfo"
    else:
        raise FrameError(f"unknown extraction method '{method}' (scene|interval)")

    out_dir.mkdir(parents=True, exist_ok=True)
    stderr = _run_ffmpeg([
        "-v", "info", "-y",
        "-i", str(video),
        "-vf", vf,
        "-vsync", "vfr",
        "-q:v", "3",
        str(out_dir / "%06d.jpg"),
    ])

    times = [float(m) for m in _PTS_RE.findall(stderr)]
    images = sorted(out_dir.glob("*.jpg"))

    # Pair by position, and keep only frames whose time ffmpeg actually
    # reported. A frame with a computed timestamp is worse than a missing one:
    # its deeplink points somewhere the reader will not find what was claimed,
    # and the whole contract here is that every claim can be re-fetched and
    # checked. If showinfo emitted fewer lines than files, that is a fact to
    # surface, not a gap to fill in.
    frames = [
        Frame(path=image, t=times[i] + t_offset, index=i)
        for i, image in enumerate(images)
        if i < len(times)
    ]
    if len(images) > len(times):
        raise FrameError(
            f"ffmpeg wrote {len(images)} frames but reported only "
            f"{len(times)} timestamps; refusing to invent the rest"
        )
    return frames


def dedupe(frames: Sequence[Frame], phash_distance: int = 8) -> List[Frame]:
    """Collapse runs of near-identical frames, keeping the first of each run.

    A held slide or a static terminal produces many identical frames; sending
    all of them costs the same as sending information. Comparison is against
    the last *kept* frame, so slow drift is still followed rather than
    collapsed into a single image.

    Without pillow/imagehash installed this returns the input unchanged: the
    ladder still works, it just costs more frames.
    """
    if phash_distance <= 0 or len(frames) < 2:
        return list(frames)
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return list(frames)

    kept: List[Frame] = []
    last_hash = None
    for frame in frames:
        try:
            with Image.open(frame.path) as img:
                current = imagehash.phash(img)
        except (OSError, ValueError):
            kept.append(frame)
            last_hash = None
            continue
        if last_hash is not None and (current - last_hash) < phash_distance:
            continue
        kept.append(frame)
        last_hash = current
    return kept


def cap(frames: Sequence[Frame], max_frames: int) -> List[Frame]:
    """Truncate to a budget, spread evenly across the clip.

    Taking the first N would bias every answer toward the start of the
    interval, so the sample is thinned instead. Truncation is reported through
    ``cost.frames_sent`` — the agent can widen the budget and re-run.
    """
    if max_frames <= 0 or len(frames) <= max_frames:
        return list(frames)
    step = len(frames) / max_frames
    return [frames[int(i * step)] for i in range(max_frames)]


def select(
    video: Path,
    out_dir: Path,
    settings: Dict[str, Any],
    t_offset: float = 0.0,
) -> List[Frame]:
    """extract → dedupe → cap, using one settings dict. Still no judgement."""
    frames = extract(
        video,
        out_dir,
        method=str(settings.get("method", "scene")),
        scene_threshold=float(settings.get("scene_threshold", 0.3)),
        interval_sec=float(settings.get("interval_sec", 5)),
        t_offset=t_offset,
    )
    frames = dedupe(frames, int(settings.get("phash_distance", 8)))
    return cap(frames, int(settings.get("max_frames", 32)))
