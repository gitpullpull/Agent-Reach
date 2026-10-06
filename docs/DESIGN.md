# Media Ladder — design record and verification log

Fork of [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach)
(MIT). Base: upstream `da5044d`, v1.5.0. Branch `media-ladder`.

This document exists so that whoever changes this code next — including a
future agent — can tell which decisions are load-bearing and which are
incidental. Read it before editing anything under `agent_reach/media/` or
`agent_reach/vlm/`.

---

## 1. The problem

Information that is on screen but not in the subtitles.

In technical video this is most of the value: benchmark numbers on a slide,
terminal output, UI labels and the order they are clicked, the contents of an
editor. A transcript reading *"latency improved quite a lot"* is not evidence
that the number was never shown.

An agent that treats "not in the subtitles" as "not in the video" answers
confidently and wrongly. Fixing that is the performance goal of this fork.
Everything else is subordinate to it.

The second problem is reachability. This runs on a home WSL2 host, so it has a
residential IP. Content that refuses datacenter IPs outright — the reason
Reddit is unreliable from cloud infrastructure — is reachable from here.
Slowness is acceptable; obtaining the material at all is the point. This is
why speed is an explicit non-goal.

---

## 2. What upstream actually is

Read from source before writing anything. The summary below cites the file and
line so it can be re-checked after a rebase.

### 2.1 It is not a wrapper

> `CLAUDE.md:5` — "Positioning: installer + doctor + config tool. **NOT a
> wrapper** — after install, agents call upstream tools directly."
>
> `core.py:7` — "call the upstream tools directly — no wrapper layer needed."

Upstream installs tools, diagnoses them, and *documents the commands* in
`skill/references/*.md`. It does not read on the agent's behalf.

### 2.2 The channel contract is two methods, not four

`channels/base.py:40` declares exactly one abstract method, `can_handle(url)`,
plus `check(config)` at `:61`. There is no `read()` or `search()` in the
contract — `CLAUDE.md` claims there is, but the code disagrees, and the code
wins. Only `web.py` and `v2ex.py` define such methods, for their own reasons.

**Consequence for this fork:** `channels/youtube.py` is 141 lines of health
check (`:50`) and contains no fetching code at all. The fork spec's plan to
"replace youtube.py with delegation to media/" was impossible — there was
nothing to delegate. See §4.1.

### 2.3 Ordered backend routing already exists

`channels/base.py:12-22`:

> "`backends` is an ORDERED candidate list: backends[0] is the preferred
> backend, the rest are fallbacks. **"Switching backends" for a platform means
> reordering this list — not rewriting code.**"
>
> "`shutil.which()` alone is NOT proof of health."
>
> "Users can force a backend with config key `<channel>_backend`
> (or env var `<CHANNEL>_BACKEND`); `ordered_backends()` applies it."

This is the fork spec's "never hardcode a provider" principle, already
implemented upstream. `media/extract.py` reuses these exact semantics rather
than inventing a parallel mechanism.

### 2.4 Probing is real execution

`probe.py:47` `probe_command()` actually runs the command and classifies
`missing` / `broken` / `timeout` / `error`, with `reinstall_hint()` (`:38`)
supplying the prescription. The distinction it protects against — a stale venv
shim that passes `which()` but cannot exec — is the same *class* of failure as
a text-only model that answers `/chat/completions` and silently drops the
image. `vlm/probe.py` is the vision-side counterpart.

### 2.5 doctor is channel-driven

`doctor.py:16` `check_all()` iterates `get_all_channels()` and nothing else.
A channel that raises degrades to `status="error"` instead of taking the
report down. `format_report()` (`:57`) groups by tier.

**Consequence:** there is no hook for a non-channel capability. See §4.2.

---

## 3. Principles this fork adds

1. **The ladder is independent of the fetch mechanism.** Fetching is
   swappable; the ladder is not. One ladder serves every extractor, which is
   why this is a re-routing of what upstream already installs, not a feature
   added per site.

2. **No provider is hardcoded, and that applies to the VLM too.** Backends
   are an ordered list read from config; the code tries the list in order and
   does nothing else. **A branch on a site name or a tool name is the point at
   which this design has failed.**

   This principle was implemented for fetch backends and violated for vision:
   a role bound to exactly one endpoint, so a rate-limited hosted model lost
   the work outright while a local one sat idle. Roles now resolve to an
   ordered list, the served endpoint is reported rather than the preferred
   one, and failures on the way surface as warnings.

