#!/bin/bash
# Start the connected tvOS qualification fixture on macOS, bounded and hidden.
#
#   bash start_connected_fixture.sh <simulator-udid> <scratch-dir>
#
# What it does, in order:
#   1. creates a throwaway CA and a localhost server certificate under <scratch-dir>
#      (mode 700). Nothing here is ever committed: no certificate or key lives in
#      the repository.
#   2. starts connected_fixture.py as a background child, stdout/stderr to
#      <scratch-dir>/server.log, PID to <scratch-dir>/server.pid.
#   3. polls /qa/health until the fixture answers, then installs the CA into the
#      simulator keychain so the unsigned app trusts the loopback origin.
#   4. prints the origin and the stop command, and exits.
#
# It never signs, installs or launches the app: that is a separate authorized
# step. A failure prints the server log and exits non-zero.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: bash start_connected_fixture.sh <simulator-udid> <scratch-dir>" >&2
  exit 64
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "this launcher needs macOS: it uses openssl key generation and xcrun simctl" >&2
  exit 69
fi

device="$1"
scratch="$2"
here="$(cd "$(dirname "$0")" && pwd)"
fixture="$here/connected_fixture.py"
port="${OMAKASE_FIXTURE_PORT:-8854}"

[[ -f "$fixture" ]] || { echo "missing fixture: $fixture" >&2; exit 66; }

mkdir -p "$scratch"
chmod 700 "$scratch"
mkdir -p "$scratch/tls"
chmod 700 "$scratch/tls"

if [[ ! -s "$scratch/tls/server.crt" || ! -s "$scratch/tls/server.key" ]]; then
  cat > "$scratch/tls/ca.conf" <<'EOF'
[req]
distinguished_name = name
x509_extensions = ca
[name]
CN = Omakase TV Fixture CA
[ca]
basicConstraints = critical,CA:true,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 \
    -config "$scratch/tls/ca.conf" -subj '/CN=Omakase TV Fixture CA' \
    -keyout "$scratch/tls/ca.key" -out "$scratch/tls/ca.crt" \
    >>"$scratch/openssl.log" 2>&1
  openssl req -newkey rsa:2048 -nodes -subj '/CN=localhost' \
    -keyout "$scratch/tls/server.key" -out "$scratch/tls/server.csr" \
    >>"$scratch/openssl.log" 2>&1
  cat > "$scratch/tls/leaf.conf" <<'EOF'
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost,IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF
  openssl x509 -req -in "$scratch/tls/server.csr" \
    -CA "$scratch/tls/ca.crt" -CAkey "$scratch/tls/ca.key" -CAcreateserial \
    -out "$scratch/tls/server.crt" -days 2 -sha256 \
    -extfile "$scratch/tls/leaf.conf" >>"$scratch/openssl.log" 2>&1
fi

if [[ -s "$scratch/server.pid" ]] && kill -0 "$(cat "$scratch/server.pid")" 2>/dev/null; then
  echo "fixture already running, pid $(cat "$scratch/server.pid")" >&2
else
  OMAKASE_FIXTURE_PORT="$port" OMAKASE_SIMULATOR_UDID="$device" \
    nohup python3 "$fixture" \
      --cert "$scratch/tls/server.crt" --key "$scratch/tls/server.key" \
      </dev/null >"$scratch/server.log" 2>&1 &
  echo "$!" > "$scratch/server.pid"
fi

# Finite readiness poll: the fixture either answers or the launcher fails.
ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 "$fixture" --state "$scratch/tls/ca.crt" >/dev/null 2>&1; then
    ready=1
    break
  fi
  kill -0 "$(cat "$scratch/server.pid")" 2>/dev/null || break
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "fixture did not answer on port $port; server log:" >&2
  cat "$scratch/server.log" >&2
  exit 1
fi

# Trust the throwaway CA in this simulator only.
xcrun simctl keychain "$device" add-root-cert "$scratch/tls/ca.crt" >/dev/null

origin="https://127.0.0.1:$port"
ca_sha="$(shasum -a 256 "$scratch/tls/ca.crt" | awk '{print $1}')"
echo "fixture   : $origin"
echo "pid       : $(cat "$scratch/server.pid")"
echo "scratch   : $scratch"
echo "CA sha256 : $ca_sha (throwaway, regenerated per scratch directory)"
echo "state     : python3 $fixture --state $scratch/tls/ca.crt"
echo "stop      : kill \$(cat $scratch/server.pid)"
echo
echo "Next, with the app installed on the same simulator:"
echo "  xcodebuild test -only-testing:OmakasePlusTVUITests/ConnectedQualification \\"
echo "    OMAKASE_QUALIFICATION_ORIGIN=$origin"
