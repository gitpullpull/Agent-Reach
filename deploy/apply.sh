#!/usr/bin/env bash
#
# Publish the container's MCP endpoint through a Cloudflare Tunnel you already
# run, behind an nginx Bearer check.
#
#   REACH_HOSTNAME=reach.example.com sudo -E bash deploy/apply.sh
#
# Set REACH_HOSTNAME to a subdomain of a zone your tunnel serves. NGINX_PORT
# only needs changing if something already listens on the default.
#
# Idempotent: re-running is safe. Every file it touches is backed up first to
# <file>.bak.<timestamp>. Nothing is deleted.
#
# What this does NOT do: start the container. Bring it up first with
#   docker compose up -d reach-mcp
# It listens on 127.0.0.1:8090 only, so publishing it is this script's job.
set -euo pipefail

HOSTNAME_FQDN="${REACH_HOSTNAME:-}"
NGINX_PORT="${REACH_NGINX_PORT:-11438}"
BACKEND="${REACH_BACKEND:-127.0.0.1:8090}"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
CF_CONFIG="/etc/cloudflared/config.yml"
TS="$(date +%Y%m%d-%H%M%S)"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[ok] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo -E bash $0"
[ -n "$HOSTNAME_FQDN" ] || die "set REACH_HOSTNAME to the subdomain to publish, e.g.
  REACH_HOSTNAME=reach.example.com sudo -E bash $0"
[ -f "$DEPLOY_DIR/.token" ] || die "$DEPLOY_DIR/.token is missing. Create one:
  openssl rand -hex 32 > $DEPLOY_DIR/.token && chmod 600 $DEPLOY_DIR/.token"
TOKEN="$(cat "$DEPLOY_DIR/.token")"
[ -n "$TOKEN" ] || die "token file is empty"

backup() { [ -e "$1" ] && cp -a "$1" "$1.bak.$TS" && echo "    backup: $1.bak.$TS"; return 0; }

log "0/4 backend reachable?"
if curl -sf -o /dev/null --max-time 10 -X POST "http://$BACKEND/mcp" \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"apply","version":"1"}}}'; then
    ok "MCP answering on $BACKEND"
else
    die "nothing answering on $BACKEND. Start it: docker compose up -d reach-mcp"
fi

log "1/4 nginx: Bearer-checked reverse proxy on :$NGINX_PORT"
backup /etc/nginx/sites-available/reach.conf
sed -e "s|__TOKEN__|${TOKEN}|" \
    -e "s|__NGINX_PORT__|${NGINX_PORT}|" \
    -e "s|__BACKEND__|${BACKEND}|" \
    "$DEPLOY_DIR/nginx-reach.conf.template" \
    > /etc/nginx/sites-available/reach.conf
chmod 640 /etc/nginx/sites-available/reach.conf
ln -sfn /etc/nginx/sites-available/reach.conf /etc/nginx/sites-enabled/reach.conf
nginx -t || die "nginx config test failed; see /etc/nginx/sites-enabled/reach.conf"
systemctl reload nginx
ok "nginx reloaded"

log "2/4 cloudflared: add $HOSTNAME_FQDN to ingress"
[ -f "$CF_CONFIG" ] || die "$CF_CONFIG not found"

# A tunnel created in the dashboard is *remotely managed*: cloudflared ignores
# the ingress in the local file entirely and serves what the dashboard sends.
# Editing the file then succeeds, validates, and changes nothing -- which is
# worse than failing. Compare the running config against the local one and
# stop before touching anything if they disagree.
RUNNING_INGRESS="$(curl -s --max-time 5 http://127.0.0.1:20241/config 2>/dev/null \
    | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for r in d.get("config",{}).get("ingress",[]):
    h=r.get("hostname")
    if h: print(h)' || true)"
