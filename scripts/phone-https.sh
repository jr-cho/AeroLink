#!/usr/bin/env bash
# HTTPS for the phone over Tailscale (PLAN E6.5/E6.7). Terminates TLS with the tailnet's real certificate and
# forwards to Caddy on plain HTTP :80, so no browser warnings and no certificate on the phone.
#   scripts/phone-https.sh start | stop | status
# Prerequisites: AEROLINK_DOMAIN=:80 in .env (scripts/dev-setup.sh --domain :80), `docker compose up -d`,
# HTTPS certificates enabled on the tailnet, and the phone on the same tailnet.
# `start` changes the machine's tailscale serve config; it is never run by CI or other scripts.
# Flags checked against `tailscale serve --help`: --bg, --https=<port>, target http://localhost:80.
set -euo pipefail
HOST="${TS_HOST:-joshuas-macbook-air.tail386e7a.ts.net}"
TS="${TAILSCALE:-$(command -v tailscale || echo /usr/local/bin/tailscale)}"

case "${1:-status}" in
  start)
    "$TS" serve --bg --https=443 http://localhost:80
    echo "Phone URL: https://$HOST/   (pilot pages: https://$HOST/device/<id>?code=<code>)"
    echo "Set AEROLINK_SITE=https://$HOST in .env so pairing QR codes point here."
    ;;
  stop)
    "$TS" serve reset   # clears ALL serve config on this machine (this script is its only user here)
    ;;
  status)
    "$TS" serve status
    ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
