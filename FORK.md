# Media Ladder — a fork of Agent Reach

> Agent Reach reads a video's subtitles. This fork watches the video.

[Agent Reach](https://github.com/Panniantong/Agent-Reach) installs and
diagnoses the tools an AI agent needs to read the internet. It ships yt-dlp —
which supports 1800+ sites — and uses it for one thing: pulling subtitles.

This fork adds the axis that was missing. Not more platforms; a **ladder of
representations** that works identically on every extractor yt-dlp already
supports.

## Why

Subtitles do not carry what is on screen. In technical and gameplay video
that is where the precise content lives: a benchmark number on a slide,
terminal output, a menu label, the order someone clicked through a UI.

An agent that reads *"latency improved quite a lot"* and concludes the number
was never given will answer confidently and wrongly. **Absence from the
subtitles is not absence from the video.**

## The ladder

```
probe  <  glance  <  read  <  comments  <<  look / grep
```

| rung | what you get | download |
|---|---|---|
| `probe` | metadata, chapters, which subtitles exist | none |
| `glance` | the thumbnail, read by a vision model | none |
| `read` | timestamped subtitles | none |
| `comments` | comments | none |
| `look` / `grep` | the video itself, watched | partial |

The first four cost nothing. The last two take minutes. Most questions end at
`read`; descending further is the exception you take when you have decided the
subtitles cannot answer it.

**Nothing here decides for you.** `--at` is required because choosing where to
look is the agent's job — the same boundary upstream draws when it refuses to
read on the agent's behalf.

## What it does not do

- **Summarise videos.** Not a transcription service.
- **Replace subtitles.** If they answer the question, you are done.
- **Guess.** No automatic descent, no content classification, no inferred
  intervals, no invented timestamps.
- **Hardcode a provider.** Fetch backends and vision endpoints are ordered
  lists in config. A branch on a site name is the point at which this design
  has failed.

## Two reasons to run it yourself

**Your address.** A residential connection reaches sites that refuse
datacenter IPs outright. That is not a workaround; for some sources it is the
only way in.

**Your models.** Vision goes to whatever OpenAI-compatible endpoint you point
it at — hosted, local, or a list of both with automatic fallback. No model
runtime is bundled.

## Install

```bash
git clone <your fork> && cd Agent-Reach
cp .env.example .env        # fill in the endpoint tokens you have
docker compose up -d
docker compose exec -T agent-reach bash -lc 'reach-media doctor'
```

The container is **disposable by design**: burn it per task. Counters, caches
and the scratch tree are on tmpfs, so a restart is a clean slate — a
long-lived environment is one an injected instruction can accumulate in.

## From a browser

`deploy/apply.sh` publishes the ladder as an MCP endpoint behind a Bearer
check, so a client with no shell can use it. See
[docs/CONNECTOR.md](docs/CONNECTOR.md).

## Read before changing anything

- [docs/DESIGN.md](docs/DESIGN.md) — what upstream actually is, which
  decisions are load-bearing, and the wrong turns taken getting here
- [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) — measured endpoint behaviour.
  Two servers disagree on almost everything; the traps are documented because
  each one cost real time
- [agent_reach/skill/references/media-ladder.md](agent_reach/skill/references/media-ladder.md)
  — what an agent needs to know

## Fork hygiene

Upstream files changed: **7, additive only**. No upstream code is touched.
Everything else is new files. Verify with:

```bash
git diff --stat upstream/main
```

If that list grows, question the change.

MIT, as upstream.
