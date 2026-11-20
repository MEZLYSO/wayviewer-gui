#!/usr/bin/env bash
# Cambia temporalmente el resolver del sistema a unbound.
#
# Uso:
#   sudo ./temporary-unbound-resolver.sh activate
#   sudo ./temporary-unbound-resolver.sh revert
#   sudo ./temporary-unbound-resolver.sh status
#
# Pensado para Arch Linux con systemd. Guarda copias del estado original en
# /var/lib/wayviewer/dns-switch y las restaura con «revert».
set -euo pipefail

STATE_DIR="${WAYVIEWER_DNS_SWITCH_STATE:-/var/lib/wayviewer/dns-switch}"
RESOLV_CONF="${WAYVIEWER_RESOLV_CONF:-/etc/resolv.conf}"
NSSWITCH_CONF="${WAYVIEWER_NSSWITCH_CONF:-/etc/nsswitch.conf}"
NM_DROPIN="${WAYVIEWER_NM_DROPIN:-/etc/NetworkManager/conf.d/90-wayviewer-dns.conf}"

RESOLVED_UNIT="systemd-resolved.service"
UNBOUND_UNIT="unbound.service"
NM_UNIT="NetworkManager.service"

RESOLVED_EXISTS=0
RESOLVED_MASKED=0
RESOLVED_ENABLED=0
RESOLVED_ACTIVE=0
NM_ACTIVE=0
NM_DROPIN_EXISTED=0
UNBOUND_ENABLED_BY_US=0
UNBOUND_STARTED_BY_US=0
STATE_READY=0

usage() {
    cat <<'EOF'
Uso:
  sudo ./temporary-unbound-resolver.sh activate
  sudo ./temporary-unbound-resolver.sh revert
  sudo ./temporary-unbound-resolver.sh status

activate:
  Detiene y enmascara systemd-resolved, impide que NetworkManager toque
  /etc/resolv.conf, apunta el sistema a 127.0.0.1, quita nss-resolve de
  /etc/nsswitch.conf y arranca unbound.

revert:
  Restaura los servicios y archivos tal como estaban antes de activate.
  Las copias se guardan en /var/lib/wayviewer/dns-switch.

status:
  Muestra el estado actual sin cambiar nada.
EOF
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Ejecuta este script como root: sudo $0 $*" >&2
        exit 1
    fi
}

require_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "Este script necesita systemd (systemctl)." >&2
        exit 1
    fi
}

unit_exists() {
    systemctl list-unit-files "$1" --no-legend 2>/dev/null | grep -q .
}

unit_active() {
    systemctl is-active --quiet "$1" 2>/dev/null
}

unit_enabled_state() {
    systemctl is-enabled "$1" 2>/dev/null || true
}

is_masked_state() {
    case "$1" in
        masked|masked-runtime) return 0 ;;
        *) return 1 ;;
    esac
}

is_enabled_state() {
    case "$1" in
        enabled|enabled-runtime|linked|linked-runtime) return 0 ;;
        *) return 1 ;;
    esac
}

write_state() {
    umask 077
    {
        printf 'RESOLVED_EXISTS=%q\n' "$RESOLVED_EXISTS"
        printf 'RESOLVED_MASKED=%q\n' "$RESOLVED_MASKED"
        printf 'RESOLVED_ENABLED=%q\n' "$RESOLVED_ENABLED"
        printf 'RESOLVED_ACTIVE=%q\n' "$RESOLVED_ACTIVE"
        printf 'NM_ACTIVE=%q\n' "$NM_ACTIVE"
        printf 'NM_DROPIN_EXISTED=%q\n' "$NM_DROPIN_EXISTED"
        printf 'UNBOUND_ENABLED_BY_US=%q\n' "$UNBOUND_ENABLED_BY_US"
        printf 'UNBOUND_STARTED_BY_US=%q\n' "$UNBOUND_STARTED_BY_US"
    } > "${STATE_DIR}/state.env"
    chmod 0600 "${STATE_DIR}/state.env"
}

backup_file() {
    local source=$1
    local destination=$2
    if [ -e "${source}" ] || [ -L "${source}" ]; then
        cp -a "${source}" "${destination}"
    fi
}

