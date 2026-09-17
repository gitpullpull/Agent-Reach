# VLM endpoint reference

Measured against the live endpoints, not assumed. Where this file and any
example in the fork spec disagree, **this file wins**.

## Two endpoints, one ordered list

A role resolves to endpoints **in order**. The first that answers, answers;
the rest are there for when it does not. This is upstream's rule for fetch
backends (`channels/base.py`), applied where it had been missing.

| | `gemini` — **primary** | `llamacpp` — fallback |
|---|---|---|
| server | Google, `gemini-3.6-flash` | llama-server, local |
| image | ✅ | ✅ |
| video | ✅ whole clip, one request | ✅ |
| **audio** | ✅ **reads the track** | ❌ frames + timestamps only |
| clip length | no practical limit | bounded by `-c` |
| rate limit | free tier, low | none |
| data leaves the host | **yes** | no |

```
gemini   : https://generativelanguage.googleapis.com/v1beta/openai
           Authorization: Bearer $GEMINI_API_KEY
llamacpp : https://llamacpp.gitpullpull.me/v1
           Authorization: Bearer $LLAMACPP_TOKEN   (~/llama.cpp/deploy/.token)
```

```yaml
vlm:
  endpoints:
    gemini:
      base_url: "https://generativelanguage.googleapis.com/v1beta/openai"
      api_key_env: GEMINI_API_KEY
      carries_audio: true
    llamacpp:
      base_url: "https://llamacpp.gitpullpull.me/v1"
      api_key_env: LLAMACPP_TOKEN
      carries_audio: false
      extra_body:
        cache_prompt: false                      # see cache aliasing
        chat_template_kwargs: {enable_thinking: false}
  roles:
    fast:     {endpoints: [gemini, llamacpp], models: {gemini: gemini-3.6-flash, llamacpp: auto}, max_frames: 8}
    accurate: {endpoints: [gemini, llamacpp], models: {gemini: gemini-3.6-flash, llamacpp: auto}, max_frames: 24}
    ocr:      {endpoints: [gemini, llamacpp], models: {gemini: gemini-3.6-flash, llamacpp: auto}, max_frames: 32}
    local:    {endpoints: [llamacpp], model: auto, max_frames: 24}
  default_role: fast
  default_input: video
```

Note the asymmetry the config records rather than infers: **`carries_audio`**.
Gemini processes the audio track, so `asr` fills itself. llama.cpp expands a
clip into frames plus timestamps and hears nothing — Qwen3.8 does not read
dialogue or lyrics — so when it serves, speech has to come from the subtitle
track, and the ladder redirects to it automatically.

`extra_body` belongs to the endpoint, not to the role: `cache_prompt` and
`chat_template_kwargs` are llama.cpp's and stay on llama.cpp's entry.

Ollama was configured earlier and removed. Its compat layer cannot carry video
at all, it served the same weights, and keeping it put a second 17 GB copy in
VRAM for nothing.

---

## Sending media

| kind | content item |
|---|---|
| image | `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}` |
| video, Gemini | `{"type":"image_url","image_url":{"url":"data:video/mp4;base64,..."}}` |
| video, llama.cpp | `{"type":"input_video","input_video":{"data":"<raw base64>"}}` |

**No two servers agree, and the shape that works on one is an outright error
on the other.** Measured:

| shape | llama.cpp | Gemini |
|---|---|---|
| `input_video.data` (raw base64) | **✅** | 400 `Invalid content part type` |
| `image_url` with an mp4 data URI | 400 `invalid image input` | **✅** |
| `video_url.url`, `video`, `input_file`, `file`, `media_url`, `input_media` | 400 | 400 |

So the shape is an ordered candidate list (`client.VIDEO_PART_SHAPES`), settled
by probing and remembered per `base_url`. `image_url_video_uri` is tried
**last**: a server that takes video through the image part might also accept an
image there and answer about the wrong thing. The probe checks the answer, so a
shape that parses but misreads is never settled on.

Adding a server means adding a list entry. Never a branch at a call site.

---

## ⚠️ The model file name lies

llama.cpp serves a file called **`Qwen3.5-27B-Q4_K_M.gguf`**. It does not
contain Qwen3.5. It is a symlink into Ollama's blob store pointing at the
**qwen3.8:27b** weights:

```
~/models/qwen3.5-27b/Qwen3.5-27B-Q4_K_M.gguf
  -> /usr/share/ollama/.ollama/models/blobs/sha256-f5f1dd89...
$ ollama list
qwen3.8:27b   22130167c4c2   17 GB
```

Anything reading `/v1/models` sees the wrong name. Config therefore uses
`model: auto`, resolved from `GET /models` once and remembered: llama-server
ignores the field entirely (verified with the full path, an arbitrary string,
and the field omitted — all identical), so pinning the path would make config
wrong the moment the file is renamed.

**`GET /models` is not called when the model is named.** A hosted endpoint
listing fifty-eight models tells you nothing about the one you asked for, and
on a metered tier it is quota spent on curiosity.

