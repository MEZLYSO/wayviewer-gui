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
install -m 0644 "${ROOT}/python/sway_control.py" "${DEST}/python/"
install -m 0755 "${ROOT}/python/wayviewer_nft_helper.py" "${DEST}/python/"
install -m 0755 "${ROOT}/packaging/install-nft-helper.sh" "${DEST}/"
install -m 0755 "${ROOT}/run.sh" "${DEST}/"
cat > "${DEST}/README.txt" <<README
WayViewer ${VERSION} (${KIND}) - Cliente

Esta es la versión cliente: recibe las órdenes de entrada y de bloqueo de
ping desde el panel de administración y las aplica localmente.

Dependencias del sistema:
  - Python 3 + tkinter
  - FFmpeg (ffplay)
  - libsrt, libwayland, libdrm, mesa (para el binario wayviewer)
  - gtk3 (opcional, para el selector de archivos nativo)

Ejecutar:
  ./run.sh

Bloqueo de ping del cliente mediante nftables (una vez, como root):
  sudo pacman -S nftables
  sudo ./install-nft-helper.sh \$USER

El cliente pide la contraseña local al cambiar el bloqueo de ping.

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