capture_state() {
    local enabled_state

    if unit_exists "${RESOLVED_UNIT}"; then
        RESOLVED_EXISTS=1
        enabled_state=$(unit_enabled_state "${RESOLVED_UNIT}")
        if is_masked_state "${enabled_state}"; then
            RESOLVED_MASKED=1
        fi
        if is_enabled_state "${enabled_state}"; then
            RESOLVED_ENABLED=1
        fi
        if unit_active "${RESOLVED_UNIT}"; then
            RESOLVED_ACTIVE=1
        fi
    fi

    if unit_active "${NM_UNIT}"; then
        NM_ACTIVE=1
    fi
    if [ -e "${NM_DROPIN}" ] || [ -L "${NM_DROPIN}" ]; then
        NM_DROPIN_EXISTED=1
        backup_file "${NM_DROPIN}" "${STATE_DIR}/nm-dropin.bak"
    fi

    backup_file "${RESOLV_CONF}" "${STATE_DIR}/resolv.conf.bak"
    backup_file "${NSSWITCH_CONF}" "${STATE_DIR}/nsswitch.conf.bak"
    write_state
}

unbound_listening() {
    if ! command -v ss >/dev/null 2>&1; then
        return 0
    fi
    ss -lunp 2>/dev/null | awk '
        $5 ~ /:53$/ && /unbound/ { found = 1 }
        END { exit(found ? 0 : 1) }
    '
}

wait_for_unbound() {
    local attempt
    for attempt in {1..20}; do
        if unit_active "${UNBOUND_UNIT}" && unbound_listening; then
            return 0
        fi
        sleep 0.2
    done
    return 1
}

revert_internal() {
    local failed=0

    if [ -f "${STATE_DIR}/state.env" ]; then
        # El archivo lo escribe este script como root y contiene solo valores
        # entrecomillados con %q.
        # shellcheck disable=SC1090
        . "${STATE_DIR}/state.env"
    fi

    if [ "${UNBOUND_STARTED_BY_US:-0}" = 1 ]; then
        if ! systemctl stop "${UNBOUND_UNIT}" >/dev/null 2>&1; then
            echo "Aviso: no se pudo detener unbound." >&2
            failed=1
        fi
    fi
    if [ "${UNBOUND_ENABLED_BY_US:-0}" = 1 ]; then
        if ! systemctl disable "${UNBOUND_UNIT}" >/dev/null 2>&1; then
            echo "Aviso: no se pudo deshabilitar unbound." >&2
            failed=1
        fi
    fi

    if [ "${NM_DROPIN_EXISTED:-0}" = 1 ] && \
        { [ -e "${STATE_DIR}/nm-dropin.bak" ] || \
          [ -L "${STATE_DIR}/nm-dropin.bak" ]; }; then
        if ! cp -a "${STATE_DIR}/nm-dropin.bak" "${NM_DROPIN}"; then
            echo "Aviso: no se pudo restaurar ${NM_DROPIN}." >&2
            failed=1
        fi
    else
        rm -f "${NM_DROPIN}"
    fi

    if [ "${NM_ACTIVE:-0}" = 1 ]; then
        if ! systemctl restart "${NM_UNIT}" >/dev/null 2>&1; then
            echo "Aviso: no se pudo reiniciar NetworkManager." >&2
            failed=1
        fi
    fi

    if [ -e "${STATE_DIR}/nsswitch.conf.bak" ] || \
        [ -L "${STATE_DIR}/nsswitch.conf.bak" ]; then
        if ! cp -a "${STATE_DIR}/nsswitch.conf.bak" "${NSSWITCH_CONF}"; then
            echo "Aviso: no se pudo restaurar ${NSSWITCH_CONF}." >&2
            failed=1
        fi
    fi

    if [ -e "${STATE_DIR}/resolv.conf.bak" ] || \
        [ -L "${STATE_DIR}/resolv.conf.bak" ]; then
        rm -f "${RESOLV_CONF}"
        if ! cp -a "${STATE_DIR}/resolv.conf.bak" "${RESOLV_CONF}"; then
            echo "Aviso: no se pudo restaurar ${RESOLV_CONF}." >&2
            failed=1
        fi
    fi

    if [ "${RESOLVED_EXISTS:-0}" = 1 ]; then
        if [ "${RESOLVED_MASKED:-0}" = 1 ]; then
            systemctl mask "${RESOLVED_UNIT}" >/dev/null 2>&1 || true
        else
            systemctl unmask "${RESOLVED_UNIT}" >/dev/null 2>&1 || true
        fi
        if [ "${RESOLVED_ENABLED:-0}" = 1 ]; then
            systemctl enable "${RESOLVED_UNIT}" >/dev/null 2>&1 || \
                echo "Aviso: no se pudo habilitar ${RESOLVED_UNIT}." >&2
        else
            systemctl disable "${RESOLVED_UNIT}" >/dev/null 2>&1 || true
        fi
        if [ "${RESOLVED_ACTIVE:-0}" = 1 ]; then
            if ! systemctl start "${RESOLVED_UNIT}" >/dev/null 2>&1; then
                echo "Aviso: no se pudo arrancar ${RESOLVED_UNIT}." >&2
                failed=1
            fi
        else
            systemctl stop "${RESOLVED_UNIT}" >/dev/null 2>&1 || true
        fi
    fi

    return "${failed}"
}

