#!/usr/bin/env bash
# Generates a local development CA and TLS leaf certificates for the
# coordinator and dashboard services. Dev use only, self-signed —
# never use this CA or these keys outside your machine.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CERT_DIR="$ROOT_DIR/certs"
DAYS=825

mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

if [[ ! -f dev-ca.key ]]; then
  echo "Generating dev CA..."
  openssl genrsa -out dev-ca.key 4096
  # Leading "//" (not "/") stops Git Bash/MSYS from mistaking this for
  # a filesystem path and rewriting it before openssl sees it.
  openssl req -x509 -new -nodes -key dev-ca.key -sha256 -days "$DAYS" \
    -subj "//O=Distributed Worker Network Dev CA/CN=dev-ca" \
    -out dev-ca.crt
else
  echo "Dev CA already exists at $CERT_DIR/dev-ca.crt, reusing."
fi

generate_leaf() {
  local name="$1"
  local sans="$2"

  echo "Generating certificate for $name..."
  openssl genrsa -out "${name}.key" 2048
  openssl req -new -key "${name}.key" \
    -subj "//O=Distributed Worker Network Dev/CN=${name}" \
    -out "${name}.csr"

  # Relative filename (cwd is already $CERT_DIR) — avoids passing an
  # absolute /tmp path to the native Windows openssl.exe, which won't
  # resolve MSYS-style absolute paths.
  local extfile="${name}.ext"
  printf "subjectAltName=%s\n" "$sans" > "$extfile"

  openssl x509 -req -in "${name}.csr" -CA dev-ca.crt -CAkey dev-ca.key -CAcreateserial \
    -out "${name}.crt" -days "$DAYS" -sha256 \
    -extfile "$extfile"

  rm -f "${name}.csr" "$extfile"
}

generate_leaf coordinator "DNS:coordinator,DNS:localhost,IP:127.0.0.1"
generate_leaf dashboard "DNS:dashboard,DNS:localhost,IP:127.0.0.1"

echo
echo "Done. Certs written to $CERT_DIR"
echo "Trust $CERT_DIR/dev-ca.crt in your browser/OS to avoid TLS warnings (dev only)."
