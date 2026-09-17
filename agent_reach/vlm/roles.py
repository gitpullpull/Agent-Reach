# -*- coding: utf-8 -*-
"""Role indirection — agents name a role, never a model or a provider.

An agent asks for ``fast``, ``accurate`` or ``video``. It never learns which
models exist, where they run, or which one answered. That is a design
principle: the agent should reason about how much fidelity it needs, not about
which weights are loaded or whose quota is left.

A role resolves to an **ordered list of endpoints**, not to one. This is
upstream's rule for fetch backends applied where it was missing
(``channels/base.py``):

    `backends` is an ORDERED candidate list: backends[0] is the preferred
    backend, the rest are fallbacks. "Switching backends" means reordering
    this list — not rewriting code.

The failure this exists for is real and was measured: a hosted endpoint on a
free tier answers HTTP 429 partway through a call, and without a fallback the
work is simply lost even though a local endpoint could have served it. Rate
limits, cold starts and outages are transient conditions, not verdicts on
whether the ladder can see.

An unknown role falls back to ``default_role`` with a warning rather than
failing: a typo must not abort work already paid for in downloads.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import DEFAULT_MAX_TOKENS, VLMClient, VLMError


@dataclass
class Binding:
    """One endpoint a role may use, with everything needed to call it."""

    endpoint: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    extra_body: Dict[str, Any] = field(default_factory=dict)
    stream: Optional[bool] = None
    video_shape_pin: Optional[str] = None
    input_pin: Optional[str] = None
    #: Declared in config when the answer is published and stable — e.g. a
    #: model documented as not reading dialogue. None means "look at what was
    #: probed", which is the right default for an unfamiliar endpoint.
    audio_pin: Optional[bool] = None
    _resolved_model: Optional[str] = field(default=None, repr=False)

    def client(self, **kwargs: Any) -> VLMClient:
        kwargs.setdefault("extra_body", self.extra_body)
        kwargs.setdefault("stream", self.stream)
        return VLMClient(self.base_url, api_key=self.api_key, **kwargs)

    def effective_model(self, client: Optional[VLMClient] = None) -> str:
        """Model id to send, resolving ``auto`` against the endpoint.

        Single-model servers ignore the field and advertise whatever file they
        were started with, so pinning that path makes config wrong the moment
        the file is renamed. ``auto`` asks, using only ``GET /models``.
        """
        if self.model != "auto":
            return self.model
        if self._resolved_model:
            return self._resolved_model
        from . import observed

        # Asking every process is a round trip for an answer that does not
        # change between runs. A wrong remembered id costs one failed call,
        # after which it is resolved again.
        remembered = observed.model(self.base_url)
        if remembered:
            self._resolved_model = remembered
            return remembered

        models = (client or self.client()).list_models()
        if not models:
            raise VLMError(f"endpoint '{self.endpoint}' advertises no models")
        if len(models) > 1:
            raise VLMError(
                f"endpoint '{self.endpoint}' advertises {len(models)} models "
                f"({', '.join(models[:4])}...); name one explicitly"
            )
        self._resolved_model = models[0]
        observed.remember_model(self.base_url, models[0])
        return self._resolved_model

    def resolved_video_shape(self) -> Optional[str]:
        from . import observed

        return observed.video_shape(self.base_url, self.video_shape_pin)

    def supports_video(self) -> Optional[bool]:
        """True/False once probed, None if never probed."""
        from . import observed

        return observed.supports(self.base_url, "video")

    def carries_audio(self) -> Optional[bool]:
        """Whether this endpoint's video path reads the audio track.

        Decides where speech comes from. An endpoint that expands a clip into
        frames and timestamps hears nothing, so `asr` has to come from the
        platform's subtitle track instead — an extra request worth paying only
        when it is actually needed.
        """
        if self.audio_pin is not None:
            return self.audio_pin
        from . import observed

        return observed.supports(self.base_url, "audio")

    def describe(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self._resolved_model or self.model,
        }


@dataclass
class ResolvedRole:
    """A role, and the ordered endpoints that may serve it."""

    role: str
    requested_role: str
    candidates: List[Binding]
    max_frames: int
    max_tokens: int = DEFAULT_MAX_TOKENS
    warnings: List[str] = field(default_factory=list)
    #: Endpoint that actually served the last successful call, mirroring
    #: upstream's `active_backend`. None until something succeeds.
    active: Optional[Binding] = None

    @property
    def primary(self) -> Binding:
        return self.candidates[0]

    def attempt(self, call: Callable[[Binding], Any]) -> Tuple[Any, List[str]]:
        """Run ``call`` against each endpoint in order until one succeeds.

        Returns the result and the failures collected on the way, which the
        caller is expected to surface: an answer that came from the second
        choice is still an answer, but the reader should know the first one
        was unavailable.
        """
        failures: List[str] = []
        for binding in self.candidates:
            try:
                result = call(binding)
            except VLMError as exc:
                failures.append(f"{binding.endpoint}: {exc}")
                continue
            self.active = binding
            return result, failures
        raise VLMError(
            "every endpoint configured for role '%s' failed:\n  %s"
            % (self.role, "\n  ".join(failures))
        )

    def describe(self) -> Dict[str, Any]:
        """Backend identification for the output contract (fork SPEC §6)."""
        served = self.active or self.primary
        return {
            **served.describe(),
            "role": self.role,
            "configured_endpoints": [b.endpoint for b in self.candidates],
        }


def _vlm_section(config: Any) -> Dict[str, Any]:
    data = getattr(config, "data", None)
    if not isinstance(data, dict):
        data = config if isinstance(config, dict) else {}
    section = data.get("vlm") or {}
    return section if isinstance(section, dict) else {}


def _resolve_api_key(name: str, endpoint: Dict[str, Any], warnings: List[str]) -> Optional[str]:
    """Token for an endpoint, preferring the environment over the file."""
    env_name = endpoint.get("api_key_env")
    if env_name:
        value = os.environ.get(str(env_name))
        if value:
            return value
        warnings.append(
            f"vlm.endpoints.{name}.api_key_env names ${env_name}, "
            f"but that variable is empty or unset"
        )
    inline = endpoint.get("api_key")
    if inline:
        warnings.append(
            f"vlm.endpoints.{name}.api_key holds a literal token; move it to "
            f"an environment variable and use api_key_env instead"
        )
        return str(inline)
    if not env_name:
        warnings.append(
            f"vlm.endpoints.{name} has neither api_key_env nor api_key; "
            f"sending unauthenticated requests"
        )
    return None


def _endpoint_names(spec: Dict[str, Any], vlm: Dict[str, Any]) -> List[str]:
    """Ordered candidates for a role, honouring the user override.

    ``vlm.role_endpoint`` (env ``VLM_ROLE_ENDPOINT``) moves a named endpoint to
    the front. An unknown name is ignored rather than obeyed, so a stale
    override can never hide a working endpoint — upstream's rule verbatim.
    """
    raw = spec.get("endpoints") or spec.get("endpoint")
    names = [str(raw)] if isinstance(raw, str) else [str(x) for x in (raw or [])]

    override = vlm.get("role_endpoint") or os.environ.get("VLM_ROLE_ENDPOINT")
    if override:
        for i, name in enumerate(names):
            if name == override or name.startswith(str(override)):
                names.insert(0, names.pop(i))
                break
    return names


def resolve_role(config: Any, role: Optional[str] = None) -> ResolvedRole:
    """Turn a role name into an ordered list of usable endpoints."""
    vlm = _vlm_section(config)
    roles = vlm.get("roles") or {}
    endpoints = vlm.get("endpoints") or {}
    default_role = vlm.get("default_role")

    if not roles:
        raise VLMError(
            "no vlm.roles configured. Add a vlm section to "
            "~/.agent-reach/config.yaml (see `reach-media doctor`)."
        )

    warnings: List[str] = []
    requested = role or default_role or next(iter(roles))
    chosen = requested
    if chosen not in roles:
        fallback = default_role if default_role in roles else next(iter(roles))
        warnings.append(
            f"unknown role '{requested}'; fell back to '{fallback}'. "
            f"Configured roles: {', '.join(sorted(roles))}"
        )
        chosen = fallback

    spec = roles[chosen] or {}
    if not isinstance(spec, dict):
        raise VLMError(f"vlm.roles.{chosen} must be a mapping")

    candidates: List[Binding] = []
    for name in _endpoint_names(spec, vlm):
        endpoint = endpoints.get(name)
        if not isinstance(endpoint, dict) or not endpoint.get("base_url"):
            warnings.append(
                f"vlm.roles.{chosen} lists endpoint '{name}', which has no "
                f"base_url in vlm.endpoints; skipping it"
            )
            continue
        model = (spec.get("models") or {}).get(name) or spec.get("model") or endpoint.get("model")
        if not model:
            warnings.append(f"no model for endpoint '{name}' in role {chosen}; skipping it")
            continue
        candidates.append(Binding(
            endpoint=name,
            base_url=str(endpoint["base_url"]),
            model=str(model),
            api_key=_resolve_api_key(name, endpoint, warnings),
            extra_body={**(endpoint.get("extra_body") or {}),
                        **(spec.get("extra_body") or {})},
            stream=(spec.get("stream") if spec.get("stream") is not None
                    else endpoint.get("stream")),
            video_shape_pin=(spec.get("video_shape") or endpoint.get("video_shape")),
            input_pin=(spec.get("input") or endpoint.get("input")),
            audio_pin=(spec.get("carries_audio")
                       if spec.get("carries_audio") is not None
                       else endpoint.get("carries_audio")),
        ))

    if not candidates:
        raise VLMError(
            f"vlm.roles.{chosen} has no usable endpoint. Configured endpoints: "
            f"{', '.join(sorted(endpoints)) or '(none)'}"
        )

    return ResolvedRole(
        role=chosen,
        requested_role=requested,
        candidates=candidates,
        max_frames=int(spec.get("max_frames", 16)),
        max_tokens=int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)),
        warnings=warnings,
    )


def default_input(config: Any) -> str:
    """Fallback representation when neither the call nor the role names one.

    ``video`` is the default. Sending a clip as a clip is the honest thing to
    do: it is what the material is, it preserves what happens between the
    frames a sampler would have picked, it carries the audio, and it is one
    request instead of N. Frames are the workaround for endpoints that cannot
    take video, and the ladder falls back to them on its own when a probe says
    so — they are not the normal path.
    """
    value = str(_vlm_section(config).get("default_input") or "video").lower()
    return value if value in ("image", "video") else "video"


def list_roles(config: Any) -> List[str]:
    return sorted((_vlm_section(config).get("roles") or {}).keys())