3. **Send the material as what it is.** A clip goes as a clip: one request
   instead of N, nothing discarded between sampled frames, and the audio
   carried where the endpoint reads it. Frame sampling was a workaround for an
   endpoint that could not take video and is now the fallback, chosen from
   what was probed.

4. **Never fabricate a value to avoid reporting a gap.** A computed timestamp
   makes a deeplink that points where the reader will not find the claim;
   prose filed under `scene` reads as something seen. Both existed here and
   both were removed. Report the gap.

5. **A health check must not spend anything.** `doctor` reports what is
   configured and what was measured before, with its age, and calls nothing.
   Verifying is `--probe`, opt-in, because every probe bills a quota — and a
   check that bills you is a check people stop running. Fallback endpoints are
   left asleep; waking a locally hosted one parks a model in VRAM nobody asked
   for.

6. **The fork is thin; the agent judges.** Which video, how far to descend,
   which interval, which role — all the agent's. The fork executes and reports
   cost honestly. Upstream refuses to *read* for the agent; this fork refuses
   to *choose* for it. Same boundary, one level down.

7. **Roles, not model names.** The agent asks for `fast` / `accurate` / `ocr`
   and never learns which models exist. This is a design principle — keeping
   the agent reasoning about required fidelity, not about loaded weights — and
   **not** an accommodation of any backend limitation. Do not justify it in
   comments by citing a runtime's model-swapping behaviour; the backend here
   is Ollama, which has no such constraint.

8. **Capabilities are probed, never declared.** Whether a model accepts images
   or video is established by sending one. Never write "this model supports
   vision" in code.

---

## 4. Where the fork spec broke against upstream reality

Recorded because these were real decisions, and someone will otherwise
re-derive them.

### 4.1 `channels/youtube.py` is not edited

The spec asked for delegation to `media/`, then for a `CAPABILITIES` list.
Neither happened:

- There is no fetching code in `youtube.py` to delegate (§2.2).
- The ladder rides yt-dlp's extractor set directly, so it never needs to ask a
  channel whether a URL is supported. A `CAPABILITIES` declaration would add a
  rebase conflict surface for no caller.

**Result: zero edits to `channels/`.** If a future need arises, adding
`CAPABILITIES = [...]` is a three-line change.

### 4.2 `doctor.py` is not edited either

The spec wanted media rows inside `agent-reach doctor`, but also forbade
registering media/vlm in `channels/__init__.py`. Those are mutually exclusive
(§2.5). Resolved as a separate `reach-media doctor`, which:

- reports everything the spec's §9 asked for,
- edits no upstream file,
- cannot conflict when upstream changes its report format.

### 4.3 `media.fetch_backends` follows upstream semantics

The spec said an unset backend list must not fall back to a hardcoded default.
Upstream's own pattern declares the candidate list in code
(`youtube.py:42` — `backends = ["yt-dlp"]`) and lets config *reorder* it.
This fork follows upstream:

| config state | behaviour |
|---|---|
| key absent | `DEFAULT_FETCH_BACKENDS` (the declared candidate list) |
| explicitly `[]` | **no backends**; doctor prescribes, nothing is substituted |
| `media.fetch_backend: name` | moves that entry to the front; unknown names ignored |

The distinction that matters is preserved: an empty list is honoured, never
silently replaced.

### 4.4 Tiers 0–1.5 are documented, not wrapped

They are single yt-dlp invocations. Wrapping them would make this a wrapper
for no gain (§2.1), so they live in
`agent_reach/skill/references/media-ladder.md`. Only the rungs an agent cannot
express as one shell command are code:

| rung | why it needs code |
|---|---|
| 0.5 `glance` | thumbnail → vision model |
| 2 `look` / `grep` | download pacing, frame extraction, per-frame VLM calls |
| 3 `full` | same, gated |

---

## 5. Module map

