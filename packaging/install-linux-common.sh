#!/usr/bin/env bash
# Instalador compartido por los roles master (cliente) y admin.
set -euo pipefail

ROLE="${1:-}"
shift || true
TARGET_USER="${1:-${SUDO_USER:-}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    echo "Uso: sudo packaging/install-${ROLE}.sh [usuario]" >&2
}

if [[ "$ROLE" != client && "$ROLE" != admin ]]; then
    echo "Rol inválido. Use client o admin." >&2
    exit 2
fi
if [[ "$ROLE" == client ]]; then
    INSTALLER="install-master.sh"
else
    INSTALLER="install-admin.sh"
fi
if [[ $(id -u) -ne 0 ]]; then
    echo "Ejecuta como root: sudo packaging/install-${ROLE}.sh usuario" >&2
    exit 1
fi
if [[ -z "$TARGET_USER" || "$TARGET_USER" == root ]]; then
    echo "Indica el usuario normal que ejecutará WayViewer." >&2
    usage
    exit 1
fi
id "$TARGET_USER" >/dev/null 2>&1 || {
    echo "No existe el usuario: $TARGET_USER" >&2
    exit 1
}

DEFAULT_BIN="${ROOT}/wayviewer"
if [[ ! -x "$DEFAULT_BIN" && -x "${ROOT}/bin/wayviewer" ]]; then
    DEFAULT_BIN="${ROOT}/bin/wayviewer"
fi
BIN="${WAYVIEWER_BIN:-${DEFAULT_BIN}}"
[[ -x "$BIN" ]] || {
    echo "No hay binario WayViewer ejecutable: $BIN" >&2
    echo "Puedes proporcionar uno nativo con: sudo env WAYVIEWER_BIN=/ruta/wayviewer packaging/${INSTALLER} $TARGET_USER" >&2
    exit 1
}
BIN="$(readlink -f "$BIN")"

install_dependencies() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "Detectado apt (Debian, Ubuntu, Mint y derivados)."
        apt-get update
        local packages=(python3 python3-tk ffmpeg binutils libwayland-client0 libgbm1 libdrm2 libstdc++6)
        if [[ "$ROLE" == client ]]; then
            packages+=(sudo nftables)
        fi
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
        PACKAGE_FAMILY=debian
    elif command -v pacman >/dev/null 2>&1; then
        echo "Detectado pacman (Arch y derivados)."
        echo "En Arch, actualiza primero el sistema para evitar una actualización parcial: pacman -Syu"
        local packages=(python tk ffmpeg binutils wayland mesa libdrm gcc-libs)
        if [[ "$ROLE" == client ]]; then
            packages+=(sudo nftables)
        fi
        pacman -S --needed --noconfirm "${packages[@]}"
        PACKAGE_FAMILY=arch
    else
        echo "Distribución no soportada automáticamente: se requiere apt o pacman." >&2
        echo "Instala Python 3, Tkinter, FFmpeg, binutils y las bibliotecas Wayland/DRM/GBM manualmente." >&2
        exit 1
    fi
}

check_binary_compatibility() {
    local machine expected interpreter output
    machine="$(readelf -h "$BIN" 2>/dev/null | sed -n 's/^[[:space:]]*Machine:[[:space:]]*//p')"
    case "$(uname -m)" in
        x86_64) expected='X86-64' ;;
        aarch64) expected='AArch64' ;;
        armv7l|armv6l) expected='ARM' ;;
        *) expected="$(uname -m)" ;;
    esac
    if [[ -n "$machine" && "$machine" != *"$expected"* ]]; then
        echo "El binario es para '$machine', pero el sistema es $(uname -m)." >&2
        echo "Compila/proporciona un binario nativo mediante WAYVIEWER_BIN." >&2
        return 1
    fi

    interpreter="$(readelf -l "$BIN" 2>/dev/null | sed -n 's/.*Requesting program interpreter: \([^]]*\)].*/\1/p' | head -n 1)"
    if [[ -n "$interpreter" && ! -x "$interpreter" ]]; then
        echo "Falta el cargador ELF requerido por WayViewer: $interpreter" >&2
        echo "El binario incluido no es compatible con esta distribución/libc." >&2
        echo "Proporciona un build nativo: sudo env WAYVIEWER_BIN=/ruta/wayviewer packaging/${INSTALLER} $TARGET_USER" >&2
        return 1
    fi

    if ! output="$(ldd "$BIN" 2>&1)"; then
        echo "No se pudo cargar el binario WayViewer en este sistema:" >&2
        printf '%s\n' "$output" >&2
        echo "El ejecutable debe compilarse para la libc y FFmpeg de esta distribución." >&2
        return 1
    fi
    if grep -Eq 'not found|Error loading shared library|No such file or directory' <<<"$output"; then
        echo "El binario WayViewer tiene bibliotecas compartidas incompatibles o ausentes:" >&2
        grep -E 'not found|Error loading shared library|No such file or directory' <<<"$output" >&2
        echo "Instala las ABI de FFmpeg/SRT requeridas o proporciona un binario nativo con WAYVIEWER_BIN." >&2
        return 1
    fi
}

install_dependencies
check_binary_compatibility

APP_DIR="/opt/wayviewer-${ROLE}"
install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/python" "$APP_DIR/packaging"
install -o root -g root -m 0644 "$ROOT"/python/*.py "$APP_DIR/python/"
install -o root -g root -m 0755 "$BIN" "$APP_DIR/wayviewer"
install -o root -g root -m 0755 "$ROOT/run.sh" "$APP_DIR/run.sh"
install -o root -g root -m 0755 "$ROOT"/packaging/*.sh "$APP_DIR/packaging/"

LAUNCHER="/usr/local/bin/wayviewer-${ROLE}"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "${APP_DIR}/run.sh" "\$@"
EOF
chown root:root "$LAUNCHER"
chmod 0755 "$LAUNCHER"

if [[ "$ROLE" == client ]]; then
    # Los helpers están limitados a comandos concretos; la GUI continúa como
    # usuario normal. Instalación/actualización aislada por archivos sudoers.
    bash "$APP_DIR/packaging/install-nft-helper.sh" "$TARGET_USER"
    bash "$APP_DIR/packaging/install-hosts-helper.sh" "$TARGET_USER"
    if [[ -x /usr/bin/systemctl ]]; then
        bash "$APP_DIR/packaging/install-power-helper.sh" "$TARGET_USER"
    else
        echo "Aviso: systemd no está presente; la suspensión remota no se instalará." >&2
    fi
    echo "Cliente instalado. Ejecuta wayviewer-client como ${TARGET_USER}."
    echo "Nftables, /etc/hosts y suspensión remota quedan habilitados mediante helpers limitados."
else
    echo "Administrador instalado. Ejecuta wayviewer-admin como ${TARGET_USER}."
fi
