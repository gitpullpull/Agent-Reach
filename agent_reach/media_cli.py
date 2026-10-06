# -*- coding: utf-8 -*-
"""``reach-media`` — the rungs an agent cannot reach with one shell command.

Tiers 0 through 1.5 are plain yt-dlp invocations and are documented in
``skill/references/media-ladder.md`` instead of being wrapped here: upstream
is an enabling layer, and wrapping a command an agent can already run would
make this a wrapper for no gain.

What is left needs code:

    glance   tier 0.5   one thumbnail through a vision model
    look     tier 2     download an interval, extract frames, describe them
    grep     tier 2     the same, scanning for a pattern on screen
    full     tier 3     the whole video; gated, and rarely correct
    doctor   diagnose fetch backends, vision, pacing, cookies
    clean    drop the disposable working tree

Every command is stateless, prints JSON to stdout, and reports what it cost.
None of them decides whether the tier was warranted — that is the agent's job.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Dict, List, Optional

from agent_reach.config import Config
from agent_reach.media import cache, extract, frames as frames_mod, ladder, pace, scratch
from agent_reach.vlm import probe as vlm_probe
from agent_reach.vlm.client import VLMError, image_part, request_marker, text_part
from agent_reach.vlm.roles import default_input, list_roles, resolve_role

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2  # pacing or gating refused the work; not a malfunction


def _emit(payload: Dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _fail(message: str, code: int = EXIT_ERROR, **extra: Any) -> int:
    _emit({"error": message, **extra})
    return code


def _housekeep(config: Config) -> None:
    """Drop debris from crashed runs. No daemon, so every run sweeps a little."""
    try:
        scratch.sweep(scratch.scratch_root(config))
    except OSError:
        pass


def _extraction_flags(parser: argparse.ArgumentParser) -> None:
    """Frame-selection flags. All explicit — nothing here is auto-selected."""
    parser.add_argument("--role", help="vision role (see `reach-media doctor`)")
    parser.add_argument("--method", choices=("scene", "interval"),
                        help="scene: cut detection. interval: fixed cadence, "
                             "for material whose pixels barely change")
    parser.add_argument("--scene-threshold", type=float)
    parser.add_argument("--interval-sec", type=float)
    parser.add_argument("--phash-distance", type=int)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--input", choices=("image", "video"),
                        help="representation to send; only what doctor observed "
                             "the endpoint accept")
    parser.add_argument("--with-subtitles", action="store_true",
                        help="also fetch the tier-1 subtitle track and fill "
                             "`asr`. Off by default: it is a separate platform "
                             "request, and these models do not read audio")


# ── tier 0.5 ────────────────────────────────────────────────────────────


def cmd_glance(args: argparse.Namespace, config: Config) -> int:
    """Tier 0.5 — one thumbnail, no media download.

    The thumbnail comes from an image CDN, so it is neither paced nor counted
    against the daily budget. That is what makes it usable as a sieve over a
    list of search results before spending a tier-2 download on one of them.
    """
    try:
        meta = extract.summarize_metadata(extract.metadata(args.url, config))
    except extract.FetchError as exc:
        return _fail(str(exc))
    if not meta.get("thumbnail"):
        return _fail("this video exposes no thumbnail", tier=0.5)

    try:
        role = resolve_role(config, args.role)
        image = extract.fetch_thumbnail(meta["thumbnail"])

        def _describe(binding):
            client = binding.client()
            return client.chat(
                binding.effective_model(client),
                [text_part(f"{ladder._FRAME_PROMPT} [{request_marker()}]"),
                 image_part(image, "image/jpeg")],
                max_tokens=role.max_tokens,
            )

        reply, failures = role.attempt(_describe)
        role.warnings += [f"fell through to another endpoint: {f[:160]}" for f in failures]
    except (VLMError, extract.FetchError) as exc:
        return _fail(str(exc), tier=0.5)

    parsed = ladder._parse_frame_reply(reply.text)
    _emit(ladder.result(
        source=ladder._source(meta, args.url),
        tier=0.5,
        backend={"fetch": "image CDN (not the fetch backend)",
                 "vlm": role.describe()},
        cost={"frames_sent": 1, "downloaded_seconds": 0, "cached": False,
              "counted_against_daily_limit": False},
        segments=[{
            "t_start": 0, "t_end": 0, "asr": "",
            "visible_text": parsed["visible_text"], "scene": parsed["scene"],
            "deeplink": meta.get("webpage_url") or args.url,
        }],
        warnings=role.warnings + [
            "a thumbnail is authored, not sampled: it shows what the uploader "
            "chose to advertise, not what the video contains"
        ],
    ))
    return EXIT_OK


# ── tier 2 / 3 ──────────────────────────────────────────────────────────


def _run_visual_tier(
    args: argparse.Namespace,
    config: Config,
    tier: float,
    pattern: Optional[str] = None,
) -> int:
    """Shared body of look / grep / full.

    Order matters: metadata and the pacing guard come before any download, and
    the working directory is destroyed on the way out whatever happens.
    """
    try:
        role = resolve_role(config, args.role)
    except VLMError as exc:
        return _fail(str(exc), tier=tier)

    settings = frames_mod.extraction_settings(
        config,
        method=args.method,
        scene_threshold=args.scene_threshold,
        interval_sec=args.interval_sec,
        phash_distance=args.phash_distance,
        max_frames=args.max_frames if args.max_frames is not None else role.max_frames,
    )
    warnings: List[str] = list(role.warnings)

    try:
        raw_meta = extract.metadata(args.url, config)
    except extract.FetchError as exc:
        return _fail(str(exc), tier=tier)
    meta = extract.summarize_metadata(raw_meta)

    if tier >= 2 and args.at is not None:
        t_start = max(0.0, float(args.at) - 0.0)
        t_end = t_start + float(args.window)
    else:
        t_start, t_end = 0.0, float(meta.get("duration") or 0)

    key = cache.make_key(
        str(meta.get("id")), tier, role.role,
        {**settings, "t_start": t_start, "t_end": t_end, "pattern": pattern,
         "input": args.input or default_input(config)},
    )
    cached = cache.get(key, config)
    if cached and not args.no_cache:
        cached.setdefault("cost", {})["cached"] = True
        _emit(cached)
        return EXIT_OK

    # A clip is what the material actually is: sending it whole keeps what
    # happens between sampled frames, carries the audio, and costs one request
    # rather than N. Frames are the fallback for endpoints that cannot take
    # video — chosen from what the probe observed, never assumed.
    input_mode = args.input or role.primary.input_pin or default_input(config)
    if input_mode == "video":
        usable = [b for b in role.candidates if b.supports_video() is not False]
        unprobed = [b.endpoint for b in usable if b.supports_video() is None]
        if not usable:
            names = ", ".join(b.endpoint for b in role.candidates)
            warnings.append(
                f"no endpoint for role '{role.role}' ({names}) accepts video; "
                f"falling back to frames"
            )
            input_mode = "image"
        else:
            role.candidates = usable
            if unprobed:
                warnings.append(
                    f"never probed for video: {', '.join(unprobed)} — run "
                    f"`reach-media doctor` if this call fails"
                )
    root = scratch.scratch_root(config)

    try:
        with pace.guard(config, tier) as paced:
            with scratch.workspace(root, f"tier{tier}-{meta.get('id')}") as work:
                if tier >= 3:
                    clip = extract.download_full(args.url, work / "media", config)
                    t_end = float(meta.get("duration") or 0)
                else:
                    clip = extract.download_section(
                        args.url, work / "media", t_start, t_end, config
                    )

                # Tier 2 does not fetch subtitles. It used to, on every call,
                # which cost an extra platform request per call (reliably
                # earning HTTP 429), added latency, and in video mode was
                # thrown away anyway because the model hears the clip.
                #
                # Subtitles are tier 1 — a separate, cheaper rung that
                # downloads nothing. A tier-2 call silently doing tier 1's job
                # is what made the two indistinguishable. Ask for `read` if you
                # want the platform's track; ask for video input if you want
                # what was actually said.
                # Subtitles are tier 1 — a separate, cheaper rung that
                # downloads nothing — and tier 2 used to fetch them on every
                # call, costing an extra platform request (reliably earning
                # HTTP 429) for a field that video mode overwrites anyway.
                #
                # They are still the only source of speech: the vision models
                # here read frames and timestamps, not audio. So they are
                # available on request rather than by default.
                cues: List[Dict[str, Any]] = []
                if getattr(args, "with_subtitles", False):
                    try:
                        cues, language = extract.subtitles(
                            args.url, work / "subs", config,
                            video_id=str(meta.get("id")),
                        )
                        warnings.append(
                            f"asr is the '{language}' subtitle track"
                            if language else
                            "no subtitle track found for --with-subtitles"
                        )
                    except extract.FetchError as exc:
                        warnings.append(f"subtitles unavailable: {exc}")

                if input_mode == "video":
                    segments, more = ladder.describe_clip(
                        role, args.url, clip, t_start, t_end, cues
                    )
                    # Speech redirect. Which endpoint answered is only known
                    # now, and endpoints differ: one reads the audio track,
                    # another expands the clip into frames and hears nothing.
                    # Rather than assume, ask what was measured, and fetch the
                    # subtitle track only when the answer is "it cannot hear"
                    # and nothing was in fact heard.
                    served = role.active or role.primary
                    if segments and not any(s.get("asr") for s in segments):
                        if served.carries_audio() is not True:
                            try:
                                cues, language = extract.subtitles(
                                    args.url, work / "subs", config,
                                    video_id=str(meta.get("id")),
                                )
                            except extract.FetchError as exc:
                                cues, language = [], None
                                warnings.append(f"subtitles unavailable: {exc}")
                            if cues:
                                for seg in segments:
                                    seg["asr"] = ladder._asr_at(
                                        cues, seg["t_start"],
                                        max(seg["t_end"], seg["t_start"] + 1))
                                warnings.append(
                                    f"'{served.endpoint}' reads frames, not audio, "
                                    f"so `asr` was filled from the '{language}' "
                                    f"subtitle track"
                                )
                    # No frames are sent, but the call is not free: report what
                    # was uploaded so `cost` stays honest across input modes.
                    frames_sent = 0
                    video_bytes = clip.stat().st_size
                else:
                    picked = frames_mod.select(clip, work / "frames", settings, t_start)
                    if not picked:
                        warnings.append(
                            f"method '{settings['method']}' selected no frames in "
                            f"this interval; try --method interval or a lower "
                            f"--scene-threshold"
                        )
                    segments, more = ladder.describe_frames(
                        role, args.url, picked, cues, t_limit=t_end
                    )
                    frames_sent = len(picked)
                    video_bytes = 0
                    if not cues:
                        warnings.append(
                            "`asr` is empty: these models read frames and "
                            "timestamps, not audio. Pass --with-subtitles, or "
                            "call tier 1 (`read`) separately."
                        )
                warnings.extend(more)
    except pace.PaceError as exc:
        return _fail(str(exc), EXIT_REFUSED, tier=tier)
    except (extract.FetchError, frames_mod.FrameError) as exc:
        return _fail(str(exc), tier=tier)

    if pattern:
        try:
            needle = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return _fail(f"invalid --for pattern: {exc}", tier=tier)
        matched = [
            s for s in segments
            if any(needle.search(t) for t in s["visible_text"])
            or needle.search(s.get("scene") or "")
        ]
        warnings.append(
            f"{len(matched)}/{len(segments)} frames matched; "
            f"absence here is not absence from the video"
        )
        segments = matched

    payload = ladder.result(
        source=ladder._source(meta, args.url),
        tier=tier,
        backend={
            "fetch": _fetch_label(config),
            "vlm": {**role.describe(), "input": input_mode},
        },
        cost={
            "frames_sent": frames_sent,
            "video_bytes_sent": video_bytes,
            "downloaded_seconds": round(t_end - t_start, 1),
            "cached": False,
            "downloads_today": paced.get("downloads_today"),
            "daily_limit": paced.get("daily_limit"),
        },
        segments=segments,
        warnings=warnings,
    )
    cache.put(key, payload, config)
    _emit(payload)
    return EXIT_OK


def _fetch_label(config: Config) -> str:
    backends = extract.fetch_backends(config)
    if not backends:
        return "(none configured)"
    version = extract.backend_version(backends[0])
    return f"{backends[0].name} {version}" if version else backends[0].name


def cmd_look(args, config):
    return _run_visual_tier(args, config, 2.0)


def cmd_grep(args, config):
    return _run_visual_tier(args, config, 2.0, pattern=args.for_pattern)


def cmd_full(args, config):
    if not args.yes_i_know_this_is_expensive:
        return _fail(
            "tier 3 downloads the entire video: slowest, highest bot-detection "
            "risk, and usually the wrong rung. If tier 1 gave you a timestamp, "
            "use `look --at`. Pass --yes-i-know-this-is-expensive to proceed.",
            EXIT_REFUSED,
            tier=3,
        )
    args.at, args.window = None, 0
    return _run_visual_tier(args, config, 3.0)


# ── doctor / clean ──────────────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """Diagnose the ladder. Every line that can break carries its fix."""
    report: Dict[str, Any] = {}

    backends = extract.fetch_backends(config)
    report["fetch"] = {
        "configured": [b.name for b in backends],
        "active": None,
        "version": None,
        "fix": None,
    }
    if not backends:
        report["fetch"]["fix"] = (
            "no backend configured — set media.fetch_backends in "
            "~/.agent-reach/config.yaml"
        )
    else:
        for backend in backends:
            version = extract.backend_version(backend)
            if version:
                report["fetch"].update(active=backend.name, version=version)
                break
        if not report["fetch"]["active"]:
            report["fetch"]["fix"] = (
                f"none of {[b.name for b in backends]} can execute. Install the "
                f"preferred one, or add an alternative to media.fetch_backends"
            )
        elif len(backends) == 1:
            # Stating the exposure, not prescribing a fix. The alternative
            # upstream documents for YouTube rides a desktop browser session,
            # which a headless container cannot have — so telling this
            # deployment to "add one" every single run is advice it cannot
            # act on, and an unactionable warning is one people stop reading.
            report["fetch"]["fix"] = (
                f"single point of failure: {backends[0].name} is the only "
                f"fetch backend, so a platform change that breaks it takes "
                f"every tier with it. Add an alternative to "
                f"media.fetch_backends if one becomes available."
            )

    report["vision"] = {"roles": list_roles(config)}
    try:
        role = resolve_role(config, args.role)
        # Only the endpoint that would actually serve, unless asked otherwise.
        # Probing the fallbacks woke a locally hosted model — tens of
        # gigabytes into VRAM, a cold start, and nothing needed it — on every
        # health check. A fallback is there for when the primary fails; that is
        # when finding out whether it works is worth something.
        candidates = role.candidates if args.all_endpoints else role.candidates[:1]
        endpoints = []
        for binding in candidates:
            entry = {"endpoint": binding.endpoint, "model": binding.model}
            if not args.probe:
                # Report what is known; spend nothing. Upstream's doctor makes
                # the same choice for the same reason — a health check that
                # costs requests against a quota is a health check people stop
                # running. What was measured before is reported as measured
                # before, with its age, and never as "verified just now".
                from agent_reach.vlm import observed as vlm_observed

                seen = vlm_observed.get(binding.base_url)
                entry.update(
                    model=seen.get("model") or binding.model,
                    observed_inputs=seen.get("inputs") or [],
                    video_shape=binding.resolved_video_shape(),
                    last_checked=seen.get("checked_at"),
                    verified_this_run=False,
                )
                endpoints.append(entry)
                continue
            try:
                client = binding.client()
                results = vlm_probe.probe_all(
                    client, binding.effective_model(client),
                    video=not args.no_video_probe,
                )
                entry.update(
                    model=binding.describe()["model"],
                    probes={k: {"status": v.status, "detail": v.output,
                                "fix": v.hint or None}
                            for k, v in results.items()},
                    observed_inputs=vlm_probe.available_inputs(results),
                    video_shape=binding.resolved_video_shape(),
                    verified_this_run=True,
                )
            except VLMError as exc:
                entry["error"] = str(exc)
            endpoints.append(entry)
        skipped = [b.endpoint for b in role.candidates[len(candidates):]]
        if skipped:
            role.warnings.append(
                f"not probed: {', '.join(skipped)} (fallbacks are left asleep; "
                f"--all-endpoints checks them)"
            )
        report["vision"].update(
            role=role.role, endpoints=endpoints, warnings=role.warnings,
            configured_endpoints=[b.endpoint for b in role.candidates],
            fix=(None if len(role.candidates) > 1 else
                 "only one endpoint configured for this role — add a fallback "
                 "to vlm.roles.<role>.endpoints before the current one rate-limits"),
        )
    except VLMError as exc:
        report["vision"].update(
            error=str(exc),
            fix="add a vlm section to ~/.agent-reach/config.yaml (see docs/ENVIRONMENT.md)",
        )

    report["pacing"] = pace.status(config)
    age = extract.cookie_age_days(config)
    report["cookies"] = {
        "age_days": round(age, 1) if age is not None else None,
        "fix": (
            "cookies are stale; re-export them. Expiry surfaces as "
            "\"Sign in to confirm you're not a bot\", which looks unrelated"
            if age is not None and age > 3
            else ("no cookie file configured or present" if age is None else None)
        ),
    }
    root = scratch.scratch_root(config)
    report["disposable"] = {
        "scratch_root": str(root),
        "scratch_bytes": scratch.usage_bytes(root),
        "cache": cache.stats(config),
        "fix": "`reach-media clean --all` drops both",
    }

    if args.json:
        _emit(report)
    else:
        _print_doctor(report)
    healthy = bool(report["fetch"]["active"]) and any(
        e.get("observed_inputs") for e in report["vision"].get("endpoints", [])
    )
    return EXIT_OK if healthy else EXIT_ERROR


def _print_doctor(report: Dict[str, Any]) -> None:
    def mark(ok: Optional[bool]) -> str:
        return {True: "OK  ", False: "FAIL", None: "--  "}[ok]

    out = sys.stdout.write
    out("media ladder\n")
    fetch = report["fetch"]
    out(f"  fetch      {mark(bool(fetch['active']))} "
        f"{fetch['active'] or 'none'} {fetch['version'] or ''}"
        f"   configured: {', '.join(fetch['configured']) or '(none)'}\n")
    if fetch.get("fix"):
        out(f"             -> {fetch['fix']}\n")

    vision = report["vision"]
    if "error" in vision:
        out(f"  vision     FAIL {vision['error']}\n             -> {vision['fix']}\n")
    else:
        eps = vision.get("endpoints", [])
        out(f"  vision     role={vision['role']}   endpoints in order: "
            f"{', '.join(vision.get('configured_endpoints') or [])}\n")
        for i, e in enumerate(eps):
            tag = "preferred" if i == 0 else "fallback"
            out(f"    [{tag}] {e['endpoint']}  model={e.get('model')}\n")
            if "error" in e:
                out(f"      FAIL {e['error'][:150]}\n")
                continue
            if not e.get("verified_this_run"):
                age = e.get("last_checked")
                when = (f"{(time.time() - age) / 3600:.0f}h ago"
                        if age else "never")
                out(f"      measured   {when}  (not called this run; "
                    f"--probe verifies)\n")
            for name, p in e.get("probes", {}).items():
                out(f"      {name:<10} {mark(p['status'] == 'ok')} {p['detail'][:90]}\n")
                if p.get("fix"):
                    out(f"                 -> {p['fix'][:110]}\n")
            shape = e.get("video_shape")
            out(f"      observed   inputs: {', '.join(e.get('observed_inputs') or []) or 'none'}"
                + (f"   video shape: {shape}" if shape else "")
                + "   [measured, not declared]\n")
        if vision.get("fix"):
            out(f"             -> {vision['fix']}\n")
        for warning in vision.get("warnings", []):
            out(f"    warn       {warning}\n")

    p = report["pacing"]
    since = p["seconds_since_last"]
    out(f"  pacing     {p['downloads_today']}/{p['daily_limit']} downloads today"
        + (f"   last {since / 60:.0f} min ago\n" if since else "   no downloads yet\n"))

    c = report["cookies"]
    out(f"  cookies    {mark(None if c['age_days'] is None else c['age_days'] <= 3)} "
        f"{'age ' + str(c['age_days']) + 'd' if c['age_days'] is not None else 'not configured'}\n")
    if c.get("fix"):
        out(f"             -> {c['fix']}\n")

    d = report["disposable"]
    out(f"  disposable scratch {d['scratch_bytes'] / 1e6:.1f} MB at {d['scratch_root']}"
        f"   cache {d['cache']['entries']} entries\n")


def cmd_clean(args: argparse.Namespace, config: Config) -> int:
    """Drop the disposable tree. Structured results survive unless --cache."""
    root = scratch.scratch_root(config)
    if args.all:
        removed = scratch.purge(root)
    else:
        removed = scratch.sweep(root, args.older_than * 3600)
    payload = {"scratch_removed": len(removed), "scratch_root": str(root)}
    if args.cache:
        payload["cache_removed"] = cache.purge(
            config, None if args.all else args.older_than * 3600
        )
    _emit(payload)
    return EXIT_OK


# ── entry point ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reach-media",
        description="Descend the representation ladder. Tiers 0-1.5 are plain "
                    "yt-dlp commands — see the media-ladder skill reference.",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("glance", help="tier 0.5 — thumbnail through a vision model")
    p.add_argument("url")
    p.add_argument("--role")
    p.set_defaults(func=cmd_glance)

    p = sub.add_parser("look", help="tier 2 — describe frames from an interval")
    p.add_argument("url")
    p.add_argument("--at", type=float, required=True,
                   help="interval start, seconds. Required: choosing where to "
                        "look is the agent's job, not this tool's")
    p.add_argument("--window", type=float, default=30)
    p.add_argument("--no-cache", action="store_true")
    _extraction_flags(p)
    p.set_defaults(func=cmd_look)

    p = sub.add_parser("grep", help="tier 2 — scan on-screen text for a pattern")
    p.add_argument("url")
    p.add_argument("--for", dest="for_pattern", required=True, help="regex")
    p.add_argument("--at", type=float, required=True)
    p.add_argument("--window", type=float, default=30)
    p.add_argument("--no-cache", action="store_true")
    _extraction_flags(p)
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("full", help="tier 3 — the whole video. Gated.")
    p.add_argument("url")
    p.add_argument("--yes-i-know-this-is-expensive", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    _extraction_flags(p)
    p.set_defaults(func=cmd_full)

    p = sub.add_parser("doctor", help="diagnose backends, vision, pacing, cookies")
    p.add_argument("--role")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-video-probe", action="store_true")
    p.add_argument("--probe", action="store_true",
                   help="actually call the endpoint to verify it. Off by "
                        "default: every probe spends requests against a quota, "
                        "so a health check should not silently bill you")
    p.add_argument("--all-endpoints", action="store_true",
                   help="also probe the fallback endpoints. Off by default: "
                        "probing a locally hosted fallback cold-starts it and "
                        "parks a model in VRAM that nothing asked for")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("clean", help="drop the disposable working tree")
    p.add_argument("--all", action="store_true", help="ignore age")
    p.add_argument("--older-than", type=float, default=6, help="hours (default 6)")
    p.add_argument("--cache", action="store_true", help="also drop cached results")
    p.set_defaults(func=cmd_clean)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    config = Config()
    _housekeep(config)
    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        return _fail("interrupted", EXIT_REFUSED)


if __name__ == "__main__":
    sys.exit(main())
