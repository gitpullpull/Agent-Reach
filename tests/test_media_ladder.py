# -*- coding: utf-8 -*-
"""Tests for the media ladder — offline, no endpoint and no downloads.

The behaviours worth pinning here are the ones whose failure would be silent:
a citation that points at the wrong second, a cache that serves a result from
different parameters, a role fallback that hides a typo, and a token budget
that turns a working vision model into an apparent text-only one.
"""

import json
import time
from pathlib import Path

import pytest

from agent_reach.media import cache, extract, frames, ladder, pace, scratch
from agent_reach.vlm import probe as vlm_probe
from agent_reach.vlm import roles
from agent_reach.vlm.client import VLMClient, VLMError


class _FakeConfig:
    def __init__(self, data):
        self.data = data


# ── citations ───────────────────────────────────────────────────────────


def test_deeplink_adds_time_parameter():
    link = ladder.deeplink("https://www.youtube.com/watch?v=abc", 412.7)
    assert "v=abc" in link and "t=412" in link


def test_deeplink_replaces_existing_time_rather_than_appending():
    """A stale t= would cite the wrong second while looking correct."""
    link = ladder.deeplink("https://youtu.be/abc?t=10", 412)
    assert link.count("t=") == 1
    assert "t=412" in link


def test_deeplink_survives_urls_without_query():
    assert ladder.deeplink("https://youtu.be/abc", 5) == "https://youtu.be/abc?t=5"


# ── subtitles ───────────────────────────────────────────────────────────

_VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
hello world

00:00:03.000 --> 00:00:05.000
hello world and more

