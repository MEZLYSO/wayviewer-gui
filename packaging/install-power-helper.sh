#!/usr/bin/env bash
# Instala un helper sudo limitado exclusivamente a suspender el cliente.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER=/usr/local/libexec/wayviewer-power
SUDOERS=/etc/sudoers.d/wayviewer-power
TARGET_USER="${1:-${SUDO_USER:-}}"

if [[ $(id -u) -ne 0 ]]; then
    echo "Ejecuta como root: sudo $0 usuario" >&2
    exit 1
fi
if [[ -z "$TARGET_USER" || "$TARGET_USER" == root ]]; then
    echo "Indica el usuario que ejecuta WayViewer." >&2
    exit 1
fi
id "$TARGET_USER" >/dev/null 2>&1 || { echo "Usuario inexistente: $TARGET_USER" >&2; exit 1; }
command -v visudo >/dev/null 2>&1 || { echo "No se encontró visudo." >&2; exit 1; }
[[ -f "$ROOT/python/wayviewer_power_helper.py" ]] || { echo "Falta el helper." >&2; exit 1; }
[[ -x /usr/bin/systemctl ]] || { echo "No se encontró /usr/bin/systemctl." >&2; exit 1; }

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$ROOT/python/wayviewer_power_helper.py" "$HELPER"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT INT TERM
cat > "$tmp" <<EOF
# WayViewer: solo autoriza la suspensión explícita del cliente.
${TARGET_USER} ALL=(root) PASSWD: ${HELPER} suspend
EOF
chmod 0440 "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" "$SUDOERS"
echo "Helper de suspensión instalado para ${TARGET_USER}."
