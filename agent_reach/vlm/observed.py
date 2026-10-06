# -*- coding: utf-8 -*-
"""What an endpoint was actually observed to accept.

Capabilities are established by probing, not declaration — but re-probing on
every call would mean uploading a test video before each real one. So the
probe writes down what it saw, keyed by ``base_url``, and callers read it
back.

This file is a cache of measurements, never a source of truth: deleting it
costs one probe. It is keyed by URL rather than by model name because the
representation a server accepts is a property of the server, not the weights —
the same model rejected video through Ollama's compat layer and accepted it
through llama-server.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def store_path() -> Path:
    """Persistent, deliberately — unlike the result cache.

    What an endpoint accepts is a property of that endpoint. It does not
    change between tasks, and re-establishing it costs real requests against a
    quota. Results and counters are this task's debris and belong on tmpfs;
    these measurements are closer to configuration and outlive the container.
    """
    from agent_reach.utils.paths import home_dir

    return home_dir() / ".agent-reach" / "vlm-observed.json"


def _load() -> Dict[str, Any]:
    try:
        payload = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record(
    base_url: str,
    inputs: List[str],
    video_shape: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Persist what a probe just observed for this endpoint."""
    data = _load()
    previous = data.get(base_url) or {}
    data[base_url] = {
        "inputs": inputs,
        "video_shape": video_shape,
        "model": model or previous.get("model"),
        "checked_at": time.time(),
    }
    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def get(base_url: str) -> Dict[str, Any]:
    """What was observed for this endpoint, or an empty record."""
    entry = _load().get(base_url)
    return entry if isinstance(entry, dict) else {}


def video_shape(base_url: str, configured: Optional[str] = None) -> Optional[str]:
    """The shape to send video in: an explicit config pin beats a measurement."""
    return configured or get(base_url).get("video_shape")


def remember_model(base_url: str, model: str) -> None:
    """Note which model an endpoint advertised.

    A single-model server answers ``GET /models`` with the same id every time.
    Asking again on every command is a round trip for an answer that does not
    change; a stale entry costs one failed call and is then re-resolved.
    """
    data = _load()
    entry = data.get(base_url) or {"inputs": [], "video_shape": None}
    if entry.get("model") == model:
        return
    entry["model"] = model
    data[base_url] = entry
    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def model(base_url: str) -> Optional[str]:
    """The model id this endpoint last advertised, if it has been asked."""
    return get(base_url).get("model")


def supports(base_url: str, representation: str) -> Optional[bool]:
    """True/False if measured, None if this endpoint has never been probed.

    None is deliberately distinct from False: "not yet checked" and "checked
    and unsupported" call for different behaviour, and collapsing them would
    let an unprobed endpoint look broken.
    """
    entry = get(base_url)
    if not entry:
        return None
    return representation in (entry.get("inputs") or [])
