#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIND="${1:-$(ldd --version 2>&1 | grep -qi musl && echo musl || echo glibc)}"
BIN="${WAYVIEWER_BIN:-${ROOT}/wayviewer}"
VERSION="${VERSION:-$(git -C "${ROOT}" describe --tags --always 2>/dev/null || echo dev)}"
[[ -x "${BIN}" ]] || { echo "No se encontró binario ejecutable en ${BIN}"; exit 1; }
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
PKG="wayviewer-${VERSION}-linux-x86_64-${KIND}"
DEST="${STAGE}/${PKG}"
mkdir -p "${DEST}/python" "${DEST}/bin"
install -m 0755 "${BIN}" "${DEST}/bin/wayviewer"
install -m 0644 "${ROOT}/python/wayviewer_gui.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/wayviewer_relay.py" "${DEST}/python/"
install -m 0755 "${ROOT}/run.sh" "${DEST}/"
cat > "${DEST}/README.txt" <<README
WayViewer ${VERSION} (${KIND})

Dependencias del sistema:
  - Python 3 + tkinter
  - FFmpeg (ffplay)
  - libsrt, libwayland, libdrm, mesa (para el binario wayviewer)
  - gtk3 (opcional, para el selector de archivos nativo)

Ejecutar:
  ./run.sh

Variables útiles:
  WAYVIEWER_BIN=/ruta/alternativa/wayviewer
  WAYVIEWER_CHAT_PORT=9500
  WAYVIEWER_UNIX_SOCKET=/tmp/wayviewer-\$UID.sock
README
OUT="${ROOT}/dist"
mkdir -p "${OUT}"
tar -C "${STAGE}" -czf "${OUT}/${PKG}.tar.gz" "${PKG}"
sha256sum "${OUT}/${PKG}.tar.gz" > "${OUT}/${PKG}.tar.gz.sha256"
echo "Generado: ${OUT}/${PKG}.tar.gz"
