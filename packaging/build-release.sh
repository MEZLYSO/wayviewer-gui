#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${WAYVIEWER_BIN:-${ROOT}/wayviewer}"
if [[ $# -ge 1 ]]; then
    KIND="$1"
else
    INTERPRETER="$(readelf -l "${BIN}" 2>/dev/null | sed -n 's/.*Requesting program interpreter: \([^]]*\)].*/\1/p' | head -n 1 || true)"
    case "${INTERPRETER}" in
        *ld-musl*) KIND=musl ;;
        *ld-linux*) KIND=glibc ;;
        *) KIND="$(ldd --version 2>&1 | grep -qi musl && echo musl || echo glibc)" ;;
    esac
fi
VERSION="${VERSION:-$(git -C "${ROOT}" describe --tags --always 2>/dev/null || echo dev)}"
[[ -x "${BIN}" ]] || { echo "No se encontró binario ejecutable en ${BIN}"; exit 1; }
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
PKG="wayviewer-${VERSION}-linux-x86_64-${KIND}"
DEST="${STAGE}/${PKG}"
mkdir -p "${DEST}/python" "${DEST}/bin" "${DEST}/packaging"
install -m 0755 "${BIN}" "${DEST}/bin/wayviewer"
install -m 0644 "${ROOT}/python/wayviewer_gui.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/wayviewer_relay.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/sway_control.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/hosts_control.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/discovery.py" "${DEST}/python/"
install -m 0755 "${ROOT}/packaging/install-admin.sh" "${DEST}/packaging/"
install -m 0755 "${ROOT}/packaging/install-linux-common.sh" "${DEST}/packaging/"
install -m 0755 "${ROOT}/run.sh" "${DEST}/"
cat > "${DEST}/README.txt" <<README
WayViewer ${VERSION} (${KIND}) - Administración

Esta es la versión de administración: envía las órdenes de bloqueo de entrada
y ping al equipo cliente y puede suspenderlo remotamente. No necesita
nftables ni un helper local.

Dependencias del sistema:
  - Python 3 + tkinter
  - FFmpeg (ffplay)
  - libsrt ABI 1.5, libwayland, libdrm, mesa (para el binario wayviewer)
  - gtk3 (opcional, para el selector de archivos nativo)

Ejecutar:
  ./run.sh

Instalar en Mint/Debian o Arch (requiere un binario compatible):
  sudo ./packaging/install-admin.sh \$USER

El instalador instala dependencias con apt o pacman y comprueba libc y ABI.
El binario incluido puede ser específico de Void/musl. En ese caso proporciona
un build nativo: sudo env WAYVIEWER_BIN=/ruta/wayviewer ./packaging/install-admin.sh \$USER

Bloqueo de /etc/hosts:
  Esta función edita la sección de hosts únicamente en el equipo cliente.

Descubrimiento LAN:
  El panel muestra clientes que anuncian UDP en el puerto 9501.

Variables útiles:
  WAYVIEWER_BIN=/ruta/alternativa/wayviewer
  WAYVIEWER_CHAT_PORT=9500
  WAYVIEWER_DISCOVERY_PORT=9501
  WAYVIEWER_UNIX_SOCKET=/tmp/wayviewer-\$UID.sock
README
OUT="${ROOT}/dist"
mkdir -p "${OUT}"
tar -C "${STAGE}" -czf "${OUT}/${PKG}.tar.gz" "${PKG}"
sha256sum "${OUT}/${PKG}.tar.gz" > "${OUT}/${PKG}.tar.gz.sha256"
echo "Generado: ${OUT}/${PKG}.tar.gz"
