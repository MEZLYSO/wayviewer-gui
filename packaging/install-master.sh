#!/usr/bin/env bash
# Instala WayViewer master (cliente) en Debian/Mint, Arch o Void Linux.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${DIR}/install-linux-common.sh" client "$@"