if [ -n "$RUNNING_INGRESS" ]; then
    MISSING=""
    while read -r h; do
        [ -z "$h" ] && continue
        grep -q "hostname: $h" "$CF_CONFIG" || MISSING="$MISSING $h"
    done <<< "$RUNNING_INGRESS"
    if [ -n "$MISSING" ]; then
        warn "This tunnel is REMOTELY MANAGED. The running config serves"
        warn "hostnames that are not in $CF_CONFIG:$MISSING"
        warn ""
        warn "Editing the local file would validate and do nothing. Add the"
        warn "hostname in the dashboard instead:"
        warn "  Zero Trust > Networks > Tunnels > $(awk '/^tunnel:/{print $2}' "$CF_CONFIG")"
        warn "  > Public Hostnames > Add:"
        warn "      subdomain  ${HOSTNAME_FQDN%%.*}"
        warn "      domain     ${HOSTNAME_FQDN#*.}"
        warn "      type       HTTP"
        warn "      URL        localhost:$NGINX_PORT"
        warn ""
        warn "nginx is configured and listening; only the hostname is missing."
        warn "Skipping local ingress and DNS steps."
        SKIP_INGRESS=1
    fi
fi

if [ "${SKIP_INGRESS:-0}" = "1" ]; then
    ok "local ingress skipped (dashboard-managed tunnel)"
elif true; then
if grep -q "$HOSTNAME_FQDN" "$CF_CONFIG"; then
    ok "ingress already contains $HOSTNAME_FQDN (skipped)"
else
    backup "$CF_CONFIG"
    python3 - "$CF_CONFIG" "$HOSTNAME_FQDN" "$NGINX_PORT" <<'PY'
import sys
path, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines(keepends=True)
out, inserted = [], False
for ln in lines:
    # the catch-all stays last: insert immediately before it
    if not inserted and ln.strip() == "- service: http_status:404":
        indent = ln[:len(ln) - len(ln.lstrip())]
        out.append(f"{indent}- hostname: {host}\n")
        out.append(f"{indent}  service: http://localhost:{port}\n")
        inserted = True
    out.append(ln)
if not inserted:
    sys.exit("catch-all (- service: http_status:404) not found; cannot place the entry")
open(path, "w").writelines(out)
print("    ingress entry added")
PY
    cloudflared --config "$CF_CONFIG" tunnel ingress validate \
        || die "ingress validation failed. Restore: $CF_CONFIG.bak.$TS"
    systemctl restart cloudflared
    ok "cloudflared restarted"
fi

fi

log "3/4 DNS: point $HOSTNAME_FQDN at the tunnel"
TUNNEL_ID="$(awk '/^tunnel:/{print $2}' "$CF_CONFIG")"
if cloudflared tunnel route dns "$TUNNEL_ID" "$HOSTNAME_FQDN" 2>&1 | tee /tmp/cf-route-reach.log; then
    ok "DNS route created or already present"
else
    warn "route command failed (it also reports this when the record exists)."
    warn "If missing, add a proxied CNAME by hand:"
    warn "  $HOSTNAME_FQDN -> ${TUNNEL_ID}.cfargotunnel.com"
fi

log "4/4 verify"
echo "--- no auth (401 is correct) ---"
curl -s -o /dev/null -w "    HTTP %{http_code}\n" "http://127.0.0.1:$NGINX_PORT/mcp" || true
echo "--- with auth ---"
curl -s -o /dev/null -w "    HTTP %{http_code}  (%{time_total}s)\n" --max-time 60 \
     -X POST "http://127.0.0.1:$NGINX_PORT/mcp" \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"apply","version":"1"}}}' || true

cat <<EOM

$(printf '\033[1;32mdone\033[0m')

  MCP endpoint : https://$HOSTNAME_FQDN/mcp
  auth         : Authorization: Bearer <deploy/.token>
  local check  : http://127.0.0.1:$NGINX_PORT/mcp
  logs         : docker compose logs -f reach-mcp
  revoke       : write a new token to deploy/.token and re-run this script

  Add it in claude.ai as a custom connector. Put the token in the connector's
  own auth configuration -- not in chat or memory, where it would persist in
  transcripts and could not be revoked per-connector.
EOM
