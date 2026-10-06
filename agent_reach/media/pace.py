# -*- coding: utf-8 -*-
"""Download pacing — enforced in code, not in the prompt.

An agent told to be gentle will not be. Pacing therefore lives here, where it
cannot be skipped: tier 2 and tier 3 take an exclusive lock, so concurrent
agents queue instead of bursting, and a daily budget refuses work once spent.

The operational reasons this matters (fork SPEC §5.7):
  - YouTube's web client has required a proof-of-origin token since 2024. It
    is produced by YouTube's own JS in a real browser and cannot be forged;
    cookies answer "who" but never "from where".
  - Cookies expire within hours, are invalidated when used from an IP other
    than the one that created them, and typically die after 20-50 downloads.
  - Bulk downloading is the fastest way to get flagged.

Tiers 0 through 1.5 download nothing and are deliberately not rate limited.

State lives in ``~/.agent-reach/media/`` — it must outlive a single command,
so it is not scratch. Everything is file-based: no daemon, no scheduler.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX
    fcntl = None  # type: ignore[assignment]

DEFAULT_DAILY_LIMIT = 30
DEFAULT_PACING: Dict[str, Any] = {
    "sleep_requests": 2,
    "sleep_interval": 5,
    "max_sleep_interval": 15,
    "limit_rate": "2M",
}

#: How long to wait for another agent's download before giving up. Long,
#: because slowness is acceptable and a burst is not.
LOCK_TIMEOUT_SECONDS = 1800


class PaceError(RuntimeError):
    """Work refused by pacing. The message says why and what to do."""


def _media_section(config: Any) -> Dict[str, Any]:
    data = getattr(config, "data", None)
    if not isinstance(data, dict):
        data = config if isinstance(config, dict) else {}
    section = data.get("media") or {}
    return section if isinstance(section, dict) else {}


def state_dir(config: Any = None) -> Path:
    """Where run state lives: counters, the lock, cached results.

    ``$REACH_MEDIA_STATE`` puts it inside the container, which is where it
    belongs. This is a tool invoked per task, not a service: state that
    outlives the container is state nobody asked for, and a cache that grows
    across unrelated tasks is just debris.

    Within one container's life the cache matters — repeating a lookup for an
    answer already fetched is a request nobody needs. Across restarts it does
    not, so the default points at somewhere that vanishes.

    Configuration and credentials are a different thing and live elsewhere.
    """
    # Config first, environment second — upstream's `Config.get` resolves in
    # that order and there is no reason for this to differ.
    override = _media_section(config).get("state_dir")
    if not override:
        override = os.environ.get("REACH_MEDIA_STATE")
    if override:
        return Path(os.path.expanduser(str(override)))
    from agent_reach.utils.paths import home_dir

    return home_dir() / ".agent-reach" / "media"


def daily_limit(config: Any) -> int:
    return int(_media_section(config).get("daily_download_limit", DEFAULT_DAILY_LIMIT))


def ytdlp_pacing_args(config: Any) -> List[str]:
    """Pacing flags passed to every download invocation."""
    pacing = dict(DEFAULT_PACING)
    configured = _media_section(config).get("pacing") or {}
    if isinstance(configured, dict):
        pacing.update(configured)
    return [
        "--sleep-requests", str(pacing["sleep_requests"]),
        "--sleep-interval", str(pacing["sleep_interval"]),
        "--max-sleep-interval", str(pacing["max_sleep_interval"]),
        "--limit-rate", str(pacing["limit_rate"]),
    ]


# ── counter ─────────────────────────────────────────────────────────────


def _counter_path(config: Any) -> Path:
    return state_dir(config) / "downloads.json"


def _read_counter(config: Any) -> Dict[str, Any]:
    path = _counter_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"date": date.today().isoformat(), "count": 0, "last_run": None}
    if payload.get("date") != date.today().isoformat():
        # A new day resets the budget but keeps the last-run timestamp, which
        # doctor uses to show how recently this host touched the platform.
        return {
            "date": date.today().isoformat(),
            "count": 0,
            "last_run": payload.get("last_run"),
        }
    return payload


def _write_counter(config: Any, payload: Dict[str, Any]) -> None:
    directory = state_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "downloads.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, directory / "downloads.json")


def status(config: Any) -> Dict[str, Any]:
    """Current pacing state, for ``reach-media doctor``."""
    counter = _read_counter(config)
    last = counter.get("last_run")
    return {
        "downloads_today": counter.get("count", 0),
        "daily_limit": daily_limit(config),
        "last_download_epoch": last,
        "seconds_since_last": (time.time() - last) if last else None,
    }


# ── lock ────────────────────────────────────────────────────────────────


@contextmanager
def _exclusive_lock(path: Path, timeout: int) -> Iterator[None]:
    """Serialise downloads across concurrent agents sharing this home."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if fcntl is None:  # pragma: no cover — non-POSIX
            yield
            return
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise PaceError(
                        f"another download has held {path} for over "
                        f"{timeout}s. Wait, or remove the file if no "
                        f"reach-media process is running."
                    )
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def guard(config: Any, tier: float, timeout: int = LOCK_TIMEOUT_SECONDS) -> Iterator[Dict[str, Any]]:
    """Hold the download slot for a tier-2 or tier-3 run.

    Tiers below 2 pass through untouched — they download nothing, so pacing
    them would only slow the cheap path the ladder wants agents to prefer.

    The counter is incremented *before* the download, not after: a failed
    download still contacted the platform and still counts toward the
    behaviour that gets a session flagged.
    """
    if tier < 2:
        yield {"paced": False}
        return

    limit = daily_limit(config)
    with _exclusive_lock(state_dir(config) / ".lock", timeout):
        counter = _read_counter(config)
        used = int(counter.get("count", 0))
        if used >= limit:
            raise PaceError(
                f"daily download budget spent ({used}/{limit}). Tier 2 and "
                f"tier 3 are refused until tomorrow. Tiers 0-1.5 still work, "
                f"and media.daily_download_limit raises the cap if you accept "
                f"the session risk."
            )
        counter["count"] = used + 1
        counter["last_run"] = time.time()
        _write_counter(config, counter)
        yield {
            "paced": True,
            "downloads_today": counter["count"],
            "daily_limit": limit,
        }
