#!/usr/bin/env bash
# Instala WayViewer admin en Debian/Mint, Arch o Void Linux.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${DIR}/install-linux-common.sh" admin "$@"
