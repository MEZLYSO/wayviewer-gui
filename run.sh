#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${ROOT}/python${PYTHONPATH:+:${PYTHONPATH}}"
command -v python3 >/dev/null 2>&1 || { echo "Falta python3"; exit 1; }
command -v ffplay >/dev/null 2>&1 || echo "Aviso: no se encontró ffplay (FFmpeg)."
exec python3 "${ROOT}/python/wayviewer_gui.py" "$@"
