# Media Ladder — reading what the subtitles do not say

**Absence from the subtitles is not absence from the video.**

Technical and gameplay video carries most of its precise content on screen,
not in speech: a benchmark number on a slide, terminal output, the name of a
menu option, the order someone clicked through a UI. A transcript reading
*"latency improved quite a lot"* is not evidence that the number was never
shown.

The ladder works the same way for every site yt-dlp supports. There is nothing
per-platform to learn.

---

## Before you descend: is the answer in text?

The ladder reads what is **only** on screen. It is the most expensive thing
here and it is not a general research tool.

For "what is the best way to do X", a wiki page or a forum thread is faster,
cheaper and usually more complete than any video — and this toolkit reaches
those through other channels (`references/web.md`, `references/social.md`).

Measured on a real task: one wiki fetch produced a full procedure, while
several tier-2 calls over the matching video cost 25 minutes and returned a
handful of screen labels.

**But text goes stale, and video often does not.** For anything that changes
between versions — a patched game, a moving API — the newest source is
frequently the *only* current one, and that is usually a video. Written guides
for the previous version can be detailed and completely wrong. `search`
returns results newest first for this reason, and reports whether each has
subtitles.

So: text first for *what* and *why*; the ladder for *what is actually on
screen now*.

**The ladder is one option among several, not the objective.** If wikis,
forums and the subtitle track answer the question, you are done — reaching for
the screen to prove you can is wasted time and someone else's quota. Descend
when text has actually failed you, and say which part it failed on.

---

## Cost order

```
probe  <  glance  <  read  <  comments  <<  look / grep
```

| rung | what you get | download | risk |
|---|---|---|---|
| `probe` | metadata, chapters, duration, which subtitles exist | none | negligible |
| `glance` | one thumbnail, read by a vision model | none (image CDN) | none |
| `read` | timestamped subtitles, auto-generated included | none | small |
| `comments` | comments, best-effort | none | medium |
| `look` / `grep` | a slice of the video, actually watched | partial | large |

The first four are effectively free. `look` and `grep` download and take
**minutes**. Sieve with the cheap rungs first.

Most questions end at `read`. Descending further is the exception you take
when you have decided the subtitles cannot answer it.

---

## Send the video as video

`--input video` is the **default**, and it is not an optimisation:

- It is what the material is. Sampled frames throw away everything between
  them, including transitions that are the answer.
- It is **one request**, not one per frame. Frame-by-frame means one chance
  per frame to hit a rate limit, and a rejected frame is simply lost.
- Measured on the same window: video mode produced more observations in half
  the wall time, with nothing lost.

Frames (`--input image`) are the **fallback** for endpoints that cannot take
video. The ladder picks based on what was probed and falls back on its own —
you should not normally pass `--input` at all.

### Window length

One request can cover minutes of video. A wide window costs the same as a
narrow one, so **widen the window rather than making repeated calls** — every
call is a separate download and a separate wait.

There is an upper limit per endpoint and you will be told when you exceed it:
the result comes back with no timestamped series and a warning. Halve the
window and retry.

### Timestamps: what "measured" actually means

With `--input image`, a timestamp is the frame's own position in the stream.
With `--input video`, the model reports it, within a few seconds.

**Neither tells you anything about what the screen says.** If a video displays
an in-game clock, a version number or a date, that is *content* — it comes
back in `visible_text` either way, and the frame path has no advantage for
reading it. The distinction only matters when you need to cite *where in the
video* something appeared, and for that a few seconds of slack is fine: a
deeplink is a pointer for checking, not a measurement.

So do not reach for frames because you want precision. Reach for them only
when an endpoint cannot take video.

---

## Speech: `asr` does not come from the video

**These vision models read frames and timestamps. They do not hear audio.**
A clip sent as a clip still yields no speech.

Speech comes from the subtitle track, which is `read` — a separate, cheaper
rung. Tier 2 does not fetch it for you: doing that silently cost an extra
platform request on every call and reliably earned HTTP 429.

- want speech alongside the screen → call `read` as well, or pass
  `--with-subtitles`
- an endpoint that *does* process audio fills `asr` by itself, and the
  warnings say which happened

---

## Running the commands

`reach-media` runs **inside the sandbox container**:

```bash
cd <repo>                       # where docker-compose.yml lives
docker compose up -d
docker compose exec -T agent-reach bash -lc 'reach-media doctor'
```

The container is **disposable and per-task**. Counters, the result cache and
the scratch tree live on tmpfs: restarting it is a clean slate, by design.
Nothing you download accumulates anywhere.

### `doctor` is free; verifying is not

`reach-media doctor` calls no endpoint. It reports what is configured, what
was measured before and when, the download count and the cookie age. Run it
freely.

`reach-media doctor --probe` actually calls the endpoint. **That spends
requests against a quota**, so it is opt-in. Use it when something is failing,
not as a warm-up.

Fallback endpoints are not probed unless you pass `--all-endpoints` — probing
a locally hosted one cold-starts it and parks a model in memory nothing asked
for.