cleanup_on_error() {
    local status=$1
    trap - ERR
    if [ "${STATE_READY:-0}" = 1 ]; then
        echo "Error durante la activación; restaurando el estado anterior…" >&2
        revert_internal || true
    fi
    rm -rf "${STATE_DIR}"
    exit "${status}"
}

abort_activation() {
    echo "Error: $*" >&2
    cleanup_on_error 1
}

activate() {
    require_root "$@"
    require_systemd

    if [ -e "${STATE_DIR}/active" ]; then
        echo "El modo temporal ya está activo."
        status
        return 0
    fi
    if [ -d "${STATE_DIR}" ] && \
        [ -n "$(find "${STATE_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        echo "Hay estado previo en ${STATE_DIR}." >&2
        echo "Ejecuta «$0 revert» antes de volver a activar." >&2
        exit 1
    fi

    if ! unit_exists "${UNBOUND_UNIT}"; then
        echo "No está instalado unbound (${UNBOUND_UNIT})." >&2
        echo "En Arch Linux: sudo pacman -S unbound" >&2
        exit 1
    fi
    if ! command -v unbound >/dev/null 2>&1; then
        echo "No se encontró el binario unbound." >&2
        exit 1
    fi
    if command -v unbound-checkconf >/dev/null 2>&1; then
        if ! unbound-checkconf >/dev/null 2>&1; then
            echo "unbound-checkconf ha fallado; corrige la configuración antes." >&2
            unbound-checkconf >&2 || true
            exit 1
        fi
    fi
    if [ ! -f "${NSSWITCH_CONF}" ]; then
        echo "No existe ${NSSWITCH_CONF}." >&2
        exit 1
    fi
    if [ ! -f /etc/unbound/wayviewer-blocklist.conf ]; then
        echo "Aviso: aún no existe /etc/unbound/wayviewer-blocklist.conf." >&2
        echo "El resolver cambiará, pero no habrá dominios bloqueados hasta" >&2
        echo "que instales el helper y apliques la lista." >&2
    fi

    mkdir -p "${STATE_DIR}"
    chmod 0700 "${STATE_DIR}"
    trap 'cleanup_on_error $?' ERR

    capture_state
    STATE_READY=1

    if [ "${RESOLVED_EXISTS}" = 1 ]; then
        systemctl stop "${RESOLVED_UNIT}" >/dev/null 2>&1 || true
        systemctl disable "${RESOLVED_UNIT}" >/dev/null 2>&1 || true
        if ! systemctl mask "${RESOLVED_UNIT}" >/dev/null 2>&1; then
            abort_activation "no se pudo enmascarar ${RESOLVED_UNIT}."
        fi
    fi

    if [ "${NM_ACTIVE}" = 1 ]; then
        mkdir -p "$(dirname "${NM_DROPIN}")"
        cat > "${NM_DROPIN}" <<'EOF'
[main]
dns=none
EOF
        chmod 0644 "${NM_DROPIN}"
        if ! systemctl restart "${NM_UNIT}" >/dev/null 2>&1; then
            abort_activation "no se pudo reiniciar NetworkManager."
        fi
    fi

    if grep -q '^hosts:' "${NSSWITCH_CONF}"; then
        sed -i 's/^hosts:.*/hosts: files mymachines myhostname dns/' \
            "${NSSWITCH_CONF}"
    else
        printf 'hosts: files mymachines myhostname dns\n' >> "${NSSWITCH_CONF}"
    fi

    rm -f "${RESOLV_CONF}"
    printf 'nameserver 127.0.0.1\n' > "${RESOLV_CONF}"
    chmod 0644 "${RESOLV_CONF}"

    if ! is_enabled_state "$(unit_enabled_state "${UNBOUND_UNIT}")"; then
        if ! systemctl enable "${UNBOUND_UNIT}" >/dev/null 2>&1; then
            abort_activation "no se pudo habilitar ${UNBOUND_UNIT}."
        fi
        UNBOUND_ENABLED_BY_US=1
        write_state
    fi

    if ! unit_active "${UNBOUND_UNIT}"; then
        if ! systemctl start "${UNBOUND_UNIT}" >/dev/null 2>&1; then
            abort_activation "no se pudo arrancar ${UNBOUND_UNIT}."
        fi
        UNBOUND_STARTED_BY_US=1
        write_state
    fi

    if [ "${RESOLVED_EXISTS}" = 1 ] && unit_active "${RESOLVED_UNIT}"; then
        abort_activation "systemd-resolved sigue activo."
    fi
    if ! wait_for_unbound; then
        abort_activation "unbound no está escuchando en el puerto 53."
    fi

    touch "${STATE_DIR}/active"
    trap - ERR
    echo "Modo temporal activado."
    echo "  Resolver: 127.0.0.1"
    echo "  Estado:   ${STATE_DIR}"
    echo
    echo "Para volver al estado anterior:"
    echo "  sudo $0 revert"
}

revert() {
    require_root "$@"
    require_systemd

    if [ ! -d "${STATE_DIR}" ] || [ ! -f "${STATE_DIR}/state.env" ]; then
        echo "No hay ningún estado temporal guardado en ${STATE_DIR}."
        return 0
    fi

    trap - ERR
    if revert_internal; then
        rm -rf "${STATE_DIR}"
        echo "Estado anterior restaurado."
        echo "Revisa el resolver con:"
        echo "  cat ${RESOLV_CONF}"
        echo "  systemctl is-active systemd-resolved unbound"
    else
        echo "La restauración quedó incompleta." >&2
        echo "Se conservan las copias en ${STATE_DIR}." >&2
        echo "Corrige el problema y vuelve a ejecutar «$0 revert»." >&2
        exit 1
    fi
}

status() {
    require_systemd
    echo "Modo temporal: $(
        [ -e "${STATE_DIR}/active" ] && echo ACTIVO || echo inactivo
    )"
    if [ -d "${STATE_DIR}" ]; then
        echo "Estado guardado: ${STATE_DIR}"
    fi

    echo
    echo "== /etc/resolv.conf =="
    cat "${RESOLV_CONF}" 2>/dev/null || true
    echo
    echo "== nsswitch.conf =="
    grep '^hosts:' "${NSSWITCH_CONF}" 2>/dev/null || true
    echo
    echo "== servicios =="
    printf 'systemd-resolved: '
    systemctl is-active "${RESOLVED_UNIT}" 2>/dev/null || true
    printf 'unbound:          '
    systemctl is-active "${UNBOUND_UNIT}" 2>/dev/null || true
    printf 'NetworkManager:   '
    systemctl is-active "${NM_UNIT}" 2>/dev/null || true
    echo
    echo "== puerto 53 =="
    if command -v ss >/dev/null 2>&1; then
        ss -lunp 2>/dev/null | grep ':53' || echo "nada escuchando en :53"
    else
        echo "ss no está instalado"
    fi
    echo
    echo "== blocklist =="
    if [ -f /etc/unbound/wayviewer-blocklist.conf ]; then
        grep -E 'local-zone|^server:' \
            /etc/unbound/wayviewer-blocklist.conf || true
    else
        echo "No existe /etc/unbound/wayviewer-blocklist.conf"
    fi
}

case "${1:-}" in
    activate|on|activar)
        activate
        ;;
    revert|off|restore|revertir)
        revert
        ;;
    status|estado)
        status
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        echo "Comando desconocido: $1" >&2
        usage >&2
        exit 2
        ;;
esac
