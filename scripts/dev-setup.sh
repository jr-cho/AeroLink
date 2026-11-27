#!/usr/bin/env bash
# One-time local setup (PLAN E6.2, E6.7). Idempotent; safe to re-run.
#   scripts/dev-setup.sh [--domain :80]     # --domain sets AEROLINK_DOMAIN (use :80 for scripts/phone-https.sh)
# Does, in order:
#   1. creates .env from .env.example if missing
#   2. fills in values that are still EMPTY in .env (generated secrets, never overwriting a value that is set)
#   3. makes the MQTT CA + broker certificate   (mosquitto/make-ca.sh  -> mosquitto/certs/)
#   4. creates MQTT accounts backend, sim and a test unit (dev-test)   (-> mosquitto/passwd)
#   5. creates the dashboard login (user "admin") and writes its bcrypt hash
# .env is never printed or cat'ed: presence is tested with grep -q, values are written with a small
# python edit. The only secret echoed is the NEW dashboard password, once, because nobody else knows it.
# Needs: openssl, python3, and for the hashes either (mosquitto_passwd | docker) and (caddy | docker).
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env
PASSWD=mosquitto/passwd

[[ "${1:-}" == "--domain" ]] && { DOMAIN="${2:?--domain needs a value}"; } || DOMAIN=""

[[ -f $ENV ]] || { cp .env.example "$ENV"; chmod 600 "$ENV"; echo "created $ENV from .env.example"; }

# has_key KEY: line exists.  has_value KEY: and its value is not empty ('' and "" count as empty).
has_key()   { grep -q "^$1=" "$ENV"; }
has_value() { has_key "$1" && ! grep -Eq "^$1=(''|\"\")?[[:space:]]*$" "$ENV"; }

# set_value KEY VALUE: replace the line if the key exists, else append. Value is passed via the environment.
set_value() {
  KEY="$1" VAL="$2" python3 - "$ENV" <<'PY'
import os, re, sys
path, key, val = sys.argv[1], os.environ["KEY"], os.environ["VAL"]
line = f"{key}={val}"
lines = open(path).read().split("\n")
for i, l in enumerate(lines):
    if l.startswith(key + "="):
        lines[i] = line
        break
else:
    if lines and lines[-1] == "":
        lines.pop()
    lines.append(line)
    lines.append("")
open(path, "w").write("\n".join(lines))
PY
}
set_if_empty() { has_value "$1" || { set_value "$1" "$2"; echo "  set $1"; }; }
secret() { openssl rand -hex 16; }

echo "== .env"
[[ -n $DOMAIN ]] && { set_value AEROLINK_DOMAIN "$DOMAIN"; echo "  set AEROLINK_DOMAIN"; }
set_if_empty POSTGRES_PASSWORD "$(secret)"
set_if_empty DASHBOARD_USER admin
set_if_empty AEROLINK_SITE "https://localhost"
set_if_empty MQTT_TEST_UNIT_ID dev-test
set_if_empty MQTT_BACKEND_USERNAME backend   # an empty value would override the app's default
set_if_empty MQTT_SIM_USERNAME sim

echo "== MQTT certificates"
if [[ -f mosquitto/certs/server.crt ]]; then echo "  keep mosquitto/certs"; else mosquitto/make-ca.sh; fi

echo "== MQTT accounts"
mkpass() {  # mkpass USER PASSWORD -> adds/updates the line in mosquitto/passwd
  if command -v mosquitto_passwd >/dev/null; then
    touch $PASSWD; mosquitto_passwd -b $PASSWD "$1" "$2"
  elif command -v docker >/dev/null; then
    touch $PASSWD; docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 mosquitto_passwd -b /m/passwd "$1" "$2"
  else
    # Fallback without either tool: mosquitto 2.x PBKDF2-SHA512 entry ($7$, 101 iterations, 12-byte salt).
    # Prefer the real tool; scripts/broker-check.sh proves the result against a broker.
    touch $PASSWD
    python3 - "$PASSWD" "$1" "$2" <<'PY'
import base64, hashlib, os, sys
path, user, pw = sys.argv[1:4]
salt = os.urandom(12)
h = hashlib.pbkdf2_hmac("sha512", pw.encode(), salt, 101)
entry = f"{user}:$7$101${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"
rows = [l for l in open(path).read().splitlines() if l and not l.startswith(user + ":")]
open(path, "w").write("\n".join(rows + [entry]) + "\n")
PY
  fi
  chmod 644 $PASSWD
}
account() {  # account USER ENV_KEY: new password only when .env has none (an existing one is never read)
  if has_value "$2"; then
    grep -q "^$1:" $PASSWD 2>/dev/null && echo "  keep $1" \
      || echo "  WARNING: $2 is set in .env but $PASSWD has no '$1' entry; empty $2 in .env and re-run to regenerate both"
  else
    p="$(secret)"; mkpass "$1" "$p"; set_value "$2" "$p"; echo "  created $1"
  fi
}
account backend MQTT_BACKEND_PASSWORD
account sim MQTT_SIM_PASSWORD
account dev-test MQTT_TEST_UNIT_PASSWORD

echo "== dashboard login"
if has_value DASHBOARD_PASSWORD_HASH; then
  echo "  keep DASHBOARD_PASSWORD_HASH"
else
  pw="$(openssl rand -base64 12 | tr -d '/+=')"
  if command -v caddy >/dev/null; then hash="$(caddy hash-password --plaintext "$pw")"
  elif command -v docker >/dev/null; then hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$pw")"
  else hash=""; fi
  if [[ -n $hash ]]; then
    set_value DASHBOARD_PASSWORD_HASH "'$hash'"
    echo "  dashboard login: user 'admin', password: $pw   (shown once; only the hash is stored)"
  else
    echo "  neither caddy nor docker is installed: run later  docker run --rm caddy:2 caddy hash-password --plaintext '<password>'"
    echo "  and put the result in .env as DASHBOARD_PASSWORD_HASH='<hash>' (keep the single quotes)"
  fi
fi
echo "done. Next: scripts/basemap.sh, then docker compose up -d --build"
