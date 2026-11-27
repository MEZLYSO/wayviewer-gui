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
install -m 0755 "${ROOT}/python/wayviewer_nft_helper.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/power_control.py" "${DEST}/python/"
install -m 0755 "${ROOT}/python/wayviewer_power_helper.py" "${DEST}/python/"
install -m 0755 "${ROOT}/python/wayviewer_hosts_helper.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/hosts_control.py" "${DEST}/python/"
install -m 0644 "${ROOT}/python/discovery.py" "${DEST}/python/"
install -m 0755 "${ROOT}/packaging/install-nft-helper.sh" "${DEST}/"
install -m 0755 "${ROOT}/packaging/install-power-helper.sh" "${DEST}/"
install -m 0755 "${ROOT}/packaging/install-hosts-helper.sh" "${DEST}/"
install -m 0755 "${ROOT}/packaging/install-master.sh" "${DEST}/packaging/"
install -m 0755 "${ROOT}/packaging/install-linux-common.sh" "${DEST}/packaging/"
install -m 0755 "${ROOT}/run.sh" "${DEST}/"
cat > "${DEST}/README.txt" <<README
WayViewer ${VERSION} (${KIND}) - Cliente

Esta es la versión cliente: recibe órdenes de bloqueo de entrada y ping, y
puede suspenderse desde el panel de administración. Al iniciar, pide la
contraseña local de sudo y la conserva únicamente en memoria hasta cerrar.

Dependencias del sistema:
  - Python 3 + tkinter
  - FFmpeg (ffplay)
  - libsrt ABI 1.5, libwayland, libdrm, mesa (para el binario wayviewer)
  - gtk3 (opcional, para el selector de archivos nativo)

Ejecutar:
  ./run.sh

Instalar en Mint/Debian o Arch (requiere un binario compatible):
  sudo ./packaging/install-master.sh \$USER

Bloqueo de ping y hosts del cliente (una vez, como root):
  sudo pacman -S nftables
  sudo ./install-nft-helper.sh \$USER
  sudo ./install-hosts-helper.sh \$USER

El cliente pide la contraseña local al cambiar el bloqueo o suspender.
Para habilitar suspensión remota, instala el helper una vez:
  sudo ./install-power-helper.sh \$USER

El instalador comprueba libc y bibliotecas. El binario del repositorio puede
ser específico de Void/musl; para Debian/Arch usa un build nativo con:
  sudo env WAYVIEWER_BIN=/ruta/al/wayviewer ./packaging/install-master.sh \$USER

Descubrimiento LAN:
  Este cliente anuncia su presencia por UDP en el puerto 9501 para que el
  panel de administración pueda mostrarlo.

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
