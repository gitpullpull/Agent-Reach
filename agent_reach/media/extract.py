# -*- coding: utf-8 -*-
"""Fetching — ordered backends, no site-specific branching.

Backends are an ordered candidate list: element 0 is preferred, the rest are
fallbacks, and switching backends means reordering the list or adding to it in
config. This is upstream's routing rule from ``channels/base.py``, applied to
the ladder. The code below tries the list in order and does nothing else.

There must never be a branch here on a site name or a tool name. When a site
stops working with a given tool that is an operating condition, not a bug, and
the fix belongs in ``media.fetch_backends`` — not in an ``if``. A branch like
that is the point at which this design has failed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .pace import ytdlp_pacing_args

#: Declared candidate order, mirroring the ``backends`` class attribute
#: upstream channels carry. Config reorders or extends it; it is data, not a
#: decision. An explicitly empty ``media.fetch_backends`` means "none
#: configured" and is reported as such rather than silently replaced.
DEFAULT_FETCH_BACKENDS: List[Dict[str, Any]] = [
    {"name": "yt-dlp", "cmd": "yt-dlp", "args": []},
]


class FetchError(RuntimeError):
    """Every configured backend failed. The message lists what was tried."""


class NoBackendError(FetchError):
    """No backend is configured at all."""


@dataclass
class Backend:
    name: str
    cmd: str
    args: List[str] = field(default_factory=list)

    def argv(self, extra: Sequence[str]) -> List[str]:
        return [self.cmd, *self.args, *extra]


def _media_section(config: Any) -> Dict[str, Any]:
    data = getattr(config, "data", None)
    if not isinstance(data, dict):
        data = config if isinstance(config, dict) else {}
    section = data.get("media") or {}
    return section if isinstance(section, dict) else {}


def fetch_backends(config: Any = None) -> List[Backend]:
    """Candidate backends in probe order, honouring the user override.

    ``media.fetch_backend`` (or ``$MEDIA_FETCH_BACKEND``) moves a named
    backend to the front. An unknown name is ignored rather than obeyed, so a
    stale override can never hide a working backend — same rule as upstream's
    ``Channel.ordered_backends``.
    """
    section = _media_section(config)
    configured = section.get("fetch_backends")
    raw = DEFAULT_FETCH_BACKENDS if configured is None else configured
    if not isinstance(raw, list):
        raw = []

    backends = [
        Backend(
            name=str(entry.get("name") or entry.get("cmd") or "?"),
            cmd=str(entry.get("cmd") or entry.get("name") or ""),
            args=[str(a) for a in (entry.get("args") or [])],
        )
        for entry in raw
        if isinstance(entry, dict) and (entry.get("cmd") or entry.get("name"))
    ]

    override = section.get("fetch_backend")
    if not override:
        import os

        override = os.environ.get("MEDIA_FETCH_BACKEND")
    if override:
        for i, backend in enumerate(backends):
            if backend.name == override or backend.name.startswith(str(override)):
                backends.insert(0, backends.pop(i))
                break
    return backends


def cookie_args(config: Any) -> List[str]:
    """``--cookies`` when a cookie file is configured and present."""
    import os

    path = _media_section(config).get("cookies_from")
    if not path:
        return []
    resolved = Path(os.path.expanduser(str(path)))
    return ["--cookies", str(resolved)] if resolved.is_file() else []


def cookie_age_days(config: Any) -> Optional[float]:
    """Age of the cookie file in days, for ``doctor``.

    Expired cookies surface as "Sign in to confirm you're not a bot", which
    looks unrelated to cookie freshness and costs real time to diagnose.
    """
    import os
    import time

    path = _media_section(config).get("cookies_from")
    if not path:
        return None
    resolved = Path(os.path.expanduser(str(path)))
    if not resolved.is_file():
        return None
    return (time.time() - resolved.stat().st_mtime) / 86400


def run_backend(
    args: Sequence[str],
    config: Any = None,
    timeout: int = 1800,
) -> Tuple[Backend, subprocess.CompletedProcess]:
    """Run ``args`` against each backend in order until one succeeds.

    Returns the backend that worked, so ``cost``/``backend`` in the output
    contract reports what actually served the request rather than what was
    preferred.
    """
    backends = fetch_backends(config)
    if not backends:
        raise NoBackendError(
            "no fetch backend configured. Add one to media.fetch_backends in "
            "~/.agent-reach/config.yaml, e.g. "
            "[{name: yt-dlp, cmd: yt-dlp, args: []}]"
        )

    failures: List[str] = []
    for backend in backends:
        try:
            proc = subprocess.run(
                backend.argv(args),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            failures.append(f"{backend.name}: not installed")
            continue
        except subprocess.TimeoutExpired:
            failures.append(f"{backend.name}: timed out after {timeout}s")
            continue
        if proc.returncode == 0:
            return backend, proc
        failures.append(f"{backend.name}: {(proc.stderr or '').strip()[-300:]}")

    raise FetchError(
        "every configured fetch backend failed:\n  " + "\n  ".join(failures)
    )


def backend_version(backend: Backend) -> Optional[str]:
    """Version string, or None when the backend cannot run at all."""
    try:
        proc = subprocess.run(
            [backend.cmd, "--version"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or "").strip() or None if proc.returncode == 0 else None


# ── tier 0 ──────────────────────────────────────────────────────────────


#: Titles, chapters and durations do not change; view counts do, and nothing
#: here depends on them. A day is long enough to make repeated calls free and
#: short enough that a re-uploaded or edited video is picked up.
METADATA_TTL_SECONDS = 24 * 3600


def metadata(url: str, config: Any = None, refresh: bool = False) -> Dict[str, Any]:
    """Tier 0 — everything obtainable without downloading media.

    Cached by URL. Every tier-2 call needs this first, in order to know the
    video id the result cache is keyed on — so without a cache here even a
    cache *hit* cost a request to the platform, which is the opposite of what
    the cheap rungs are for.
    """
    import hashlib

    from . import cache as cache_mod

    key = cache_mod.make_key(
        hashlib.sha256(url.encode("utf-8")).hexdigest()[:32], 0.0, None, {"meta": 1}
    )
    if not refresh:
        hit = cache_mod.get(key, config)
        if hit and (time.time() - float(hit.get("_cached_at", 0))) < METADATA_TTL_SECONDS:
            return hit.get("meta") or {}

    _, proc = run_backend(
        ["-J", "--no-warnings", *cookie_args(config), url], config, timeout=180
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(f"backend did not return JSON metadata: {exc}") from exc
    cache_mod.put(key, {"meta": payload}, config)
    return payload


def summarize_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """The tier-0 fields the ladder and the output contract care about."""
    subs = meta.get("subtitles") or {}
    auto = meta.get("automatic_captions") or {}
    return {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "extractor": meta.get("extractor_key") or meta.get("extractor"),
        "duration": meta.get("duration"),
        "uploader": meta.get("uploader"),
        "upload_date": meta.get("upload_date"),
        "webpage_url": meta.get("webpage_url"),
        "thumbnail": meta.get("thumbnail"),
        "chapters": [
            {
                "title": c.get("title"),
                "start_time": c.get("start_time"),
                "end_time": c.get("end_time"),
            }
            for c in (meta.get("chapters") or [])
        ],
        "subtitle_languages": sorted(subs.keys()),
        "auto_caption_languages": sorted(auto.keys()),
    }


# ── tier 0.5 ────────────────────────────────────────────────────────────


def fetch_thumbnail(thumbnail_url: str, timeout: int = 60) -> bytes:
    """Tier 0.5 — one still, from the image CDN.

    Not a media download: it does not go through the fetch backend, is not
    paced, and does not count against the daily budget, because it never
    touches the endpoint that flags automated behaviour. That is what makes it
    a cheap sieve over search results.
    """
    import requests

    try:
        response = requests.get(thumbnail_url, timeout=timeout)
    except requests.RequestException as exc:
        raise FetchError(f"could not fetch thumbnail: {exc}") from exc
    if response.status_code != 200:
        raise FetchError(f"thumbnail returned HTTP {response.status_code}")
    return response.content


# ── tier 1 ──────────────────────────────────────────────────────────────

_TIMECODE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG = re.compile(r"<[^>]+>")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> List[Dict[str, Any]]:
    """Parse WebVTT into timestamped cues.

    Auto-generated captions roll: each cue repeats the previous line and adds
    to it, so the *later* cue supersedes the earlier one. Such a cue extends
    the previous entry rather than appending a near-duplicate, and the earlier
    start time is kept — that is where the phrase actually began, and it is
    what the deeplink must point at. Exact repeats are dropped outright.
    """
    cues: List[Dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _TIMECODE.search(lines[i])
        if not match:
            i += 1
            continue
        start = _seconds(*match.groups()[:4])
        end = _seconds(*match.groups()[4:])
        i += 1
        body: List[str] = []
        while i < len(lines) and lines[i].strip() and not _TIMECODE.search(lines[i]):
            body.append(_TAG.sub("", lines[i]).strip())
            i += 1
        content = " ".join(part for part in body if part).strip()
        if not content:
            continue
        if cues:
            previous = cues[-1]["text"]
            if content == previous or content in previous:
                cues[-1]["t_end"] = max(cues[-1]["t_end"], end)
                continue
            if content.startswith(previous):
                cues[-1]["text"] = content
                cues[-1]["t_end"] = end
                continue
        cues.append({"t_start": start, "t_end": end, "text": content})
    return cues


def subtitle_languages(config: Any = None) -> List[str]:
    """Preferred subtitle languages, most wanted first."""
    configured = _media_section(config).get("subtitle_languages")
    if isinstance(configured, (list, tuple)) and configured:
        return [str(x) for x in configured]
    return ["en", "ja", "zh-Hans", "zh"]


def subtitles(
    url: str,
    dest: Path,
    config: Any = None,
    languages: Optional[Sequence[str]] = None,
    video_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Tier 1 — timestamped subtitles, no media download.

    Languages are tried **one at a time, stopping at the first hit**, and the
    parsed result is cached per video. Asking for four languages at once meant
    four requests to the platform on every tier-2 call, which reliably earned
    an HTTP 429 and left every segment's ``asr`` field empty — the ladder
    losing the very information it already had from tier 1.

    Returns the cues and the language actually obtained. An empty result is a
    fact about the video, not a failure — and specifically not evidence that
    the information is absent from the video.
    """
    from . import cache as cache_mod

    wanted = list(languages) if languages else subtitle_languages(config)
    key = None
    if video_id:
        key = cache_mod.make_key(str(video_id), 1.0, None, {"subs": wanted})
        hit = cache_mod.get(key, config)
        if hit is not None:
            return hit.get("cues") or [], hit.get("language")

    dest.mkdir(parents=True, exist_ok=True)
    failures: List[str] = []
    for language in wanted:
        try:
            run_backend(
                [
                    "--write-subs", "--write-auto-subs",
                    "--sub-langs", language,
                    "--sub-format", "vtt",
                    "--skip-download", "--no-warnings",
                    *cookie_args(config),
                    "-o", str(dest / "%(id)s"),
                    url,
                ],
                config,
                timeout=300,
            )
        except FetchError as exc:
            failures.append(f"{language}: {exc}")
            continue
        for candidate in sorted(dest.glob(f"*.{language}.vtt")):
            cues = parse_vtt(candidate.read_text(encoding="utf-8", errors="replace"))
            if key:
                cache_mod.put(key, {"cues": cues, "language": language}, config)
            return cues, language

    for candidate in sorted(dest.glob("*.vtt")):
        language = candidate.name.split(".")[-2] if "." in candidate.name else None
        cues = parse_vtt(candidate.read_text(encoding="utf-8", errors="replace"))
        if key:
            cache_mod.put(key, {"cues": cues, "language": language}, config)
        return cues, language

    if failures and len(failures) == len(wanted):
        raise FetchError("no subtitle language could be fetched:\n  " + "\n  ".join(failures))
    if key:
        cache_mod.put(key, {"cues": [], "language": None}, config)
    return [], None