00:00:05.000 --> 00:00:07.000
<c>a tagged</c> line
"""


def test_parse_vtt_collapses_rolling_captions_keeping_the_original_start():
    """The phrase began at 1s; a deeplink must point there, not at 3s."""
    cues = extract.parse_vtt(_VTT)
    assert [c["text"] for c in cues] == ["hello world and more", "a tagged line"]
    assert cues[0]["t_start"] == 1.0 and cues[0]["t_end"] == 5.0


def test_parse_vtt_drops_exact_repeats():
    doubled = _VTT.replace("hello world and more", "hello world")
    cues = extract.parse_vtt(doubled)
    assert [c["text"] for c in cues] == ["hello world", "a tagged line"]


def test_asr_lookup_only_returns_overlapping_cues():
    cues = [
        {"t_start": 0, "t_end": 5, "text": "before"},
        {"t_start": 10, "t_end": 15, "text": "during"},
        {"t_start": 30, "t_end": 35, "text": "after"},
    ]
    assert ladder._asr_at(cues, 9, 16) == "during"


# ── frame selection: parameters in, no judgement ────────────────────────


def test_extraction_settings_precedence_defaults_then_config_then_flags():
    config = _FakeConfig({"media": {"extraction": {"scene_threshold": 0.9,
                                                   "max_frames": 4}}})
    settings = frames.extraction_settings(config, max_frames=99, method=None)
    assert settings["scene_threshold"] == 0.9   # config beats default
    assert settings["max_frames"] == 99         # flag beats config
    assert settings["method"] == "scene"        # None override keeps the default


def test_cap_spreads_across_the_clip_instead_of_truncating():
    """Keeping the first N would bias every answer toward the interval start."""
    picked = frames.cap([frames.Frame(Path(f"{i}.jpg"), float(i), i) for i in range(100)], 5)
    assert len(picked) == 5
    assert picked[0].t == 0 and picked[-1].t >= 80


def test_cap_is_a_noop_below_the_budget():
    given = [frames.Frame(Path("a.jpg"), 1.0, 0)]
    assert frames.cap(given, 10) == given


def test_unknown_extraction_method_is_rejected_not_guessed(tmp_path):
    with pytest.raises(frames.FrameError):
        frames.extract(tmp_path / "x.mp4", tmp_path / "out", method="magic")


# ── model replies ───────────────────────────────────────────────────────


def test_parse_frame_reply_reads_fenced_json():
    parsed = ladder._parse_frame_reply(
        '```json\n{"visible_text": ["p99: 48ms"], "scene": "terminal"}\n```'
    )
    assert parsed["visible_text"] == ["p99: 48ms"] and parsed["scene"] == "terminal"


def test_parse_frame_reply_promotes_a_bare_string_to_a_list():
    assert ladder._parse_frame_reply('{"visible_text": "solo"}')["visible_text"] == ["solo"]


def test_unparseable_reply_is_flagged_not_filed_as_an_observation():
    """Prose under `scene` would read as something seen in the frame."""
    parsed = ladder._parse_frame_reply("no json here")
    assert parsed["visible_text"] == []
    assert parsed["scene"] == ""
    assert "no json" in parsed["unparsed"]


# ── roles ───────────────────────────────────────────────────────────────


_ROLE_CONFIG = {
    "vlm": {
        "endpoints": {"local": {"base_url": "http://x/v1", "api_key_env": "TEST_KEY"}},
        "roles": {"fast": {"endpoint": "local", "model": "m", "max_frames": 8}},
        "default_role": "fast",
    }
}


def test_unknown_role_falls_back_and_says_so(monkeypatch):
    """A typo must not abort work the agent already paid to download."""
    monkeypatch.setenv("TEST_KEY", "t")
    resolved = roles.resolve_role(_FakeConfig(_ROLE_CONFIG), "typo")
    assert resolved.role == "fast"
    assert resolved.requested_role == "typo"
    assert any("typo" in w for w in resolved.warnings)


def test_api_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "secret-value")
    assert roles.resolve_role(_FakeConfig(_ROLE_CONFIG)).primary.api_key == "secret-value"


def test_missing_env_var_warns_rather_than_silently_sending_no_auth(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    resolved = roles.resolve_role(_FakeConfig(_ROLE_CONFIG))
    assert resolved.primary.api_key is None
    assert any("TEST_KEY" in w for w in resolved.warnings)


def test_role_budget_defaults_wide():
    """Frame description is output-heavy; a narrow budget returns empty text."""
    assert roles.resolve_role(_FakeConfig(_ROLE_CONFIG)).max_tokens >= 16384


def test_no_roles_configured_is_an_error_with_a_prescription():
    with pytest.raises(VLMError, match="vlm.roles"):
        roles.resolve_role(_FakeConfig({}))


# ── the empty-content trap ──────────────────────────────────────────────


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def post(self, *a, **kw):
        return _FakeResponse(self._payload)


def test_empty_content_from_a_reasoning_model_names_the_real_cause():
    """Silence from a healthy model must not read as "cannot see the image".

    Exercises the non-streaming path explicitly; the streaming path has its
    own equivalent below.
    """
    client = VLMClient("http://x/v1", stream=False, session=_FakeSession({
        "choices": [{"message": {"content": "", "reasoning": "thinking..."},
                     "finish_reason": "length"}],
        "usage": {"completion_tokens": 64},
    }))
    with pytest.raises(VLMError, match="max_tokens"):
        client.chat("m", [])


def test_normal_reply_exposes_reasoning_separately():
    client = VLMClient("http://x/v1", stream=False, session=_FakeSession({
        "choices": [{"message": {"content": "Green", "reasoning": "why"},
                     "finish_reason": "stop"}],
    }))
    result = client.chat("m", [])
    assert result.text == "Green" and result.reasoning == "why"


def test_probe_png_is_a_real_png():
    payload = vlm_probe.solid_png((1, 2, 3), size=4)
    assert payload.startswith(b"\x89PNG\r\n\x1a\n") and payload.endswith(b"IEND\xaeB`\x82")


# ── fetch backends: ordered data, never a branch ────────────────────────


def test_backends_default_to_the_declared_candidate_list():
    assert [b.name for b in extract.fetch_backends(_FakeConfig({}))] == ["yt-dlp"]


def test_explicitly_empty_backend_list_is_reported_not_silently_replaced():
    assert extract.fetch_backends(_FakeConfig({"media": {"fetch_backends": []}})) == []


def test_override_moves_a_backend_to_the_front():
    config = _FakeConfig({"media": {
        "fetch_backends": [{"name": "a", "cmd": "a"}, {"name": "b", "cmd": "b"}],
        "fetch_backend": "b",
    }})
    assert [b.name for b in extract.fetch_backends(config)] == ["b", "a"]


def test_unknown_override_is_ignored_so_it_cannot_hide_a_working_backend():
    config = _FakeConfig({"media": {
        "fetch_backends": [{"name": "a", "cmd": "a"}],
        "fetch_backend": "nope",
    }})
    assert [b.name for b in extract.fetch_backends(config)] == ["a"]


def test_no_backend_configured_raises_with_the_config_key_to_fix():
    with pytest.raises(extract.NoBackendError, match="media.fetch_backends"):
        extract.run_backend(["--version"], _FakeConfig({"media": {"fetch_backends": []}}))


# ── cache keys ──────────────────────────────────────────────────────────


def test_cache_key_changes_with_frame_parameters():
    """Same interval, different sampling, is a different observation."""
    a = cache.make_key("v", 2, "fast", {"scene_threshold": 0.3})
    b = cache.make_key("v", 2, "fast", {"scene_threshold": 0.9})
    assert a != b


def test_cache_key_is_stable_for_identical_inputs():
    args = ("v", 2, "fast", {"max_frames": 4})
    assert cache.make_key(*args) == cache.make_key(*args)


def test_cache_roundtrip(tmp_path):
    config = _FakeConfig({"media": {"state_dir": str(tmp_path)}})
    cache.put("k", {"segments": []}, config)
    assert cache.get("k", config)["segments"] == []
    assert cache.purge(config) == 1
    assert cache.get("k", config) is None


# ── pacing ──────────────────────────────────────────────────────────────


def test_cheap_tiers_are_not_paced(tmp_path):
    config = _FakeConfig({"media": {"state_dir": str(tmp_path)}})
    with pace.guard(config, 1.5) as state:
        assert state["paced"] is False
    assert pace.status(config)["downloads_today"] == 0


def test_download_counts_before_it_runs(tmp_path):
    """A failed download still contacted the platform and still counts."""
    config = _FakeConfig({"media": {"state_dir": str(tmp_path)}})
    with pytest.raises(RuntimeError):
        with pace.guard(config, 2):
            raise RuntimeError("download blew up")
    assert pace.status(config)["downloads_today"] == 1


def test_daily_budget_refuses_with_a_reason(tmp_path):
    config = _FakeConfig({"media": {"state_dir": str(tmp_path),
                                    "daily_download_limit": 1}})
    with pace.guard(config, 2):
        pass
    with pytest.raises(pace.PaceError, match="daily download budget"):
        with pace.guard(config, 2):
            pass


def test_pacing_flags_are_configurable():
    config = _FakeConfig({"media": {"pacing": {"limit_rate": "9M"}}})
    args = pace.ytdlp_pacing_args(config)
    assert "9M" in args and "--sleep-requests" in args


# ── disposability ───────────────────────────────────────────────────────


def test_workspace_is_removed_even_when_the_body_raises(tmp_path):
    """An exception mid-extraction must not leave a downloaded video behind."""
    seen = {}
    with pytest.raises(ValueError):
        with scratch.workspace(tmp_path, "job") as work:
            (work / "clip.mp4").write_bytes(b"x")
            seen["path"] = work
            raise ValueError
    assert not seen["path"].exists()


def test_sweep_only_removes_expired_marked_workspaces(tmp_path):
    with scratch.workspace(tmp_path, "fresh"):
        pass
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / ".reach-media-workspace").write_text(str(time.time() - 99999))
    unrelated = tmp_path / "not-ours"
    unrelated.mkdir()

    removed = scratch.sweep(tmp_path, ttl_seconds=3600)
    assert removed == ["stale"]
    assert unrelated.exists()   # never touch what this module did not create


def test_scratch_root_prefers_the_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("REACH_MEDIA_SCRATCH", str(tmp_path))
    assert scratch.scratch_root(None) == tmp_path


# ── video representation: measured per endpoint, never assumed ───────────


def test_video_shapes_are_an_ordered_candidate_list():
    from agent_reach.vlm import client as vlm_client

    assert vlm_client.VIDEO_SHAPE_ORDER[0] == "input_video_data"
    assert set(vlm_client.VIDEO_SHAPE_ORDER) <= set(vlm_client.VIDEO_PART_SHAPES)


def test_input_video_shape_carries_raw_base64_not_a_data_uri():
    """llama-server rejects a data: prefix in input_video.data."""
    from agent_reach.vlm.client import video_part

    part = video_part(b"\x00\x01", shape="input_video_data")
    assert part["type"] == "input_video"
    assert not part["input_video"]["data"].startswith("data:")


def test_video_url_shape_uses_a_data_uri():
    from agent_reach.vlm.client import video_part

    part = video_part(b"\x00\x01", shape="video_url_data_uri")
    assert part["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_unknown_video_shape_is_rejected():
    from agent_reach.vlm.client import video_part

    with pytest.raises(VLMError, match="unknown video shape"):
        video_part(b"", shape="invented")


def test_observed_store_distinguishes_unprobed_from_unsupported(tmp_path, monkeypatch):
    """"never checked" and "checked, no video" need different handling."""
    from agent_reach.vlm import observed

    monkeypatch.setattr(observed, "store_path", lambda: tmp_path / "obs.json")
    assert observed.supports("http://a/v1", "video") is None

    observed.record("http://a/v1", ["image"], None)
    assert observed.supports("http://a/v1", "video") is False

    observed.record("http://b/v1", ["image", "video"], "input_video_data")
    assert observed.supports("http://b/v1", "video") is True
    assert observed.video_shape("http://b/v1") == "input_video_data"


def test_config_pin_overrides_the_measured_video_shape(tmp_path, monkeypatch):
    from agent_reach.vlm import observed

    monkeypatch.setattr(observed, "store_path", lambda: tmp_path / "obs.json")
    observed.record("http://a/v1", ["image", "video"], "input_video_data")
    assert observed.video_shape("http://a/v1", "video_url_data_uri") == "video_url_data_uri"


# ── model resolution: ask the endpoint, don't pin a filename ─────────────


class _ModelsClient:
    def __init__(self, models):
        self._models = models
        self.calls = 0

    def list_models(self):
        self.calls += 1
        return self._models


def _role(model):
    """A single-endpoint binding, for the model-resolution tests."""
    return roles.Binding(endpoint="e", base_url="http://x/v1", model=model)


def test_explicit_model_is_used_verbatim_and_asks_the_endpoint_nothing():
    client = _ModelsClient(["other"])
    assert _role("pinned").effective_model(client) == "pinned"
    assert client.calls == 0


def test_auto_resolves_the_single_advertised_model():
    """Survives the served file being renamed; config needs no edit."""
    resolved = _role("auto")
    client = _ModelsClient(["/models/whatever-27b.gguf"])
    assert resolved.effective_model(client) == "/models/whatever-27b.gguf"
    assert resolved.describe()["model"] == "/models/whatever-27b.gguf"


def test_auto_is_cached_so_one_run_makes_one_lookup():
    resolved = _role("auto")
    client = _ModelsClient(["only"])
    resolved.effective_model(client)
    resolved.effective_model(client)
    assert client.calls == 1


def test_auto_refuses_to_guess_between_several_models():
    with pytest.raises(VLMError, match="name one explicitly"):
        _role("auto").effective_model(_ModelsClient(["a", "b"]))


def test_auto_reports_an_endpoint_advertising_nothing():
    with pytest.raises(VLMError, match="advertises no models"):
        _role("auto").effective_model(_ModelsClient([]))


# ── prompt-prefix cache aliasing ────────────────────────────────────────


def test_request_marker_is_unique_per_call():
    """Identical prompt text let a server serve a previous clip's answer."""
    from agent_reach.vlm.client import request_marker

    assert len({request_marker() for _ in range(50)}) == 50


