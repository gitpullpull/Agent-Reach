# -*- coding: utf-8 -*-
"""Real capability probing for VLM endpoints.

Upstream's rule for fetch backends is that ``shutil.which()` is not proof of
health, so channels really execute the command (``agent_reach.probe``). The
same rule applies here, and the failure it guards against is worse: a
text-only model answers ``/chat/completions`` perfectly and silently drops the
image. The endpoint is up, the model replies, and every answer about the
picture is invented.

So vision is never inferred from a model name. It is established by sending a
solid-colour PNG and checking the colour comes back. The colour is chosen at
random per probe, so a text-only model guessing a common colour does not pass
by luck.

Video input is a separate, optional capability probed the same way.
"""

from __future__ import annotations

import random
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import observed
from .client import (
    DEFAULT_MAX_TOKENS,
    request_marker,
    VIDEO_SHAPE_ORDER,
    VIDEO_TIMEOUT,
    VLMClient,
    VLMError,
    image_part,
    text_part,
    video_part,
)

#: Colours distinctive enough that a wrong answer is unambiguous, with the
#: synonyms a model may reasonably return.
_PROBE_COLORS: Dict[str, Tuple[Tuple[int, int, int], Tuple[str, ...]]] = {
    "red": ((220, 20, 20), ("red", "crimson", "赤", "红")),
    "green": ((20, 180, 60), ("green", "緑", "绿")),
    "blue": ((30, 60, 220), ("blue", "青", "蓝")),
    "yellow": ((240, 220, 40), ("yellow", "黄")),
    "magenta": ((220, 40, 200), ("magenta", "pink", "purple", "紫", "粉")),
    "orange": ((240, 130, 20), ("orange", "橙", "オレンジ")),
}

_PROBE_PROMPT = "What color is this image? Answer with one word."

#: Generous, because reasoning models bill their chain-of-thought to the same
#: completion budget. Too small a budget empties `content` on a healthy model
#: and would be misread as "cannot see" — the exact misdiagnosis this fork
#: exists to prevent.
_PROBE_MAX_TOKENS = DEFAULT_MAX_TOKENS


@dataclass
class VLMProbeResult:
    """Outcome of one probe, in upstream ``ProbeResult`` shape.

    status: "ok" | "unreachable" | "no_vision" | "unsupported" | "error"
    """

    status: str
    output: str = ""
    hint: str = ""
    detail: Dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ── test fixtures, built without pillow so probing has no image dependency ──