### Long calls must be detached

A tier-2 call takes minutes. Killing `docker compose exec` from the host kills
only the client: **the process inside keeps running**, holds the download
lock, and its output goes nowhere. Measured — an apparently-timed-out call was
still running eleven minutes later, having spent a download for nothing.

```bash
docker compose exec -d agent-reach bash -lc \
  'reach-media look "URL" --at 412 --window 120 > /workspaces/agent-reach/.out.json 2>&1'

until [ -s .out.json ]; do sleep 20; done      # written at the end
```

### The download count is not a bill

Nothing here is metered. The cap guards against the host's IP getting flagged:
downloads are serialised and paced, and bursting is what draws attention.
Time, not quota, is the real cost of a wrong `--at`.

---

## Commands

```bash
reach-media doctor [--probe] [--all-endpoints]

reach-media probe   "URL"                       # tier 0
reach-media read    "URL"                       # tier 1
reach-media glance  "URL" [--role fast]         # tier 0.5

reach-media look "URL" --at 412 --window 120 [--role accurate]
reach-media grep "URL" --for "p99" --at 412 --window 120 [--role ocr]

reach-media full "URL" --yes-i-know-this-is-expensive    # whole video, gated
reach-media clean --all [--cache]
```

That is the whole interface you normally need. `look` and `grep` send the clip
as a clip and pick the endpoint themselves.

<details>
<summary>Frame-sampling flags — only when an endpoint cannot take video</summary>

```
--input image          send sampled stills instead of the clip
--method scene|interval    how to sample
--interval-sec N       cadence for `interval`
--scene-threshold F    sensitivity for `scene`
--max-frames N         cap
--with-subtitles       also fetch the tier-1 track to fill `asr`
```

**Each frame is a separate request.** Twenty frames is twenty chances to hit a
rate limit, and when the quota runs out the work falls through to whatever
endpoint is next — which may be a local model that takes tens of gigabytes of
memory to wake up. Sending the clip whole is one request and does not have
this failure mode.

</details>

Tiers 0 to 1.5 are also plain yt-dlp calls you can run yourself:

```bash
yt-dlp -J --no-warnings "URL" | jq '{id,title,duration,chapters}'
yt-dlp --write-auto-subs --sub-langs en --sub-format vtt --skip-download \
       -o "/tmp/%(id)s" "URL"
```

### `--at` is required, and that is the point

Choosing where to look is your job. You have chapters from `probe` and
timestamps from `read`; use them. Nothing here will guess an interval, and
nothing will decide which rung you should be on.

### Roles, not model names

`--role fast | accurate | ocr | local`. Roles map to endpoints and models in
config; you never name a model.

A role resolves to an **ordered list** of endpoints. If the first one is rate
limited or down, the next one serves and the warnings say so. `backend.vlm`
in the result names the endpoint that actually answered — not the one that was
preferred.

Some endpoints are hosted, which means **the video leaves this machine** and
its rate limits apply. Others are local with no limit. `doctor` shows which is
behind each role. For material that must not leave the host, use a local role.

---

## Output contract

```json
{
  "source": {"extractor": "youtube", "id": "VIDEO_ID", "url": "...",
             "title": "...", "duration": 1830},
  "tier": 2,
  "backend": {"fetch": "yt-dlp 2026.08.19",
              "vlm": {"endpoint": "gemini", "model": "...", "role": "fast",
                      "input": "video",
                      "configured_endpoints": ["gemini", "llamacpp"]}},
  "cost": {"frames_sent": 0, "video_bytes_sent": 5067325,
           "downloaded_seconds": 24, "cached": false,
           "downloads_today": 3, "daily_limit": 200},
  "segments": [
    {"t_start": 412, "t_end": 431,
     "asr": "",
     "visible_text": ["TREATY OF ANKARA", "Turkey was annexed."],
     "scene": "peace conference interface",
     "deeplink": "https://youtu.be/VIDEO_ID?t=412"}
  ],
  "warnings": []
}
```

- `asr` and `visible_text` are **never merged**. Speech and on-screen text are
  different kinds of evidence, and the whole point of the ladder is that the
  second exists when the first is silent.
- Every segment carries `t_start` and a `deeplink`. **Cite the deeplink** for
  anything you take from the screen; it is what lets a reader check you.
- `cost` tells you what the last rung actually cost. Read it before descending
  again.
- Exit code 2 means the work was **refused** (budget spent, tier 3 ungated),
  not that it failed.

### `warnings` is not decoration

Read it every time. Frames that failed, an endpoint that fell through, a
window too long for the endpoint, timestamps that are model-reported, speech
filled from subtitles — all of it appears there. **A short segment list can
mean frames were lost, not that nothing was on screen.**

---

## Reporting what you found

Say where each claim came from: the subtitle track, the screen, or another
channel entirely. When sources disagree, report the disagreement rather than
flattening it — for versioned material the newest source is often right and
the most detailed one is often stale.

If something could not be fetched, say so. "Not available through the tools I
have" is a finding. Inventing a plausible substitute is not.