```
agent_reach/vlm/
  client.py    OpenAI-compatible chat. /chat/completions and /models, nothing
               else. Must never branch on server implementation or model name.
  roles.py     role → (endpoint, model, max_frames, max_tokens). Unknown role
               falls back to default_role WITH a warning, never an error.
  probe.py     Real capability probing. Random-colour PNG; video probe walks
               the shape candidates. Writes results to observed.py.
  observed.py  Measured endpoint capabilities, keyed by base_url. Persistent
               on purpose: they describe the endpoint, not the task, and
               re-measuring costs requests against a quota.

agent_reach/media/
  ladder.py    Thin control flow + the output contract. No descent logic.
  extract.py   Ordered fetch backends, tier 0/1/2/3 fetching, VTT parsing.
  frames.py    Extraction primitives. Deliberately stupid — see §6.
  pace.py      Lockfile serialisation + daily budget. Not promptable.
  cache.py     Content-addressed, structured results only. No media bytes.
  scratch.py   Disposable workspace + TTL sweep.

agent_reach/media_cli.py                     reach-media entry point
agent_reach/integrations/mcp_http.py         the ladder over MCP, for clients
                                             with no shell (browser chat).
                                             Upstream's mcp_server.py untouched.
deploy/                                      nginx template + apply.sh for
                                             publishing the MCP endpoint
agent_reach/skill/references/media-ladder.md tiers 0–1.5 + output contract
tests/test_media_ladder.py                   36 offline tests
tests/eval/                                  ladder regression harness
```

### Upstream footprint

**3 files, 13 insertions, 1 modification — all additive.**

```
agent_reach/skill/SKILL.md      +2    one routing row, one reference row
agent_reach/skill/SKILL_en.md   +2    same
pyproject.toml                  +9/-1 reach-media script, `media` extra
```

`version`, `requires-python`, `build-backend` and every core dependency are
untouched. Verify with `git diff --stat upstream/main` before any commit; if
that list grows, question the change.

> Context: the previous implementation replaced `pyproject.toml` wholesale —
> version `1.5.0`→`0.1.0`, `requires-python` 3.10→3.12, hatchling→setuptools,
> dropped `loguru`/`rich`/`feedparser`/`python-dotenv`, and repointed the
> `agent-reach` entry point from `cli:main` to `doctor:main`, which removed the
> entire upstream CLI. It also had no `.git`, so it could never be rebased. That
> is the failure mode this section exists to prevent.

---

## 6. What `frames.py` must never become

It turns a clip into images using exactly the parameters given. It must never
grow:

- classification of a video as slides / terminal / UI / talk
- "scene detection isn't working here, switch to fixed interval"
- inference of interesting intervals from subtitle gaps
- any opinion about which part of a video is worth looking at

Scene detection genuinely fails on terminal recordings — too few pixels
change. That is *why* both methods are flags. When `--method scene` returns
nothing, the tool says so; silently retrying with `interval` would hide that
the agent's parameter choice was wrong for this material.

**If evaluation later shows agents descending badly, the fix is the skill
reference or the information the CLI returns — not heuristics here.** Adding
"subtitles are silent for N seconds in this range" to tier-1 output is
correct. The fork guessing "look here" is not.

---

## 7. Verified against the live environment

All results measured 2026-09-06 inside the sandbox container.

### 7.1 Results

| check | result |
|---|---|
| Test suite | **659 passed, 0 failed** (586 upstream + 73 fork) |
| Fork tests | 0.2 s, **zero network calls** (verified by blocking `socket.connect`) |
| Upstream CLI intact | `agent-reach v1.5.0`, all 15 channels check |
| Gemini | image ✅, video ✅ (whole clip, one request), **audio ✅** |
| llama.cpp | image ✅, video ✅, **audio ❌** (frames + timestamps) |
| Tier 2, video mode | 40 s window in ~2 min via Gemini; 24 s window in ~97 s locally |
| `doctor` | **0.4 s, zero API calls** |
| Disposability | counters/cache/scratch on tmpfs; a restart is a clean slate |
| Reddit | `rdt-cli` with cookies; `--subreddit` is mandatory in practice |

### 7.2 The representation is a property of the server

Ollama's compat layer rejected every video shape offered. llama.cpp accepts
`input_video.data` (raw base64) and rejects an mp4 in `image_url`. Gemini does
the exact opposite. Nothing about this is inferable from what a server
advertises, so the shape is an ordered candidate list settled by probing and
remembered per `base_url`.

`observed.supports()` returns `None` for "never probed", distinct from `False`
for "probed, unsupported" — collapsing them would make an unprobed endpoint
look broken.

