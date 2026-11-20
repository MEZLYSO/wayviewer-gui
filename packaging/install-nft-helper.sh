#!/bin/sh
# Instala el helper nftables de WayViewer y su regla sudoers.
# La contraseña se solicita localmente en la GUI del cliente al aplicar cambios.
set -eu
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "${SELF_DIR}/.." && pwd)
HELPER=/usr/local/libexec/wayviewer-nft
MODULE=/usr/local/libexec/domain_control.py
SUDOERS=/etc/sudoers.d/wayviewer-nft
TARGET_USER=${1:-${SUDO_USER:-}}

if [ "$(id -u)" -ne 0 ]; then
    echo "Ejecuta como root: sudo $0 usuario" >&2
    exit 1
fi
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || { echo "Indica el usuario de la GUI." >&2; exit 1; }
id "$TARGET_USER" >/dev/null 2>&1 || { echo "El usuario no existe." >&2; exit 1; }
[ -f "$ROOT/python/wayviewer_nft_helper.py" ] || { echo "Falta el helper nftables." >&2; exit 1; }
[ -f "$ROOT/python/domain_control.py" ] || { echo "Falta domain_control.py." >&2; exit 1; }
command -v sudo >/dev/null 2>&1 || { echo "Instala sudo primero." >&2; exit 1; }
command -v visudo >/dev/null 2>&1 || { echo "Falta visudo." >&2; exit 1; }
command -v nft >/dev/null 2>&1 || { echo "Instala nftables: sudo pacman -S nftables." >&2; exit 1; }
command -v getent >/dev/null 2>&1 || { echo "Falta getent." >&2; exit 1; }

# Limpia instalaciones anteriores del helper DNS/hosts de WayViewer.
rm -f /usr/local/libexec/wayviewer-dns \
    /usr/local/libexec/wayviewer-hosts \
    /etc/sudoers.d/wayviewer-dns \
    /etc/sudoers.d/wayviewer-hosts
if [ -f /etc/hosts ] && grep -q '^# BEGIN WAYVIEWER BLOCKED DOMAINS$' /etc/hosts; then
    cp -a /etc/hosts /etc/hosts.before-wayviewer-nft
    sed -i \
        '/^# BEGIN WAYVIEWER BLOCKED DOMAINS$/,/^# END WAYVIEWER BLOCKED DOMAINS$/d' \
        /etc/hosts
fi

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$ROOT/python/wayviewer_nft_helper.py" "$HELPER"
install -o root -g root -m 0644 "$ROOT/python/domain_control.py" "$MODULE"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT INT TERM
cat > "$tmp" <<EOF
# WayViewer: solo modifica la tabla inet wayviewer.
${TARGET_USER} ALL=(root) PASSWD: ${HELPER} set, ${HELPER} get, ${HELPER} clear
EOF
chmod 0440 "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" "$SUDOERS"

echo "Helper nftables instalado: $HELPER"
echo "Sudoers instalado: $SUDOERS"
echo "La contraseña se solicitará localmente al aplicar cambios."
