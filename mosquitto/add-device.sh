#!/usr/bin/env bash
# Create or reset an MQTT login (PLAN E6.2). Writes mosquitto/passwd (git-ignored).
#   mosquitto/add-device.sh <username> [password]
# Usernames: "backend", "sim", or a device id such as dev-001 (must match its aircraft_id).
# A random password is generated and printed if none is given. Uses the mosquitto image,
# so nothing needs installing; restart or SIGHUP the broker afterwards:
#   docker compose kill -s SIGHUP mosquitto
set -euo pipefail
cd "$(dirname "$0")"
USER_NAME="$1"; PASS="${2:-$(openssl rand -base64 18)}"
touch passwd
if command -v mosquitto_passwd >/dev/null; then
  mosquitto_passwd -b passwd "$USER_NAME" "$PASS"
else
  docker run --rm -v "$PWD:/m" eclipse-mosquitto:2 mosquitto_passwd -b /m/passwd "$USER_NAME" "$PASS"
fi
chmod 644 passwd
echo "$USER_NAME:$PASS"