def solid_png(rgb: Tuple[int, int, int], size: int = 32) -> bytes:
    """A ``size``x``size`` solid-colour PNG, stdlib only."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolour
    row = b"\x00" + bytes(rgb) * size  # filter byte 0 + RGB pixels
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size, 9))
        + chunk(b"IEND", b"")
    )


def tone_mp4(rgb: Tuple[int, int, int], seconds: int = 3,
             hz: Optional[int] = 440) -> Optional[bytes]:
    """A short clip with, or deliberately without, an audio track.

    ``hz=None`` produces silence. The pair is what makes the audio probe
    meaningful: a model asked "is there sound?" about a single clip can be
    right by guessing, so both are sent and both answers must be correct.
    """
    color = "0x%02x%02x%02x" % rgb
    with tempfile.TemporaryDirectory(prefix="reach-audio-probe-") as tmp:
        out = Path(tmp) / "probe.mp4"
        audio = (f"sine=frequency={hz}:duration={seconds}" if hz
                 else f"anullsrc=r=44100:cl=mono:d={seconds}")
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-f", "lavfi", "-i", f"color=c={color}:s=64x64:d={seconds}:r=4",
                 "-f", "lavfi", "-i", audio,
                 "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 str(out)],
                capture_output=True, timeout=90,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not out.exists():
            return None
        return out.read_bytes()


def probe_audio(
    client: VLMClient, model: str, shape: Optional[str] = None
) -> VLMProbeResult:
    """Does this endpoint's video path carry the audio track?

    It matters because it decides where speech comes from. A model that reads
    a clip as frames plus timestamps — which is what llama.cpp's video support
    does — never hears anything, so ``asr`` has to come from the platform's
    subtitle track instead. One that does process audio makes that extra
    request unnecessary.

    Never inferred from the provider: it is a property of how the endpoint
    expands a clip, and two endpoints serving the same weights can differ.
    """
    loud = tone_mp4((20, 20, 20), hz=440)
    quiet = tone_mp4((20, 20, 20), hz=None)
    if loud is None or quiet is None:
        return VLMProbeResult(
            "unsupported", output="ffmpeg unavailable, audio probe skipped")

    question = ("Does this clip contain any audible sound? "
                "Answer with exactly one word: yes or no.")
    answers = {}
    for label, payload in (("with_tone", loud), ("silent", quiet)):
        try:
            reply = client.chat(
                model,
                [text_part(f"{question} [{request_marker()}]"),
                 video_part(payload, shape=shape)],
                max_tokens=_PROBE_MAX_TOKENS, timeout=VIDEO_TIMEOUT,
            )
        except VLMError as exc:
            return VLMProbeResult(
                "unsupported", output=f"{label}: {str(exc)[:150]}",
                hint="Speech must come from subtitles for this endpoint.")
        answers[label] = reply.text.strip().lower()

    heard = answers["with_tone"].startswith("y") and answers["silent"].startswith("n")
    if heard:
        return VLMProbeResult("ok", output="distinguished sound from silence",
                              detail=answers)
    return VLMProbeResult(
        "unsupported",
        output=f"tone->{answers['with_tone'][:20]!r} silent->{answers['silent'][:20]!r}",
        hint=("This endpoint reads frames, not audio. Speech must come from "
              "the subtitle track (tier 1 / --with-subtitles)."),
        detail=answers,
    )


def solid_mp4(rgb: Tuple[int, int, int], seconds: int = 1) -> Optional[bytes]:
    """A tiny solid-colour mp4, or None when ffmpeg is unavailable.

    Only used to find out whether the endpoint accepts video at all, so the
    content just has to be a valid, decodable file.
    """
    color = "0x%02x%02x%02x" % rgb
    with tempfile.TemporaryDirectory(prefix="reach-vlm-probe-") as tmp:
        out = Path(tmp) / "probe.mp4"
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi",
                    "-i", f"color=c={color}:s=64x64:d={seconds}:r=4",
                    "-pix_fmt", "yuv420p",
                    str(out),
                ],
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not out.exists():
            return None
        return out.read_bytes()


# ── probes ──────────────────────────────────────────────────────────────


def probe_reachable(client: VLMClient) -> VLMProbeResult:
    """Step 1 — is the endpoint there, and what does it advertise?

    Only worth calling when the model has to be discovered. When config names
    one, the catalogue is a request that answers nothing: a hosted endpoint
    listing fifty-eight models tells you nothing about the one you asked for,
    and on a metered tier it is quota spent on curiosity.
    """
    try:
        models = client.list_models()
    except VLMError as exc:
        return VLMProbeResult(
            "unreachable",
            output=str(exc),
            hint=(
                f"Check vlm.endpoints.*.base_url and that the token in "
                f"api_key_env is valid for {client.base_url}."
            ),
        )
    return VLMProbeResult("ok", output=f"{len(models)} models", detail={"models": models})


def probe_vision(
    client: VLMClient, model: str, color: Optional[str] = None
) -> VLMProbeResult:
    """Step 2 — does this model actually see images?

    A model that answers with the wrong colour is reported ``no_vision``, not
    ``error``: it is working correctly, it just cannot see, and using it for
    the ladder would produce confident fiction.
    """
    name = color or random.choice(list(_PROBE_COLORS))
    rgb, accepted = _PROBE_COLORS[name]
    try:
        result = client.chat(
            model,
            [text_part(f"{_PROBE_PROMPT} [{request_marker()}]"),
             image_part(solid_png(rgb))],
            max_tokens=_PROBE_MAX_TOKENS,
            timeout=180,
        )
    except VLMError as exc:
        return VLMProbeResult(
            "error",
            output=str(exc),
            hint=f"Model '{model}' rejected an image payload.",
            detail={"expected": name},
        )

    answer = result.text.strip()
    if any(word in answer.lower() for word in accepted):
        return VLMProbeResult(
            "ok", output=f"saw {name}", detail={"expected": name, "answer": answer}
        )
    return VLMProbeResult(
        "no_vision",
        output=f"expected '{name}', got: {answer[:120]}",
        hint=(
            f"Model '{model}' answers text but does not see the image. Point "
            f"the role at a vision-capable model in vlm.roles.*.model."
        ),
        detail={"expected": name, "answer": answer},
    )


def probe_video(
    client: VLMClient,
    model: str,
    color: Optional[str] = None,
    shapes: Optional[Sequence[str]] = None,
) -> VLMProbeResult:
    """Step 3 — optional. Which video shape, if any, does this endpoint take?

    Tries the candidate shapes in order and reports the first that both parses
    and reads the clip. Failure is not breakage: frames are the ladder's
    baseline and video is an optimisation, so this reports ``unsupported`` and
    lets the caller record the capability as absent.
    """
    name = color or random.choice(list(_PROBE_COLORS))
    rgb, accepted = _PROBE_COLORS[name]
    payload = solid_mp4(rgb)
    if payload is None:
        return VLMProbeResult(
            "unsupported",
            output="ffmpeg unavailable, video probe skipped",
            hint="Install ffmpeg to probe video input.",
        )

    attempts: Dict[str, str] = {}
    for shape in (shapes or VIDEO_SHAPE_ORDER):
        try:
            result = client.chat(
                model,
                [text_part(f"{_PROBE_PROMPT} [{request_marker()}]"),
                 video_part(payload, shape=shape)],
                max_tokens=_PROBE_MAX_TOKENS,
                timeout=VIDEO_TIMEOUT,
            )
        except VLMError as exc:
            attempts[shape] = str(exc)[:120]
            continue
        answer = result.text.strip()
        if any(word in answer.lower() for word in accepted):
            return VLMProbeResult(
                "ok",
                output=f"saw {name} via {shape}",
                detail={"expected": name, "answer": answer, "shape": shape,
                        "rejected": attempts},
            )
        # Accepted the payload but did not read it — a wrong answer here is
        # worse than a rejection, so do not settle on this shape.
        attempts[shape] = f"answered '{answer[:60]}', expected {name}"

    return VLMProbeResult(
        "unsupported",
        output="; ".join(f"{k}: {v}" for k, v in attempts.items())[:300],
        hint=(
            "No video shape worked on this endpoint; send frames instead "
            "(--input image). Add a shape to VIDEO_PART_SHAPES if this server "
            "uses another one."
        ),
        detail={"expected": name, "rejected": attempts},
    )


def probe_all(
    client: VLMClient,
    model: str,
    *,
    video: bool = True,
    remember: bool = True,
) -> Dict[str, VLMProbeResult]:
    """Run the probe chain, stopping where a later step cannot be meaningful.

    Vision is only claimed when step 2 passes; video is only attempted after
    it does. Results are written to :mod:`agent_reach.vlm.observed` so real
    calls need not re-upload a test clip.
    """
    results: Dict[str, VLMProbeResult] = {"reachable": probe_reachable(client)}
    if not results["reachable"].ok:
        return results
    results["vision"] = probe_vision(client, model)
    if video and results["vision"].ok:
        results["video"] = probe_video(client, model)
        # Audio is NOT probed here. Whether a video path carries sound is
        # published by the model's makers and does not change between runs, so
        # it belongs in config (`carries_audio`) rather than in two requests on
        # every health check. `probe_audio` stays available for an endpoint
        # whose behaviour is genuinely unknown.
    if remember and results["vision"].ok:
        inputs = available_inputs(results)
        video_result = results.get("video")
        shape = (video_result.detail.get("shape")
                 if video_result and video_result.ok else None)
        if video_result is None:
            # The video probe was skipped, not failed. Skipping tells us
            # nothing, so a previously measured capability must survive —
            # recording "image only" here would silently disable video.
            previous = observed.get(client.base_url)
            if "video" in (previous.get("inputs") or []):
                inputs = sorted(set(inputs) | {"video"})
                shape = previous.get("video_shape")
        observed.record(client.base_url, inputs, shape)
    return results


def available_inputs(results: Dict[str, VLMProbeResult]) -> List[str]:
    """Representations the endpoint was observed to accept."""
    inputs = []
    if results.get("vision") and results["vision"].ok:
        inputs.append("image")
    if results.get("video") and results["video"].ok:
        inputs.append("video")
    if results.get("audio") and results["audio"].ok:
        inputs.append("audio")
    return inputs
