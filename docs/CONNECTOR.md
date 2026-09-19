# Using this from claude.ai

The container is reachable from the browser as a custom connector. What that
buys you is **the address it runs from**: a residential connection reaches
pages that refuse datacenter IPs, and a machine that can download video and
send it to a vision model.

## Connect

**Settings → Connectors → Add custom connector**

| field | value |
|---|---|
| URL | `https://<your-host>/mcp` — whatever `deploy/apply.sh` published |
| Auth | Bearer token |
| Token | `cat deploy/.token` |

Put the token in the connector's own auth configuration. Not in a chat
message and not in project memory: both persist in transcripts and neither can
be revoked per-connector.

To revoke, write a new token and re-run the deploy script — the nginx reload
takes a second and no dashboard change is needed:

```bash
openssl rand -hex 32 > deploy/.token
sudo bash deploy/apply.sh
```

## Before you connect

The endpoint only answers while the container is up:

```bash
docker compose up -d
```

Nothing else needs to be running. The vision work goes to a hosted model by
default, so the local GPU stays asleep unless a role asks for it.

## What the browser gets

Twelve tools. The server sends its own usage notes at handshake, so the
client already knows the cost order — you do not need to paste instructions
into the conversation.

| tool | cost |
|---|---|
| `doctor` | free, calls nothing |
| `web_search` | free, no key |
| `web_read` | free — **reads sites that block datacenter IPs** |
| `reddit_search`, `reddit_read` | free, logged-in session |
| `search`, `probe`, `read`, `comments` | free, no download |
| `glance` | one thumbnail through a vision model |
| `look`, `grep` | **downloads video, takes minutes** |

`full` (whole video) is deliberately not exposed. Anyone holding the token
could otherwise spend this connection's bandwidth and its standing with the
platform. Run it locally if you need it.

## What it costs

Nothing is billed. The limits that exist are about not getting the host's
address flagged: downloads are serialised and capped per day, and `doctor`
reports where you are against that cap.

The hosted vision model is a free tier with a low rate limit. When it runs
out, the next endpoint in the role's list answers and the result says which
one did.

## Security posture

The container is meant to be **burned per task**. Counters, caches and the
scratch tree are on tmpfs, so restarting it is a clean slate — nothing an
agent did persists, which is the point: a long-lived environment is one an
injected instruction can accumulate in.

What does persist is deliberately small: configuration, the cookie files, and
what has been measured about each endpoint. Those are mounted from the host.
**Treat the bearer token as access to them**, and rotate it if it is ever
pasted anywhere.

## If something does not answer

```bash
docker compose ps                       # is it up
docker compose logs reach-mcp | tail    # did it start
curl -s -o /dev/null -w '%{http_code}\n' https://<your-host>/mcp   # 401 is correct
```

`401` without a token is the healthy answer. `404` means the tunnel is not
routing — the hostname lives in the Cloudflare dashboard, not in
`/etc/cloudflared/config.yml`, and editing that file does nothing.
