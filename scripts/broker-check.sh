#!/usr/bin/env bash
# Broker ACL check (PLAN E6.2, for E7): anonymous is refused and unit A cannot touch unit B's topics.
#   scripts/broker-check.sh                  # against a broker on 127.0.0.1:1883 (compose publishes it on loopback)
#   BROKER_HOST=... BROKER_PORT=8883 BROKER_CA=mosquitto/certs/ca.crt scripts/broker-check.sh   # TLS listener
# Needs mosquitto_pub/mosquitto_sub (brew: /opt/homebrew/bin or /opt/homebrew/sbin) and .env with the
# MQTT_BACKEND_PASSWORD and MQTT_TEST_UNIT_* values made by scripts/dev-setup.sh.
# With LOCAL_BROKER=1 it starts its own mosquitto on :18830 with this repo's config (no docker needed).
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v mosquitto_pub >/dev/null || { echo "mosquitto clients not found" >&2; exit 2; }

# Read only the three values this check needs, into variables; never printed.
getv() { grep "^$1=" .env | head -1 | cut -d= -f2- | tr -d "'\""; }
BACKEND_PW="$(getv MQTT_BACKEND_PASSWORD)"; UNIT_PW="$(getv MQTT_TEST_UNIT_PASSWORD)"; UNIT="$(getv MQTT_TEST_UNIT_ID)"
UNIT="${UNIT:-dev-test}"
[[ -n $BACKEND_PW && -n $UNIT_PW ]] || { echo "run scripts/dev-setup.sh first" >&2; exit 2; }

HOST="${BROKER_HOST:-127.0.0.1}"; PORT="${BROKER_PORT:-1883}"
TLS=(); [[ -n ${BROKER_CA:-} ]] && TLS=(--cafile "$BROKER_CA")
if [[ ${LOCAL_BROKER:-0} == 1 ]]; then
  PORT=18830; TMPD="$(mktemp -d)"
  sed -e "s#/mosquitto/config/#$PWD/mosquitto/#; s#/mosquitto/data/#$TMPD/#" \
      -e '/^listener 8883/,$d' -e 's/^listener 1883/listener 18830 127.0.0.1/' mosquitto/mosquitto.conf > "$TMPD/m.conf"
  mosquitto -c "$TMPD/m.conf" >"$TMPD/log" 2>&1 & BPID=$!
  trap 'kill $BPID 2>/dev/null; rm -rf "$TMPD"' EXIT; sleep 1
fi

fail=0
check() { # check "description" expected(0|1) command...   (0 = must succeed, 1 = must be refused)
  local d="$1" want="$2"; shift 2
  "$@" >/dev/null 2>&1; local rc=$?
  if { [[ $want == 0 && $rc == 0 ]] || [[ $want == 1 && $rc != 0 ]]; }; then echo "PASS $d"; else echo "FAIL $d (exit $rc)"; fail=1; fi
}
denied() { # denied "description" pub-args...: the publish must come back "Not authorized"
  local d="$1"; shift
  if pub -V mqttv5 -q 1 "$@" 2>&1 | grep -qi "not authorized"; then echo "PASS $d"; else echo "FAIL $d"; fail=1; fi
}
allowed() { # allowed "description" pub-args...: no refusal text and exit 0
  local d="$1" out; shift
  if out="$(pub -V mqttv5 -q 1 "$@" 2>&1)" && ! grep -qi "not authorized" <<<"$out"; then echo "PASS $d"; else echo "FAIL $d"; fail=1; fi
}
pub() { mosquitto_pub -h "$HOST" -p "$PORT" ${TLS[@]+"${TLS[@]}"} "$@"; }

OTHER="${UNIT}-other"
check "anonymous publish is refused" 1 pub -t "aerolink/aircraft/$UNIT/telemetry" -m x
check "anonymous subscribe is refused" 1 mosquitto_sub -h "$HOST" -p "$PORT" ${TLS[@]+"${TLS[@]}"} -t '#' -C 1 -W 2
check "wrong password is refused" 1 pub -u "$UNIT" -P wrong -t "aerolink/aircraft/$UNIT/telemetry" -m x
allowed "unit publishes its own telemetry" -u "$UNIT" -P "$UNIT_PW" -t "aerolink/aircraft/$UNIT/telemetry" -m '{}'
# An ACL-denied publish still exits 0 from mosquitto_pub: with MQTT 5 + QoS 1 the broker's PUBACK carries
# reason "Not authorized", which mosquitto_pub prints as a warning. `denied` looks for that text.
denied "unit cannot publish to another unit's telemetry" -u "$UNIT" -P "$UNIT_PW" -t "aerolink/aircraft/$OTHER/telemetry" -m x
denied "unit cannot publish to the sim namespace" -u "$UNIT" -P "$UNIT_PW" -t "aerolink/sim/x" -m x
denied "unit cannot publish on its own read-only topic" -u "$UNIT" -P "$UNIT_PW" -t "aerolink/aircraft/$UNIT/traffic" -m x
allowed "backend can publish to a unit's route-response" -u backend -P "$BACKEND_PW" -t "aerolink/aircraft/$UNIT/route-response" -m '{}'

# Cross-read: unit A subscribes to B's telemetry while backend publishes it; A must receive nothing.
got="$(mosquitto_sub -h "$HOST" -p "$PORT" ${TLS[@]+"${TLS[@]}"} -u "$UNIT" -P "$UNIT_PW" -t "aerolink/aircraft/$OTHER/telemetry" -C 1 -W 3 2>/dev/null &
  sleep 1; pub -u backend -P "$BACKEND_PW" -t "aerolink/aircraft/$OTHER/telemetry" -m secret; wait)"
if [[ -z $got ]]; then echo "PASS unit cannot read another unit's telemetry"; else echo "FAIL unit read another unit's telemetry"; fail=1; fi

exit $fail
