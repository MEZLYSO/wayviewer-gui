#!/usr/bin/env bash
# Instala WayViewer admin en Debian/Mint o Arch y derivados.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${DIR}/install-linux-common.sh" admin "$@"
