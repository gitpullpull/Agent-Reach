# -*- coding: utf-8 -*-
"""Media ladder over MCP, for clients that have no shell.

Upstream's rule is that agents call the tools directly rather than through a
wrapper, and it is right: a wrapper buys a shell-capable agent nothing. But a
browser chat client has no shell at all, so "call yt-dlp yourself" is not
advice it can act on. MCP is the only transport it has, which is why upstream
ships an MCP server of its own (``integrations/mcp_server.py``, stdio) and why
this file exists alongside it rather than modifying it.

Two rules keep this from becoming a real wrapper:

  - Tiers 0.5 and 2 shell out to ``reach-media``. The MCP tool is transport,
    not a second implementation, and the JSON that comes back is the same
    contract a terminal user sees.
  - Tiers 0 to 1.5 have no CLI on purpose — they are one-liners a shell agent
    runs itself — so here they call the same library functions the CLI uses
    and are shaped with the same ``ladder.result`` builder. One implementation
    of the logic, one contract, two entry points.

Tier 3 is deliberately absent. Anyone holding the bearer token can spend this
host's bandwidth and its reputation with the platform; whole-video downloads
are not something to hand to a remote caller. Run ``reach-media full`` locally
if you need it.

Transport is streamable HTTP, stateless so a tunnel need not keep session
affinity. Authentication is normally the reverse proxy's job (see
``deploy/``); ``$REACH_MCP_TOKEN`` adds a second check for deployments that
have no proxy in front.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_NAME = "agent-reach-media"

#: Long enough for a tier-2 call on dense material; the client is expected to
#: wait rather than retry, since a retry pays for the download twice.
CALL_TIMEOUT = 1800


def _config():
    from agent_reach.config import Config

    return Config(read_only=True)


def _run_cli(args: List[str], timeout: int = CALL_TIMEOUT) -> Dict[str, Any]:
    """Invoke ``reach-media`` and return its JSON verbatim.

    Non-zero exit is not swallowed: exit code 2 means the work was *refused*
    (pacing, gating), which the caller must be able to tell apart from a
    malfunction.
    """
    # The child prints JSON with ensure_ascii=False. Without forcing its
    # stdio to UTF-8 the bytes come back reinterpreted and every non-ASCII
    # character in a warning or a transcription is mangled.
    from agent_reach.utils.process import utf8_subprocess_env

    proc = subprocess.run(
        ["reach-media", *args],
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        env=utf8_subprocess_env(),
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "error": (proc.stderr or proc.stdout or "no output").strip()[-800:],
            "exit_code": proc.returncode,
        }
    if proc.returncode == 2:
        payload.setdefault("refused", True)
    return payload


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Read internet media through a cost-ordered ladder. Absence from "
            "the subtitles is not absence from the video: when `read` returns "
            "nothing for a span, or its text is garbled, that span is exactly "
            "what `look` exists to recover.\n\n"
            "Cost order: probe < glance < read < comments << look/grep. "
            "The first four download nothing and are effectively free. "
            "`look` and `grep` download a slice of video and take minutes; "
            "call `doctor` before using them. Choose the interval yourself "
            "from the chapters and subtitle timestamps — nothing here will "
            "guess it for you.\n\n"
            "This host runs on a residential connection, so `web_read`, "
            "`web_search`, `reddit_search` and `reddit_read` reach pages that "
            "refuse datacenter addresses. Try those before the video ladder: "
            "for anything a wiki or a forum already answers, they are faster "
            "and cost nothing. Reach for the ladder when the answer is only on "
            "screen — and remember that for versioned material the newest "
            "source is often a video and the most detailed write-up is often "
            "stale."
        ),
    )

    @server.tool(description="Health of the ladder: fetch backend, vision endpoint, pacing, cookie age. Call before any tier-2 work.")
    def doctor() -> Dict[str, Any]:
        return _run_cli(["doctor", "--json", "--no-video-probe"], timeout=300)

    @server.tool(description="Search for videos, newest first. Returns id, title, uploader, upload date, duration, view count. Downloads nothing.")
    def search(query: str, limit: int = 8) -> Dict[str, Any]:
        """Search, with the upload date attached.

        ``--flat-playlist`` is much faster but omits ``upload_date``, and for
        anything that changes over time — a patched game, a moving API, a
        deprecated tool — publication date is the first thing worth sorting
        on. A stale top result is worse than a slow search, so this resolves
        each entry and returns them newest first.
        """
        from agent_reach.media import extract

        limit = max(1, min(int(limit), 25))
        try:
            _, proc = extract.run_backend(
                ["--dump-json", "--no-warnings", "--ignore-errors",
                 f"ytsearch{limit}:{query}"],
                _config(), timeout=900,
            )
        except extract.FetchError as exc:
            return {"error": str(exc)}

        results = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            subs = sorted((e.get("subtitles") or {}).keys())
            autos = sorted((e.get("automatic_captions") or {}).keys())
            results.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                "uploader": e.get("uploader") or e.get("channel"),
                "upload_date": e.get("upload_date"),
                "duration": e.get("duration"),
                "view_count": e.get("view_count"),
                "has_manual_subtitles": bool(subs),
                "has_auto_captions": bool(autos),
            })
        results.sort(key=lambda r: r.get("upload_date") or "", reverse=True)
        return {
            "query": query,
            "results": results,
            "warnings": [
                "sorted newest first. For anything that changes between "
                "versions, the newest source is usually the only current one, "
                "even when older ones are more detailed."
            ],
        }

    @server.tool(description="Tier 0. Metadata, chapters, duration and which subtitle languages exist. Downloads nothing.")
    def probe(url: str) -> Dict[str, Any]:
        from agent_reach.media import extract, ladder

        try:
            meta = extract.summarize_metadata(extract.metadata(url, _config()))
        except extract.FetchError as exc:
            return {"error": str(exc), "tier": 0}
        return ladder.result(
            source=ladder._source(meta, url), tier=0,
            backend={"fetch": "yt-dlp", "vlm": None},
            cost={"frames_sent": 0, "downloaded_seconds": 0, "cached": False},
            segments=[
                {"t_start": c["start_time"], "t_end": c["end_time"], "asr": c["title"],
                 "visible_text": [], "scene": "chapter",
                 "deeplink": ladder.deeplink(url, c["start_time"] or 0)}
                for c in meta.get("chapters") or []
            ],
            warnings=[
                f"subtitles: {', '.join(meta['subtitle_languages']) or 'none'}; "
                f"auto-captions in {len(meta['auto_caption_languages'])} languages"
            ],
        )

    @server.tool(description="Tier 1. Timestamped subtitles, auto-generated included. Downloads nothing. Gaps and garbled spans are reported, not hidden.")
    def read(url: str, languages: Optional[List[str]] = None) -> Dict[str, Any]:
        from agent_reach.media import extract, ladder

        config = _config()
        try:
            meta = extract.summarize_metadata(extract.metadata(url, config))
        except extract.FetchError as exc:
            return {"error": str(exc), "tier": 1}
        with tempfile.TemporaryDirectory(prefix="reach-mcp-") as tmp:
            try:
                cues, language = extract.subtitles(
                    url, Path(tmp), config, languages, video_id=str(meta.get("id"))
                )
            except extract.FetchError as exc:
                return {"error": str(exc), "tier": 1}

        warnings = [f"subtitle track: {language or 'none found'}"]
        if not cues:
            warnings.append(
                "no subtitles in the requested languages. That is a fact about "
                "the track, not about the video — use `look` to read the screen."
            )
        else:
            duration = float(meta.get("duration") or 0)
            covered = cues[-1]["t_end"]
            if duration and covered < duration * 0.9:
                warnings.append(
                    f"subtitles stop at {covered:.0f}s of {duration:.0f}s. "
                    f"The remainder is unread; `look` can recover it."
                )
            gaps = [
                (cues[i]["t_end"], cues[i + 1]["t_start"])
                for i in range(len(cues) - 1)
                if cues[i + 1]["t_start"] - cues[i]["t_end"] > 30
            ]
            for start, end in gaps[:5]:
                warnings.append(
                    f"no speech between {start:.0f}s and {end:.0f}s "
                    f"({end - start:.0f}s) — the screen may be carrying it"
                )
        return ladder.result(
            source=ladder._source(meta, url), tier=1,
            backend={"fetch": "yt-dlp", "vlm": None},
            cost={"frames_sent": 0, "downloaded_seconds": 0, "cached": False},
            segments=[
                {"t_start": c["t_start"], "t_end": c["t_end"], "asr": c["text"],
                 "visible_text": [], "scene": "",
                 "deeplink": ladder.deeplink(url, c["t_start"])}
                for c in cues
            ],
            warnings=warnings,
        )

    @server.tool(description="Tier 1.5. Comments, best-effort. Downloads nothing.")
    def comments(url: str, limit: int = 20) -> Dict[str, Any]:
        from agent_reach.media import extract

        limit = max(1, min(int(limit), 100))
        config = _config()
        with tempfile.TemporaryDirectory(prefix="reach-mcp-") as tmp:
            try:
                extract.run_backend(
                    ["--write-comments", "--skip-download", "--write-info-json",
                     "--no-warnings",
                     "--extractor-args", f"youtube:max_comments={limit}",
                     *extract.cookie_args(config),
                     "-o", str(Path(tmp) / "%(id)s"), url],
                    config, timeout=600,
                )
            except extract.FetchError as exc:
                return {"error": str(exc), "tier": 1.5}
            files = list(Path(tmp).glob("*.info.json"))
            if not files:
                return {"error": "no info json produced", "tier": 1.5}
            info = json.loads(files[0].read_text(encoding="utf-8", errors="replace"))
        return {
            "tier": 1.5,
            "source": {"id": info.get("id"), "url": url, "title": info.get("title")},
            "cost": {"frames_sent": 0, "downloaded_seconds": 0, "cached": False},
            "comments": [
                {"author": c.get("author"), "text": c.get("text"),
                 "likes": c.get("like_count"), "time": c.get("_time_text")}
                for c in (info.get("comments") or [])[:limit]
            ],
            "warnings": ["comments are scraped best-effort; some may be missing"],
        }

    @server.tool(description="Tier 0.5. Read the thumbnail with a vision model. No download, not counted against the daily cap. A cheap sieve over search results.")
    def glance(url: str, role: Optional[str] = None) -> Dict[str, Any]:
        return _run_cli(["glance", url, *(["--role", role] if role else [])], timeout=600)

    @server.tool(description="Tier 2. Download one interval and describe its frames. `at` is required — choosing where to look is your job. Takes minutes and spends one download.")
    def look(
        url: str, at: float, window: float = 30,
        role: Optional[str] = None, method: Optional[str] = None,
        interval_sec: Optional[float] = None, scene_threshold: Optional[float] = None,
        max_frames: Optional[int] = None, input: Optional[str] = None,
    ) -> Dict[str, Any]:
        return _run_cli(["look", url, "--at", str(at), "--window", str(window),
                         *_extraction_flags(role, method, interval_sec,
                                            scene_threshold, max_frames, input)])

    # ── the other half: pages and forums this host can reach ────────────
    #
    # The ladder is only one reason this endpoint exists. The other is the
    # address it runs from: a residential connection reaches sites that refuse
    # datacenter IPs outright. A browser client has no shell, so the channels
    # upstream documents as commands have to be tools here too — thin
    # passthroughs to the documented invocation, never a reimplementation.

    @server.tool(description="Read any web page as clean markdown, from this host's residential connection. Works on sites that block datacenter IPs. Downloads nothing.")
    def web_read(url: str, max_chars: int = 40000) -> Dict[str, Any]:
        """Jina Reader, which renders JavaScript before extracting text.

        The target URL is percent-encoded whole: a query string passed raw is
        parsed as Jina's own parameters and the request fails with a
        validation error that says nothing about the real cause.
        """
        from urllib.parse import quote

        proc = subprocess.run(
            ["curl", "-s", "--max-time", "180",
             f"https://r.jina.ai/{quote(url, safe='')}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=240,
        )
        text = (proc.stdout or "").strip()
        if not text:
            return {"url": url, "error": (proc.stderr or "empty response")[:300]}
        truncated = len(text) > max_chars
        return {
            "url": url,
            "content": text[:max_chars],
            "truncated": truncated,
            "warnings": (["output truncated; raise max_chars for the rest"]
                         if truncated else []),
        }

    @server.tool(description="Semantic web search. Free, no key. Returns titles, URLs and highlights.")
    def web_search(query: str, limit: int = 5) -> Dict[str, Any]:
        proc = subprocess.run(
            ["mcporter", "call", "exa.web_search_exa",
             f"query={query}", f"numResults={max(1, min(int(limit), 20))}"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=300, cwd="/workspaces/agent-reach",
        )
        text = (proc.stdout or "").strip()
        if not text:
            return {"query": query,
                    "error": (proc.stderr or "no output")[:300],
                    "warnings": ["search backend unavailable; try web_read on a "
                                 "search engine's HTML results page"]}
        return {"query": query, "results": text[:40000]}

    @server.tool(description="Search Reddit. A subreddit is effectively required — without one the backend returns unrelated site-wide popular posts.")
    def reddit_search(query: str, subreddit: Optional[str] = None,
                      limit: int = 8) -> Dict[str, Any]:
        args = ["rdt", "search", query, "-n", str(max(1, min(int(limit), 25))),
                "--yaml"]
        if subreddit:
            args += ["--subreddit", subreddit]
        proc = subprocess.run(args, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=300)
        out = (proc.stdout or "").strip()
        if not out:
            return {"query": query, "error": (proc.stderr or "no output")[:300]}
        return {
            "query": query, "subreddit": subreddit, "results": out[:40000],
            "warnings": ([] if subreddit else
                         ["no subreddit given: results are site-wide and "
                          "usually unrelated to the query"]),
        }

    @server.tool(description="Read one Reddit post with its comments, by post id (the base36 id in the permalink).")
    def reddit_read(post_id: str, limit: int = 40) -> Dict[str, Any]:
        proc = subprocess.run(
            ["rdt", "read", post_id, "--yaml"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=300)
        out = (proc.stdout or "").strip()
        if not out:
            return {"post_id": post_id, "error": (proc.stderr or "no output")[:300]}
        return {"post_id": post_id, "content": out[:60000]}

    @server.tool(description="Tier 2. Same as look, but reports only frames whose on-screen text matches a regex. Absence of a match is not absence from the video.")
    def grep(
        url: str, pattern: str, at: float, window: float = 30,
        role: Optional[str] = None, method: Optional[str] = None,
        interval_sec: Optional[float] = None, scene_threshold: Optional[float] = None,
        max_frames: Optional[int] = None,
    ) -> Dict[str, Any]:
        return _run_cli(["grep", url, "--for", pattern, "--at", str(at),
                         "--window", str(window),
                         *_extraction_flags(role, method, interval_sec,
                                            scene_threshold, max_frames, None)])

    return server


def _extraction_flags(role, method, interval_sec, scene_threshold, max_frames, input_mode):
    flags: List[str] = []
    for value, flag in (
        (role, "--role"), (method, "--method"),
        (interval_sec, "--interval-sec"), (scene_threshold, "--scene-threshold"),
        (max_frames, "--max-frames"), (input_mode, "--input"),
    ):
        if value is not None:
            flags += [flag, str(value)]
    return flags


def build_app(path: str = "/mcp"):
    """Starlette app, optionally behind a bearer check of our own.

    The reverse proxy in ``deploy/`` normally does the checking. This second
    check exists so the server is not wide open if it is ever published
    without one.
    """
    server = build_server()
    app = server.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        max_request_body_size=64 * 1024 * 1024,
        host="0.0.0.0",
    )

    token = os.environ.get("REACH_MCP_TOKEN")
    if token:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class _Bearer(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("authorization") != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(_Bearer)
    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("REACH_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("REACH_MCP_PORT", "8090"))
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