def test_extra_body_is_sent_verbatim_with_every_request():
    """Server-specific knobs are config data, never a branch in the client."""
    captured = {}

    class _Capturing(_FakeSession):
        def post(self, *a, **kw):
            captured.update(kw["json"])
            return super().post(*a, **kw)

    client = VLMClient(
        "http://x/v1",
        stream=False,
        session=_Capturing({"choices": [{"message": {"content": "ok"}}]}),
        extra_body={"cache_prompt": False},
    )
    client.chat("m", [])
    assert captured["cache_prompt"] is False


def test_endpoint_extra_body_reaches_the_client(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "t")
    config = _FakeConfig({"vlm": {
        "endpoints": {"local": {"base_url": "http://x/v1",
                                "api_key_env": "TEST_KEY",
                                "extra_body": {"cache_prompt": False}}},
        "roles": {"fast": {"endpoint": "local", "model": "m", "max_frames": 8}},
        "default_role": "fast",
    }})
    assert roles.resolve_role(config).primary.client().extra_body == {"cache_prompt": False}


def test_role_extra_body_overrides_the_endpoint_default(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "t")
    config = _FakeConfig({"vlm": {
        "endpoints": {"local": {"base_url": "http://x/v1",
                                "api_key_env": "TEST_KEY",
                                "extra_body": {"cache_prompt": False, "top_p": 1}}},
        "roles": {"fast": {"endpoint": "local", "model": "m", "max_frames": 8,
                           "extra_body": {"cache_prompt": True}}},
        "default_role": "fast",
    }})
    body = roles.resolve_role(config).primary.extra_body
    assert body == {"cache_prompt": True, "top_p": 1}


