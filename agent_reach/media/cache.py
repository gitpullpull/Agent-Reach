# -*- coding: utf-8 -*-
"""Content-addressed cache for extraction results.

Only structured results are persisted. Video bytes and frame images are never
cached — they are deleted the moment extraction finishes (fork SPEC §5.8). If
a result needs verifying, re-fetching the interval with ``--download-sections``
is cheaper than storing media, and it is also the honest thing to do: a cached
frame could not be re-checked against the source anyway.

There is no index and no schema beyond "one JSON file per key". Reuse
accumulates as a side effect of use; building a corpus is explicitly not a
goal (fork SPEC §2), so nothing here is designed to be queried in bulk.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


def cache_dir(config: Any = None) -> Path:
    from .pace import state_dir

    return state_dir(config) / "cache"


def make_key(
    video_id: str,
    tier: float,
    role: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Identify a result by everything that could change it.

    Frame-selection parameters are part of the key: the same interval sampled
    at a different threshold is a different observation, and silently serving
    the old one would misreport ``cost``.
    """
    payload = json.dumps(
        {
            "video": video_id,
            "tier": tier,
            "role": role,
            "params": params or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _path(key: str, config: Any = None) -> Path:
    return cache_dir(config) / f"{key}.json"


def get(key: str, config: Any = None) -> Optional[Dict[str, Any]]:
    """Return a cached result, or None. Corrupt entries read as a miss."""
    try:
        payload = json.loads(_path(key, config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def put(key: str, value: Dict[str, Any], config: Any = None) -> None:
    """Store a result. Written atomically so a crash cannot leave half a file."""
    directory = cache_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(value)
    record["_cached_at"] = time.time()
    target = _path(key, config)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def purge(config: Any = None, older_than_seconds: Optional[int] = None) -> int:
    """Delete cached results. Returns how many were removed."""
    directory = cache_dir(config)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - older_than_seconds if older_than_seconds else None
    removed = 0
    for entry in directory.glob("*.json"):
        try:
            if cutoff is not None and entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def stats(config: Any = None) -> Dict[str, Any]:
    """Entry count and size, for ``reach-media doctor``."""
    directory = cache_dir(config)
    if not directory.is_dir():
        return {"entries": 0, "bytes": 0}
    entries = list(directory.glob("*.json"))
    total = 0
    for entry in entries:
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return {"entries": len(entries), "bytes": total}