### 7.3 Prompt-prefix cache aliasing — the most dangerous finding

llama-server caches by prompt prefix and does not include the video in the
key, so every clip after the first returned the first one's answer. Images
were unaffected. Plausible, silent, and it attributes one clip's content to
another's timestamp — precisely the failure this fork exists to prevent.

Fixed by a per-request marker (provider-agnostic) plus `cache_prompt: false`
carried as endpoint config. Details and measurements in `ENVIRONMENT.md`.

### 7.4 Audio is per-endpoint, and it decides where speech comes from

Gemini reads the audio track. llama.cpp expands a clip into frames and
timestamps and hears nothing — Qwen3.8 does not read dialogue or lyrics.

So `carries_audio` is declared per endpoint, and when a non-hearing endpoint
serves a clip whose speech came back empty, the ladder fetches the subtitle
track and says it did. Subtitles are tier 1: a separate, cheaper rung, not a
precondition of tier 2.

**An earlier version of this document claimed a clip carries its audio as a
general fact. That is true of Gemini and false of llama.cpp.**

### 7.5 Two wrong diagnoses, recorded so they are not repeated

**"VRAM saturation explains the slowdown."** On 2026-09-07 llama.cpp measured
8.8-23 tok/s against 40-43 at startup, and this document blamed KV growth
against a full card, prescribing `--parallel 1`, `-fa` and quantised KV. After
a reboot the same server, same config, same workload ran at 44-48 tok/s and
did not decay over eight consecutive image requests. Co-resident Ollama costs
only 1.16×. **None of those changes were needed.** Three variables had been
correctly isolated and exonerated; a fourth was then asserted from a plausible
correlation instead of tested against a clean baseline. **Take the clean
baseline first.**

**"The clip length ceiling is ~32 seconds."** It was `-c 32768`, one flag,
left over from sharing VRAM with Ollama. The model is natively 262K-context
with linear attention, so a large context is cheap. Presenting a configured
value as a property of the system sent the design in the wrong direction for
several rounds.

## 8. Bugs found and fixed during implementation

| bug | how it surfaced | fix |
|---|---|---|
| Rolling auto-caption dedup was backwards | fork unit test | later cue supersedes earlier, original `t_start` kept (the deeplink must point where the phrase began) |
| Last tier-2 segment had `t_end == t_start` | live tier-2 run | `t_limit` extends the final frame to the window end |
| Container `/tmp` mounted `noexec` | 6 upstream tests failing | `tmpfs: /tmp:...,exec`. **Pre-existing, not caused by this work** — reproduced on pristine `upstream/main` |
| `apt-get` failing in image build | build failure after I removed a line | restored `rm -f /etc/apt/sources.list.d/yarn.list`; the base image ships a yarn source whose signing key is absent. **Load-bearing, not tidying** |
| Eval item mislabelled `required_tier: 2` | the harness itself flagged it | question changed to one the thumbnail cannot answer |
| Video mode reported `cost.frames_sent: 0` and nothing else | live video run | added `cost.video_bytes_sent` so cost is honest across input modes |
| Video answers were stale across calls | probe expected `red`, got the previous run's `Magenta` | request marker + `cache_prompt: false` — see §7.3 |
| **Fabricated frame timestamps** | design audit | `frames.extract` computed a time when ffmpeg reported fewer than it wrote. A computed time makes a deeplink that points where the reader will not find the claim. It now raises rather than inventing |
| Unparseable replies filed as observations | design audit | prose went into `scene`, indistinguishable from something seen. Now kept under `unparsed` and surfaced in `warnings` |
| Tier 2 silently did tier 1's job | HTTP 429 on every call | subtitle fetch removed from tier 2; `--with-subtitles` makes it explicit |
| One endpoint per role, no fallback | Gemini 429 lost a frame outright | roles resolve to an **ordered list**; the served endpoint is reported, failures surface as warnings |
| `doctor` spent quota on every run | user challenge | reports by default, `--probe` to verify, fallbacks left asleep |
| `GET /models` on every invocation | design audit | remembered per endpoint; skipped entirely when the model is named |
| Tier-0 metadata refetched on every call | design audit | cached, so a cache *hit* no longer costs a platform request |
| `--no-video-probe` erased a measured capability | video mode refused after a doctor run | skipping is not a negative; a prior observation survives |
| Stale host install shadowed everything | benchmark subagent hit `ImportError` | the previous fork's editable install and `agent-reach` shim were still on the host; removed |