def test_skipping_the_video_probe_does_not_erase_a_measured_capability(tmp_path, monkeypatch):
    """--no-video-probe means "not checked", never "unsupported"."""
    from agent_reach.vlm import observed, probe as vlm_probe

    monkeypatch.setattr(observed, "store_path", lambda: tmp_path / "obs.json")
    observed.record("http://x/v1", ["image", "video"], "input_video_data")

    ok = vlm_probe.VLMProbeResult("ok")
    monkeypatch.setattr(vlm_probe, "probe_reachable", lambda c: ok)
    monkeypatch.setattr(vlm_probe, "probe_vision", lambda c, m: ok)

    class _C:
        base_url = "http://x/v1"

    vlm_probe.probe_all(_C(), "m", video=False)
    assert observed.supports("http://x/v1", "video") is True
    assert observed.video_shape("http://x/v1") == "input_video_data"


# ── streaming: the proxy's first-byte clock, not latency ────────────────


class _StreamResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class _StreamSession:
    def __init__(self, lines):
        self._lines = lines
        self.body = None

    def post(self, *a, **kw):
        self.body = kw["json"]
        return _StreamResponse(self._lines)


def _sse(*chunks):
    return ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]


def test_streaming_is_the_default():
    from agent_reach.vlm.client import STREAM_BY_DEFAULT

    assert STREAM_BY_DEFAULT is True
    assert VLMClient("http://x/v1").stream is True


