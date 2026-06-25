#!/usr/bin/env bash
# Install MinCifry (Russian Ministry of Digital Development) CA certificates
# on the host so local development tooling trusts platform-api2.max.ru.
# See:
#   - https://docs.lanbilling.ru/52/integration/sber/install_sertificates_mincifry/
#   - GitHub issue #233 (fix(ssl): platform-api2.max.ru certificate not trusted)
#
# Usage:
#   ./scripts/install-mincifry-ca.sh           # auto-detect distro
#   NO_SUDO=1 ./scripts/install-mincifry-ca.sh  # manual install under current uid (root only)
set -euo pipefail

readonly ROOT_CA_URL="https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
readonly SUB_CA_URL="https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt"
readonly ROOT_CA_FILE="russian_trusted_root_ca_pem.crt"
readonly SUB_CA_FILE="russian_trusted_sub_ca_pem.crt"

if [[ "${NO_SUDO:-0}" == "1" || "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

if command -v update-ca-certificates >/dev/null 2>&1; then
  TARGET_DIR="/usr/local/share/ca-certificates"
  UPDATE_CMD="update-ca-certificates"
elif command -v update-ca-trust >/dev/null 2>&1; then
  TARGET_DIR="/etc/pki/ca-trust/source/anchors"
  UPDATE_CMD="update-ca-trust extract"
else
  echo "Error: no supported CA store updater found (need update-ca-certificates or update-ca-trust)." >&2
  exit 1
fi

echo "Target CA store: $TARGET_DIR"
echo "Update command:  $UPDATE_CMD"
$SUDO install -d "$TARGET_DIR"
$SUDO curl -fsSL "$ROOT_CA_URL" -o "$TARGET_DIR/$ROOT_CA_FILE"
echo "Downloaded $ROOT_CA_FILE"
$SUDO curl -fsSL "$SUB_CA_URL" -o "$TARGET_DIR/$SUB_CA_FILE"
echo "Downloaded $SUB_CA_FILE"
$SUDO "$UPDATE_CMD"

cat <<'HINT'

MinCifry CA certificates installed.

For Python HTTP clients (requests, httpx, aiohttp) also ensure these env
vars point at the OS trust store before running the app:

  export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
  export CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

Verify the install with:
  curl -vI https://platform-api2.max.ru
HINT
