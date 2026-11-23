#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER=/usr/local/libexec/wayviewer-hosts
MODULE=/usr/local/libexec/hosts_control.py
SUDOERS=/etc/sudoers.d/wayviewer-hosts
TARGET_USER="${1:-${SUDO_USER:-}}"
[ "$(id -u)" -eq 0 ] || { echo "Ejecuta como root: sudo $0 usuario" >&2; exit 1; }
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || { echo "Indica el usuario." >&2; exit 1; }
id "$TARGET_USER" >/dev/null 2>&1 || { echo "Usuario inexistente." >&2; exit 1; }
command -v sudo >/dev/null 2>&1 && command -v visudo >/dev/null 2>&1 || { echo "Faltan sudo o visudo." >&2; exit 1; }
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$ROOT/python/wayviewer_hosts_helper.py" "$HELPER"
install -o root -g root -m 0644 "$ROOT/python/hosts_control.py" "$MODULE"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<EOF
${TARGET_USER} ALL=(root) PASSWD: ${HELPER} set, ${HELPER} get
EOF
chmod 0440 "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" "$SUDOERS"
echo "Helper /etc/hosts instalado: $HELPER"