---

## ⚠️ Prompt-prefix cache aliasing (llama.cpp)

**The most dangerous behaviour found here.** llama-server caches by prompt
prefix and does **not** include the video in the cache key. With
byte-identical prompt text, every clip after the first returned the *first*
one's answer:

```
sent=red  green  blue  yellow   →   answered: Magenta, Magenta, Magenta, Magenta
```

("Magenta" was the first clip ever sent.) Images were unaffected. This is
worse than an unsupported feature: the answer is plausible, so nothing looks
wrong, and it attributes one clip's content to another's timestamp.

Two guards, both verified, both kept:

1. **`client.request_marker()`** — a short unique token appended to every
   prompt. Provider-agnostic, applied to images too. Opaque on purpose:
   anything meaningful there becomes content the model may describe.
2. **`extra_body: {cache_prompt: false}`** — llama.cpp's own switch, carried as
   endpoint config so no provider branch enters the client.

**When adding an endpoint, assume none of this transfers. Re-probe.**

---

## ⚠️ Empty content from a healthy model

Replies carry reasoning in a separate field (`reasoning`, or
`reasoning_content`), billed to the same `max_tokens` and emitted *before* any
content. Too small a budget returns **empty `content` from a model that is
working perfectly** — measured: a one-word colour answer consumed 78
completion tokens, so `max_tokens: 64` came back blank while `512` answered
correctly.

An empty answer is indistinguishable from a model that cannot see. So:

- `DEFAULT_MAX_TOKENS = 16384`, per-role configurable. **Never lower it to
  save time**; it does not truncate gracefully.
- `client.py` raises on empty content, reporting `finish_reason` and
  `completion_tokens` and naming the reasoning field as the likely cause.
- `enable_thinking: false` on llama.cpp: **9.0 s → 2.7-4.6 s** on a dense
  screenshot, and the transcription came back *more* complete, not less.

---

## Streaming is the default

Dense frames were lost to `HTTP 524` — Cloudflare's origin timeout, raised
after the download had already been paid for. A client-side timeout cannot
help: the connection is cut upstream.

`stream: true` fixes it, because the proxy's clock is on the **first byte**.
Once tokens flow the request may take as long as it needs. Verified: an
8-frame window that previously lost 2 completed 8/8.

With a stream, `timeout` bounds the gap *between chunks*, not the whole
request — the honest reading, since a generation still producing tokens is not
stuck.

---

## Other measured constraints

### `--image-min-tokens 1024` makes client-side downscaling pointless

llama.cpp encodes every image to at least 1024 tokens. Measured: 1280×720 and
512×288 of the same frame both produced ~1140 prompt tokens. Downscaling
before sending saves upload bytes and nothing else, while costing OCR
accuracy. Do not add a `--max-width` flag expecting it to buy speed.

### Clip length on llama.cpp is the `-c` flag, nothing more

`--video-fps 1` × `--image-min-tokens 1024` means one second of clip costs
about 1024 tokens of context. At `-c 131072` that is roughly two minutes per
request.

This is a **configured** limit, not a hardware one. Qwen3.8-27B is natively
262K-context and uses linear attention (DeltaNet), so KV does not grow
quadratically and a large context is cheap. `-c 32768` was a leftover from
sharing VRAM with Ollama, and an earlier version of this document wrongly
presented the resulting 32-second window as a ceiling of the system. It is one
flag in `~/llama.cpp/serve-backend.sh`.

Exceeding it does not error: the reply comes back with no timestamped series
and a warning. Halve the window.

### Gemini has no such limit

It takes a whole video in one request and reads the audio with it. For long
material it is not merely faster, it is the only one of the two that can do
the job in a single call.

---

## Backend topology (background; normally irrelevant)

- llama-server on the WSL2 host, `127.0.0.1:8081`, socket-activated via
  `llamacpp.socket` with `StopWhenUnneeded=yes` — it stops when idle and cold
  starts in ~10 s, which is why `GET /models` allows 180 s before calling an
  endpoint unreachable
- nginx `127.0.0.1:11437` does the Bearer check; `client_max_body_size 512m`
- Cloudflare Tunnel publishes it; the tunnel's ingress is **dashboard-managed**,
  so editing `/etc/cloudflared/config.yml` validates and changes nothing
- the ladder's own MCP endpoint is published the same way at
  `reach.gitpullpull.me` → nginx `:11438` → container `:8090` (`deploy/`)

None of this is known to the code. `vlm/client.py` receives a `base_url` and a
token and assumes nothing else — swapping in vLLM or another cloud API is a
config edit.

**Stopping the local model needs sudo**: `systemctl disable --now
llamacpp.socket`. Killing the process does not work; while the socket is
active systemd re-activates it, and each cycle reloads 16 GB.

---

## A note on role indirection

Roles exist so an agent never reasons about which models exist or what compute
is free. That is a design principle, not a workaround for a backend
limitation. Do not justify the indirection in code comments by citing a
runtime's model-swapping behaviour.
