# -*- coding: utf-8 -*-
"""VLM access for the media ladder.

A shared capability, not a channel: nothing here is registered in
``agent_reach.channels``. The ladder needs to turn frames into text, and this
package is the only place that talks to a vision model.

Design constraints (fork SPEC §3.2):
  - OpenAI-compatible ``/chat/completions`` only. No provider SDKs, no
    backend-specific branching. The endpoint may be Ollama, llama.cpp, vLLM
    or a cloud API; this package must not be able to tell.
  - Capabilities are probed, never declared. Whether a model accepts images
    (or video) is decided by actually sending one — see :mod:`.probe`.
  - Agents pick a ``role``, never a model name — see :mod:`.roles`.
"""

from .client import VLMClient, VLMError, image_part, text_part, video_part
from .roles import ResolvedRole, resolve_role

__all__ = [
    "VLMClient",
    "VLMError",
    "text_part",
    "image_part",
    "video_part",
    "ResolvedRole",
    "resolve_role",
]
