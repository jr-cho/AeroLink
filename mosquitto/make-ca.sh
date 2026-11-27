#!/usr/bin/env bash
# Project CA + broker server certificate for MQTT/TLS (PLAN E6.2). Writes mosquitto/certs/ (git-ignored).
#   mosquitto/make-ca.sh [broker-hostname ...]     default: localhost mosquitto
# The CA (ca.crt) is what devices pin. Keep ca.key offline; re-running keeps an existing CA.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs && cd certs
NAMES=("${@:-localhost}")
[[ $# -eq 0 ]] && NAMES+=(mosquitto)

if [[ ! -f ca.key ]]; then
  openssl req -x509 -sha256 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -pkeyopt ec_param_enc:named_curve -nodes -days 3650 \
    -subj "/CN=AeroLink MQTT CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout ca.key -out ca.crt
fi

SAN=$(printf 'DNS:%s,' "${NAMES[@]}"); SAN="${SAN%,},IP:127.0.0.1"
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -pkeyopt ec_param_enc:named_curve -nodes \
  -subj "/CN=${NAMES[0]}" -keyout server.key -out server.csr
# -sha256 explicitly: macOS's LibreSSL defaults to SHA-1, which OpenSSL 3 clients reject ("bad certificate").
openssl x509 -req -sha256 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 \
  -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "$SAN") -out server.crt
rm -f server.csr
chmod 644 ca.crt server.crt server.key   # the container's mosquitto user must read the key
echo "certs written to $(pwd): ca.crt (give to devices), server.crt/key (broker). SAN: $SAN"
