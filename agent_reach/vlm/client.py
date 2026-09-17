# -*- coding: utf-8 -*-
"""OpenAI-compatible chat client — implementation-agnostic.

Only ``POST {base_url}/chat/completions`` and ``GET {base_url}/models`` are
used. Authentication is a bearer token. That is the whole contract.

This module must never branch on the server implementation, the host name, or
the model name. If a backend needs special handling, that is a configuration
problem (swap the endpoint) not a code problem — the same rule upstream
applies to fetch backends in ``channels/base.py``.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import requests

#: Chat payloads carry base64 frames, so they are large and slow. The default
#: is generous because latency is an explicit non-goal (fork SPEC §2).
DEFAULT_TIMEOUT = 300

#: Describing a frame is an output-heavy job: a dense slide or a terminal
#: screen can be most of a page of text, and reasoning models bill their
#: chain-of-thought to the same budget before emitting any of it. A tight
#: budget does not truncate gracefully — it returns empty content, which reads
#: exactly like a model that cannot see. Start wide; the tokens are only spent
#: if the model actually needs them.
DEFAULT_MAX_TOKENS = 16384

#: A server that unloads its model when idle spends 15-60s reloading before
#: the first token. Video payloads then take minutes more. Waiting is always
#: correct here — speed is a non-goal, and a timeout mid-generation wastes the
#: upload as well as the wait.
VIDEO_TIMEOUT = 900

#: Streaming is the default, and not for latency.
#:
#: A proxy in front of the endpoint typically caps how long it will wait for
#: the *first byte* of a response — Cloudflare's is 100s, and it answers with
#: HTTP 524. Measured: dense frame descriptions exceeded it and were lost
#: outright, 6 of 36 in one run, after the download had already been paid for.
#: A client-side timeout cannot help; the connection is cut upstream.
#:
#: With `stream: true` the first token arrives in seconds, so the proxy's
#: first-byte clock never expires and generation may then take as long as it
#: needs. It also changes what `timeout` means: with a stream it bounds the
#: gap between chunks rather than the whole request, which is the honest
#: reading — a generation still producing tokens is not stuck.
STREAM_BY_DEFAULT = True


class VLMError(RuntimeError):
    """Endpoint could not be reached, or answered in an unusable shape."""


def text_part(text: str) -> Dict[str, Any]:
    """A text element of a multi-part ``content`` array."""
    return {"type": "text", "text": text}


def request_marker() -> str:
    """A short unique token to append to a prompt.

    Servers that cache by prompt prefix can serve a previous answer when the
    text is byte-identical, even though the attached media differs. Measured
    on llama-server: four different videos with the same prompt all returned
    the first video's answer, while the same four with a per-request marker
    were each described correctly. Images were unaffected, but the marker is
    applied to both — the failure is silent and plausible-looking, which is
    the worst kind, and a handful of tokens is a cheap guard.

    The marker is opaque on purpose: anything meaningful placed here would
    become content the model might describe or be steered by.
    """
    return uuid.uuid4().hex[:8]


def data_uri(mime: str, payload: bytes) -> str:
    """Encode raw bytes as a ``data:`` URI."""
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def image_part(payload: bytes, mime: str = "image/png") -> Dict[str, Any]:
    """An image element, inlined as a data URI.

    Frames are sent inline rather than by URL: the endpoint is not assumed to
    be able to reach anything on the caller's network.
    """
    return {"type": "image_url", "image_url": {"url": data_uri(mime, payload)}}


#: Servers disagree on how a video is carried in an OpenAI-compatible
#: ``content`` array, and the disagreement is not inferable from anything the
#: server advertises. Measured:
#:
#:   llama-server  accepts ``input_video.data`` (raw base64, no data: prefix);
#:                 rejects ``video_url`` and an mp4 sent as ``image_url``
#:   Gemini        accepts an mp4 data URI through ``image_url`` — the one
#:                 llama-server rejects outright; rejects every dedicated
#:                 video part type it was offered
#:   Ollama        rejects every video shape — its compat layer takes images
#:                 only, whatever the model itself can do
#:
#: No two agree, and none of it is inferable from what they advertise.
#:
#: So this is an ordered candidate list, tried in order and settled by
#: probing — the same rule the fetch backends follow. Adding a server means
#: adding an entry here, never a branch at a call site.
VIDEO_PART_SHAPES: Dict[str, Any] = {
    "input_video_data": lambda payload, mime: {
        "type": "input_video",
        "input_video": {"data": base64.b64encode(payload).decode("ascii")},
    },
    "video_url_data_uri": lambda payload, mime: {
        "type": "video_url",
        "video_url": {"url": data_uri(mime, payload)},
    },
    "video_data_uri": lambda payload, mime: {
        "type": "video",
        "video": data_uri(mime, payload),
    },
    # Last on purpose: a server that takes video through the image part may
    # also accept an image there and answer about the wrong thing. The probe
    # checks the answer, so a shape that "works" but misreads is not settled
    # on — but try the unambiguous, dedicated types first.
    "image_url_video_uri": lambda payload, mime: {
        "type": "image_url",
        "image_url": {"url": data_uri(mime, payload)},
    },
}

#: Probe order. First entry is preferred, the rest are fallbacks.
VIDEO_SHAPE_ORDER: List[str] = [
    "input_video_data",
    "video_url_data_uri",
    "video_data_uri",
    "image_url_video_uri",
]


def video_part(
    payload: bytes, mime: str = "video/mp4", shape: Optional[str] = None
) -> Dict[str, Any]:
    """A video element in one of the shapes servers are known to accept.

    ``shape`` defaults to the preferred candidate. Callers should pass the
    shape :mod:`agent_reach.vlm.probe` observed working for this endpoint —
    never assume support from the model name.
    """
    name = shape or VIDEO_SHAPE_ORDER[0]
    if name not in VIDEO_PART_SHAPES:
        raise VLMError(
            f"unknown video shape '{name}'. Known: "
            f"{', '.join(VIDEO_SHAPE_ORDER)}"
        )
    return VIDEO_PART_SHAPES[name](payload, mime)


@dataclass
class ChatResult:
    """One completion, plus what it cost to get it."""

    text: str
    model: str
    raw: Dict[str, Any]
    reasoning: str = ""
    finish_reason: str = ""

    @property
    def usage(self) -> Dict[str, Any]:
        return self.raw.get("usage") or {}


class VLMClient:
    """Minimal OpenAI-compatible client.

    ``base_url`` is taken verbatim from configuration. Host resolution,
    tunnels, reverse proxies and auth headers in front of the server are the
    operator's concern and invisible here.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        stream: Optional[bool] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        #: Extra top-level request fields, supplied by endpoint config. This
        #: is how a server-specific knob is expressed as data instead of as a
        #: branch here — e.g. llama.cpp's ``cache_prompt: false``.
        self.extra_body = dict(extra_body or {})
        self.stream = STREAM_BY_DEFAULT if stream is None else bool(stream)
        self._session = session or requests.Session()

    # ── plumbing ────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ── API ─────────────────────────────────────────────────────────────

    def list_models(self) -> List[str]:
        """Model ids the endpoint advertises.

        Reachability only. An id appearing here says nothing about whether the
        model accepts images — that is what :mod:`.probe` is for.
        """
        try:
            # An endpoint that stops when idle spends 15-60s loading before it
            # answers anything. Thirty seconds reported that as unreachable,
            # which is a false diagnosis of a healthy server.
            r = self._session.get(
                self._url("models"), headers=self._headers(), timeout=180
            )
        except requests.RequestException as exc:
            raise VLMError(f"cannot reach {self.base_url}: {exc}") from exc
        if r.status_code != 200:
            raise VLMError(
                f"GET {self._url('models')} returned HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        try:
            payload = r.json()
        except json.JSONDecodeError as exc:
            raise VLMError(f"/models did not return JSON: {r.text[:200]}") from exc
        return [m.get("id", "") for m in payload.get("data", []) if m.get("id")]

    def chat(
        self,
        model: str,
        parts: Sequence[Dict[str, Any]],
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        stream: Optional[bool] = None,
    ) -> ChatResult:
        """Send one multi-part user message and return the reply text.

        ``parts`` is the ``content`` array — always an array, even for a
        text-only call, so image and text elements are built the same way.
        """
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": list(parts)})

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        body["max_tokens"] = (
            DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        )
        body.update(self.extra_body)

        if self.stream if stream is None else stream:
            return self._chat_streaming(body, model, timeout)

        try:
            r = self._session.post(
                self._url("chat/completions"),
                headers=self._headers(),
                json=body,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise VLMError(f"chat request to {self.base_url} failed: {exc}") from exc

        if r.status_code != 200:
            raise VLMError(
                f"chat/completions returned HTTP {r.status_code}: {r.text[:300]}"
            )
        try:
            payload = r.json()
        except json.JSONDecodeError as exc:
            raise VLMError(
                f"chat/completions did not return JSON: {r.text[:200]}"
            ) from exc

        choices = payload.get("choices") or []
        if not choices:
            raise VLMError(f"chat/completions returned no choices: {r.text[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise VLMError(f"unexpected message shape: {str(content)[:200]}")

        # Reasoning models return their chain in a separate field and charge
        # it to the same completion budget. A too-small max_tokens therefore
        # yields empty content from a perfectly healthy model — which, left
        # unreported, reads exactly like a model that cannot see the image.
        # Fail loudly with the real cause instead.
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        finish = str(choices[0].get("finish_reason") or "")
        if not content.strip():
            spent = (payload.get("usage") or {}).get("completion_tokens")
            reason = finish or "?"
            raise VLMError(
                f"model returned empty content (finish_reason={reason}, "
                f"completion_tokens={spent}). "
                + (
                    "It spent the token budget on a reasoning field; raise "
                    "max_tokens."
                    if reasoning
                    else "The endpoint accepted the request but produced no text."
                )
            )
        return ChatResult(
            text=content,
            model=payload.get("model", model),
            raw=payload,
            reasoning=str(reasoning),
            finish_reason=finish,
        )

    def _chat_streaming(
        self, body: Dict[str, Any], model: str, timeout: Optional[int]
    ) -> ChatResult:
        """Same contract as :meth:`chat`, assembled from an SSE stream."""
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        try:
            response = self._session.post(
                self._url("chat/completions"),
                headers=self._headers(),
                json=body,
                timeout=timeout or self.timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise VLMError(f"chat request to {self.base_url} failed: {exc}") from exc

        if response.status_code != 200:
            raise VLMError(
                f"chat/completions returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        content, reasoning, finish = [], [], ""
        usage: Dict[str, Any] = {}
        served_model = model
        try:
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                served_model = chunk.get("model") or served_model
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    piece = delta.get("reasoning_content") or delta.get("reasoning")
                    if piece:
                        reasoning.append(piece)
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
        except requests.RequestException as exc:
            # The stream died mid-generation. Partial text is not silently
            # returned: a truncated transcription looks like a complete one.
            raise VLMError(
                f"stream from {self.base_url} broke after "
                f"{sum(len(c) for c in content)} chars: {exc}"
            ) from exc

        text = "".join(content)
        payload = {"usage": usage, "model": served_model}
        if not text.strip():
            raise VLMError(
                f"model returned empty content (finish_reason={finish or '?'}, "
                f"completion_tokens={usage.get('completion_tokens')}). "
                + (
                    "It spent the token budget on a reasoning field; raise "
                    "max_tokens."
                    if reasoning
                    else "The endpoint accepted the request but produced no text."
                )
            )
        return ChatResult(
            text=text,
            model=served_model,
            raw=payload,
            reasoning="".join(reasoning),
            finish_reason=finish,
        )
