#!/bin/bash
# Install (or remove) a systemd service that starts the web app at boot.
#   sudo systemd/install-service.sh              install, enable and start
#   sudo systemd/install-service.sh --uninstall  stop, disable and remove
set -euo pipefail

NAME=lte-sib-parser-webapp.service
UNIT=/etc/systemd/system/$NAME
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=$(dirname "$HERE")

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

if [[ ${1:-} == "--uninstall" ]]; then
    systemctl disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "removed $NAME"
    exit 0
fi

DOCKER=$(command -v docker) || { echo "docker not found"; exit 1; }
if ! "$DOCKER" image inspect lte-sib-parser-worker >/dev/null 2>&1; then
    echo "image lte-sib-parser-worker not found: run 'docker compose build' in $PROJECT_DIR first"
    exit 1
fi

sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" -e "s|@DOCKER@|$DOCKER|g" \
    "$HERE/lte-sib-parser-webapp.service.in" > "$UNIT"
systemctl enable docker.service
systemctl daemon-reload
systemctl enable --now "$NAME"
echo "installed $UNIT; web app on http://localhost:8080"
echo "logs: journalctl -u $NAME -f"