---

## 9. Environment traps

- **Never infer a capability from a model name.** The served file is called
  `Qwen3.5-27B` and holds qwen3.8 weights; the same weights read video through
  llama.cpp and not through Ollama. Probe, or declare it in config where the
  answer is published and stable.
- **llama-server stops after 15 min idle**; the next request blocks ~10 s on
  model load, which is why `GET /models` allows 180 s before calling an
  endpoint unreachable. Video calls use `VIDEO_TIMEOUT = 900`; never shorten
  it to "fail fast" — a timeout mid-generation wastes the upload too.
- **Killing llama-server does not stop it.** While `llamacpp.socket` is
  active systemd re-activates it, and every cycle reloads 16 GB. Stopping it
  needs `sudo systemctl disable --now llamacpp.socket`.
- **The Cloudflare tunnel's ingress is dashboard-managed.** Editing
  `/etc/cloudflared/config.yml` validates, restarts cleanly, and changes
  nothing — a silent no-op. `deploy/apply.sh` now compares the running config
  against the file and stops before touching it.
- **llama-server caches by prompt prefix and ignores the video in the key.**
  See §7.3. Both guards must stay.
- **Cookies**: currently unconfigured; tier 2 works without them. Expiry
  surfaces as *"Sign in to confirm you're not a bot"*, which looks unrelated
  — this is why `doctor` reports cookie age. Use a throwaway account.
- **`.env`** holds the endpoint tokens (`GEMINI_API_KEY_2`, `GEMINI_API_KEY`,
  `LLAMACPP_TOKEN`), is gitignored, `chmod 600`, and is read automatically by
  `docker-compose.yml`. `.env.example` lists them with no values.
- Config uses `api_key_env` — **the token value is never written to
  `config.yaml`**. Note that upstream's `Config.to_dict()` masks only
  top-level keys, so a nested inline token would not be redacted on print.

---

## 10. State: what persists and what does not

The container is a tool invoked per task, not a service. State that outlives
it is state nobody asked for, and a cache that grows across unrelated tasks is
debris.

| location | contents | lifetime |
|---|---|---|
| `/tmp/reach-media` (tmpfs) | downloaded video, extracted frames | dies with the container |
| `/tmp/reach-state` (tmpfs) | download counter, lock, result cache | dies with the container |
| `~/.agent-reach/config.yaml` | configuration | persists |
| `~/.agent-reach/cookies`, `rdt-cli/` | credentials | persists |
| `~/.agent-reach/vlm-observed.json` | probed endpoint capabilities | persists |

The last row is the one distinction worth stating: what an endpoint accepts
describes the endpoint, not the task, and re-establishing it costs real
requests. Everything the task produced is disposable; what was learned about
the world is not.

Video bytes and frames are deleted the moment extraction finishes, on the
error path too — re-fetching an interval is cheaper than storing media, and
more honest, since a cached frame could not be re-checked against the source.
No daemon: every run sweeps expired workspaces on a TTL, so a long-lived
container does not accumulate debris either.

## 11. Not done

- **Eval dataset has 1 item, not 12.** It is a harness smoke test. The 4 types
  × 3 videos need watching videos and confirming `required_tier` by hand;
  invented ids and answers would produce a regression suite that cannot fail.
  Procedure in `tests/eval/README.md`.
- **`under_descent` / `over_descent` are `null`.** They describe an agent's
  descent decisions and the harness has no agent in the loop. One benchmark
  run with a fresh subagent produced a genuine under-descent — it reported
  that a video's auto-captions collapsed after an hour and concluded the
  information did not exist, rather than descending to read the screen. The
  fix went into the skill reference, not into heuristics here.
- **Gemini's ability to take a YouTube URL directly is unconfirmed.** Of four
  content shapes tried, three returned 400 `Invalid content part type` and
  `image_url` with the URL returned 429 — quota, meaning the type was
  accepted. If it works, tier 2 stops needing a download at all, and the rung
  definitions change.
- **README pitch** not applied; it would be another upstream file edit.
- **Rebase not yet exercised.** The footprint in §5 is what keeps it cheap.