def test_streaming_assembles_content_and_usage():
    session = _StreamSession(_sse(
        {"choices": [{"delta": {"content": "Hel"}}], "model": "m1"},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"completion_tokens": 2}, "choices": []},
    ))
    result = VLMClient("http://x/v1", session=session).chat("m", [])
    assert result.text == "Hello"
    assert result.finish_reason == "stop"
    assert result.usage["completion_tokens"] == 2
    assert session.body["stream"] is True


def test_streaming_keeps_reasoning_out_of_the_answer():
    session = _StreamSession(_sse(
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
    ))
    result = VLMClient("http://x/v1", session=session).chat("m", [])
    assert result.text == "answer" and result.reasoning == "thinking"


def test_streaming_empty_content_still_raises():
    session = _StreamSession(_sse(
        {"choices": [{"delta": {"reasoning_content": "..."}, "finish_reason": "length"}]},
        {"usage": {"completion_tokens": 64}, "choices": []},
    ))
    with pytest.raises(VLMError, match="max_tokens"):
        VLMClient("http://x/v1", session=session).chat("m", [])


def test_streaming_can_be_disabled_per_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "t")
    config = _FakeConfig({"vlm": {
        "endpoints": {"local": {"base_url": "http://x/v1", "api_key_env": "TEST_KEY",
                                "stream": False}},
        "roles": {"fast": {"endpoint": "local", "model": "m", "max_frames": 8}},
        "default_role": "fast",
    }})
    assert roles.resolve_role(config).primary.client().stream is False


