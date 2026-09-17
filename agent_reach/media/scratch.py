# -*- coding: utf-8 -*-
"""Disposable working space for downloaded media.

Video bytes and extracted frames are never kept (fork SPEC §5.8). They live in
a scratch tree that is torn down when the command exits, and whose root is
expected to be a tmpfs inside the sandbox container — so an agent that
generates junk generates it somewhere that vanishes with the container.

Resolution order for the root:
  1. ``$REACH_MEDIA_SCRATCH``  — explicit override
  2. ``$TMPDIR/reach-media``   — the container mounts tmpfs at /tmp
  3. ``~/.agent-reach/media/scratch`` — last resort, swept by TTL

A crashed run cannot clean up after itself, so :func:`sweep` removes stale
workspaces on a TTL. ``reach-media clean`` calls it; so does every ladder run,
which keeps a long-lived container from accumulating debris without needing a
daemon (fork SPEC §2 rules out resident processes).
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

#: Workspaces older than this are debris from a crashed run.
DEFAULT_TTL_SECONDS = 6 * 3600

_MARKER = ".reach-media-workspace"


def scratch_root(config=None) -> Path:
    """Root of the disposable tree. Never contains anything worth keeping."""
    override = os.environ.get("REACH_MEDIA_SCRATCH")
    if not override and config is not None:
        section = getattr(config, "data", {}) or {}
        override = (section.get("media") or {}).get("scratch_dir")
    if override:
        return Path(os.path.expanduser(str(override)))

    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and Path(tmpdir).is_dir():
        return Path(tmpdir) / "reach-media"

    from agent_reach.utils.paths import home_dir

    return home_dir() / ".agent-reach" / "media" / "scratch"


@contextmanager
def workspace(root: Path, label: str = "run") -> Iterator[Path]:
    """A directory that is removed on exit, however the block ends.

    Removal is unconditional: an exception mid-extraction must not leave a
    downloaded video behind. If a frame needs re-inspecting later, it is
    cheaper to re-fetch the interval than to retain video bytes.
    """
    root.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:48]
    path = Path(
        __import__("tempfile").mkdtemp(prefix=f"{safe}-", dir=str(root))
    )
    (path / _MARKER).write_text(str(time.time()), encoding="utf-8")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def sweep(root: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> List[str]:
    """Delete workspaces older than ``ttl_seconds``. Returns what was removed.

    Only directories carrying this module's marker file are touched, so a
    misconfigured root pointing at a real directory cannot cause damage.
    """
    removed: List[str] = []
    if not root.is_dir():
        return removed
    cutoff = time.time() - ttl_seconds
    for child in root.iterdir():
        if not child.is_dir() or not (child / _MARKER).exists():
            continue
        try:
            created = float((child / _MARKER).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            created = child.stat().st_mtime
        if created < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed


def purge(root: Path) -> List[str]:
    """Delete every workspace regardless of age (``reach-media clean --all``)."""
    return sweep(root, ttl_seconds=-1)


def usage_bytes(root: Path) -> int:
    """Bytes currently held in the scratch tree, for ``doctor``."""
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total
