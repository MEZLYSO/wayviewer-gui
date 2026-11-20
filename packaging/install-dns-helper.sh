#!/bin/sh
# Instala el helper de /etc/hosts de WayViewer y su regla sudoers.
# La contraseña se solicita en la GUI del cliente al aplicar una lista.
set -eu
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "${SELF_DIR}/.." && pwd)
HELPER=/usr/local/libexec/wayviewer-hosts
MODULE=/usr/local/libexec/domain_control.py
SUDOERS=/etc/sudoers.d/wayviewer-hosts
TARGET_USER=${1:-${SUDO_USER:-}}

if [ "$(id -u)" -ne 0 ]; then
    echo "Ejecuta como root: sudo $0 usuario" >&2
    exit 1
fi
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || { echo "Indica el usuario de la GUI." >&2; exit 1; }
id "$TARGET_USER" >/dev/null 2>&1 || { echo "El usuario no existe." >&2; exit 1; }
[ -f "$ROOT/python/wayviewer_dns_helper.py" ] || { echo "Falta el helper." >&2; exit 1; }
[ -f "$ROOT/python/domain_control.py" ] || { echo "Falta domain_control.py." >&2; exit 1; }
command -v sudo >/dev/null 2>&1 || { echo "Instala sudo primero." >&2; exit 1; }
command -v visudo >/dev/null 2>&1 || { echo "Falta visudo." >&2; exit 1; }

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$ROOT/python/wayviewer_dns_helper.py" "$HELPER"
install -o root -g root -m 0644 "$ROOT/python/domain_control.py" "$MODULE"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT INT TERM
cat > "$tmp" <<EOF
# WayViewer: solo modifica su sección delimitada de /etc/hosts.
${TARGET_USER} ALL=(root) PASSWD: ${HELPER} set, ${HELPER} get
EOF
chmod 0440 "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" "$SUDOERS"

echo "Helper instalado: $HELPER"
echo "Sudoers instalado: $SUDOERS"
echo "La contraseña se solicitará localmente al aplicar cambios."