# ── subtitles: one language at a time, cached ───────────────────────────


def test_subtitles_stop_at_the_first_language_that_works(tmp_path, monkeypatch):
    """Four languages per call meant four platform requests, then HTTP 429."""
    calls = []

    def fake_run(args, config=None, timeout=1800):
        lang = args[args.index("--sub-langs") + 1]
        calls.append(lang)
        if lang == "en":
            (tmp_path / "subs").mkdir(exist_ok=True)
            (tmp_path / "subs" / "vid.en.vtt").write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", encoding="utf-8")
        return None, None

    monkeypatch.setattr(extract, "run_backend", fake_run)
    config = _FakeConfig({"media": {"state_dir": str(tmp_path / "state")}})
    cues, lang = extract.subtitles("u", tmp_path / "subs", config)
    assert lang == "en" and cues[0]["text"] == "hi"
    assert calls == ["en"]      # never asked for ja / zh


def test_subtitles_are_cached_per_video(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, config=None, timeout=1800):
        calls.append(1)
        (tmp_path / "subs").mkdir(exist_ok=True)
        (tmp_path / "subs" / "vid.en.vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", encoding="utf-8")
        return None, None

    monkeypatch.setattr(extract, "run_backend", fake_run)
    config = _FakeConfig({"media": {"state_dir": str(tmp_path / "state")}})
    for _ in range(3):
        cues, lang = extract.subtitles("u", tmp_path / "subs", config, video_id="vid")
        assert lang == "en" and cues
    assert len(calls) == 1      # one fetch, then cache


def test_image_url_video_shape_is_tried_last():
    """A server may accept video through the image part — or misread it there."""
    from agent_reach.vlm import client as vlm_client

    assert vlm_client.VIDEO_SHAPE_ORDER[-1] == "image_url_video_uri"
    part = vlm_client.video_part(b"\x00", shape="image_url_video_uri")
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:video/mp4;base64,")


# ── ordered endpoints: the rule upstream applies to fetch backends ───────

_FALLBACK_CONFIG = {
    "vlm": {
        "endpoints": {
            "hosted": {"base_url": "http://hosted/v1"},
            "local": {"base_url": "http://local/v1"},
        },
        "roles": {"fast": {"endpoints": ["hosted", "local"],
                           "models": {"hosted": "h", "local": "l"},
                           "max_frames": 4}},
        "default_role": "fast",
    }
}


def test_a_role_resolves_to_an_ordered_candidate_list():
    resolved = roles.resolve_role(_FakeConfig(_FALLBACK_CONFIG))
    assert [b.endpoint for b in resolved.candidates] == ["hosted", "local"]
    assert resolved.primary.endpoint == "hosted"
    assert resolved.primary.model == "h"


def test_attempt_falls_through_to_the_next_endpoint():
    """A rate limit is a transient condition, not a verdict on the ladder."""
    resolved = roles.resolve_role(_FakeConfig(_FALLBACK_CONFIG))
    tried = []

    def call(binding):
        tried.append(binding.endpoint)
        if binding.endpoint == "hosted":
            raise VLMError("HTTP 429: rate limited")
        return "answered"

    result, failures = resolved.attempt(call)
    assert result == "answered"
    assert tried == ["hosted", "local"]
    assert resolved.active.endpoint == "local"
    assert failures and "429" in failures[0]


def test_attempt_reports_every_failure_when_all_endpoints_fail():
    resolved = roles.resolve_role(_FakeConfig(_FALLBACK_CONFIG))
    with pytest.raises(VLMError, match="every endpoint"):
        resolved.attempt(lambda b: (_ for _ in ()).throw(VLMError("down")))


