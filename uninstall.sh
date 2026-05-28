#!/usr/bin/env bash
# Removes the tmsm venv and launcher. Does NOT touch ~/.tmsm data
# (servers, pools, mariadb, backups) unless you pass --purge.
set -euo pipefail

TMSM_HOME="${TMSM_HOME:-$HOME/.tmsm}"
BIN_DIR="${TMSM_BIN_DIR:-$HOME/.local/bin}"

rm -f "$BIN_DIR/tmsm"
rm -rf "$TMSM_HOME/tmsm-venv"
echo "Removed launcher and venv."

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$TMSM_HOME"
    echo "Purged $TMSM_HOME"
fi