# ── tier 2 / 3 ──────────────────────────────────────────────────────────


def download_section(
    url: str,
    dest: Path,
    start: float,
    end: float,
    config: Any = None,
) -> Path:
    """Tier 2 — fetch only the named interval.

    Callers must already hold the pacing guard; this function does not take it
    itself, so the lock covers metadata lookups in the same run too.
    """
    dest.mkdir(parents=True, exist_ok=True)
    run_backend(
        [
            "--download-sections", f"*{start}-{end}",
            "--force-keyframes-at-cuts",
            "-f", "bv*[height<=720]+ba/b[height<=720]/b",
            "--no-warnings",
            *ytdlp_pacing_args(config),
            *cookie_args(config),
            "-o", str(dest / "clip.%(ext)s"),
            url,
        ],
        config,
        timeout=1800,
    )
    return _sole_video(dest)


def download_full(url: str, dest: Path, config: Any = None) -> Path:
    """Tier 3 — the whole video. Exceptional; gated by the CLI, not here."""
    dest.mkdir(parents=True, exist_ok=True)
    run_backend(
        [
            "-f", "bv*[height<=720]+ba/b[height<=720]/b",
            "--no-warnings",
            *ytdlp_pacing_args(config),
            *cookie_args(config),
            "-o", str(dest / "full.%(ext)s"),
            url,
        ],
        config,
        timeout=7200,
    )
    return _sole_video(dest)


def _sole_video(dest: Path) -> Path:
    files = [p for p in dest.iterdir() if p.is_file() and p.suffix != ".part"]
    if not files:
        raise FetchError(f"backend reported success but wrote nothing to {dest}")
    return max(files, key=lambda p: p.stat().st_size)