def test_describe_names_the_endpoint_that_actually_answered():
    resolved = roles.resolve_role(_FakeConfig(_FALLBACK_CONFIG))
    assert resolved.describe()["endpoint"] == "hosted"   # before any call
    resolved.attempt(lambda b: None if b.endpoint == "local"
                     else (_ for _ in ()).throw(VLMError("x")))
    d = resolved.describe()
    assert d["endpoint"] == "local"
    assert d["configured_endpoints"] == ["hosted", "local"]


def test_override_moves_an_endpoint_to_the_front():
    config = _FakeConfig({"vlm": {**_FALLBACK_CONFIG["vlm"], "role_endpoint": "local"}})
    assert [b.endpoint for b in roles.resolve_role(config).candidates] == ["local", "hosted"]


def test_unknown_override_is_ignored():
    config = _FakeConfig({"vlm": {**_FALLBACK_CONFIG["vlm"], "role_endpoint": "nope"}})
    assert [b.endpoint for b in roles.resolve_role(config).candidates] == ["hosted", "local"]


def test_an_endpoint_missing_a_base_url_is_skipped_not_fatal():
    cfg = {"vlm": {
        "endpoints": {"broken": {}, "good": {"base_url": "http://g/v1"}},
        "roles": {"fast": {"endpoints": ["broken", "good"], "model": "m"}},
        "default_role": "fast",
    }}
    resolved = roles.resolve_role(_FakeConfig(cfg))
    assert [b.endpoint for b in resolved.candidates] == ["good"]
    assert any("broken" in w for w in resolved.warnings)


def test_video_is_the_default_representation():
    """Frames were a workaround for an endpoint that could not take video."""
    assert roles.default_input(_FakeConfig({})) == "video"
    assert roles.default_input(_FakeConfig({"vlm": {"default_input": "image"}})) == "image"


def test_clip_reply_parses_timestamped_observations_with_speech():
    parsed = ladder._parse_clip_reply(
        '[{"t": 3, "visible_text": ["p99: 48ms"], "speech": "latency improved",'
        ' "scene": "terminal"}]'
    )
    assert parsed[0]["t"] == 3
    assert parsed[0]["speech"] == "latency improved"


def test_clip_reply_returns_none_when_it_is_not_a_series():
    assert ladder._parse_clip_reply('{"visible_text": []}') is None


def test_audio_capability_is_recorded_and_distinct_from_video(tmp_path, monkeypatch):
    """Where speech comes from depends on the endpoint, not the provider."""
    from agent_reach.vlm import observed

    monkeypatch.setattr(observed, "store_path", lambda: tmp_path / "obs.json")
    observed.record("http://frames/v1", ["image", "video"], "input_video_data")
    observed.record("http://hears/v1", ["image", "video", "audio"], "image_url_video_uri")

    assert observed.supports("http://frames/v1", "audio") is False
    assert observed.supports("http://hears/v1", "audio") is True
    assert observed.supports("http://unprobed/v1", "audio") is None


def test_binding_reports_audio_capability(tmp_path, monkeypatch):
    from agent_reach.vlm import observed

    monkeypatch.setattr(observed, "store_path", lambda: tmp_path / "obs.json")
    observed.record("http://x/v1", ["image", "video"], "input_video_data")
    binding = roles.Binding(endpoint="e", base_url="http://x/v1", model="m")
    assert binding.supports_video() is True
    assert binding.carries_audio() is False


def test_extract_refuses_to_invent_a_timestamp_it_was_not_given(tmp_path, monkeypatch):
    """A computed time makes a deeplink that points at the wrong moment."""
    out = tmp_path / "frames"
    out.mkdir()
    for name in ("000001.jpg", "000002.jpg", "000003.jpg"):
        (out / name).write_bytes(b"x")

    # ffmpeg reported two times for three written files
    monkeypatch.setattr(
        frames, "_run_ffmpeg",
        lambda args: "pts_time:1.0\npts_time:2.0\n")
    with pytest.raises(frames.FrameError, match="refusing to invent"):
        frames.extract(tmp_path / "clip.mp4", out, method="interval")
