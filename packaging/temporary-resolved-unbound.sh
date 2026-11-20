#!/usr/bin/env bash
# Mantiene systemd-resolved activo y lo hace reenviar temporalmente a unbound.
#
# Uso:
#   sudo ./temporary-resolved-unbound.sh activate
#   sudo ./temporary-resolved-unbound.sh revert
#   sudo ./temporary-resolved-unbound.sh status
#
# systemd-resolved conserva 127.0.0.53:53. Unbound escucha en 127.0.0.1:5353.
# Todo lo modificado se guarda en /var/lib/wayviewer/resolved-unbound.
set -euo pipefail

STATE_DIR="${WAYVIEWER_RESOLVED_STATE:-/var/lib/wayviewer/resolved-unbound}"
RESOLVED_CONF="/etc/systemd/resolved.conf"
RESOLV_CONF="/etc/resolv.conf"
UNBOUND_CONF="/etc/unbound/unbound.conf"
UNBOUND_DROPIN="/etc/unbound/wayviewer-resolved-forward.conf"
RESOLVED_UNIT="systemd-resolved.service"
UNBOUND_UNIT="unbound.service"

usage() {
    cat <<'EOF'
Uso:
  sudo ./temporary-resolved-unbound.sh activate
  sudo ./temporary-resolved-unbound.sh revert
  sudo ./temporary-resolved-unbound.sh status

activate:
  Mantiene systemd-resolved activo en 127.0.0.53:53 y configura unbound en
  127.0.0.1:5353 como su servidor DNS ascendente.

revert:
  Restaura las configuraciones y el estado anterior de ambos servicios.
EOF
}

require_root() {
    [ "$(id -u)" -eq 0 ] || {
        echo "Ejecuta como root: sudo $0 $*" >&2
        exit 1
    }
}

require_systemd() {
    command -v systemctl >/dev/null 2>&1 || {
        echo "Este script necesita systemd." >&2
        exit 1
    }
}

unit_enabled() {
    case "$(systemctl is-enabled "$1" 2>/dev/null || true)" in
        enabled|enabled-runtime|linked|linked-runtime) return 0 ;;
        *) return 1 ;;
    esac
}

unit_active() {
    systemctl is-active --quiet "$1" 2>/dev/null
}

backup_path() {
    local path=$1
    local name=$2
    if [ -e "$path" ] || [ -L "$path" ]; then
        cp -a "$path" "${STATE_DIR}/${name}"
        printf '%s\n' present > "${STATE_DIR}/${name}.present"
    else
        : > "${STATE_DIR}/${name}.absent"
    fi
}

restore_path() {
    local path=$1
    local name=$2
    rm -f "$path"
    if [ -e "${STATE_DIR}/${name}.present" ]; then
        cp -a "${STATE_DIR}/${name}" "$path"
    fi
}

save_state() {
    umask 077
    {
        printf 'RESOLVED_ENABLED=%q\n' "$RESOLVED_ENABLED"
        printf 'RESOLVED_ACTIVE=%q\n' "$RESOLVED_ACTIVE"
        printf 'UNBOUND_ENABLED=%q\n' "$UNBOUND_ENABLED"
        printf 'UNBOUND_ACTIVE=%q\n' "$UNBOUND_ACTIVE"
    } > "${STATE_DIR}/state.env"
    chmod 0600 "${STATE_DIR}/state.env"
}

has_forward_include() {
    grep -Fqx "include-toplevel: \"${UNBOUND_DROPIN}\"" "$UNBOUND_CONF"
}

wait_for_listeners() {
    local attempt
    for attempt in {1..25}; do
        if ss -lun 2>/dev/null | grep -Eq '127\.0\.0\.1:5353|127\.0\.0\.53:53'; then
            if ss -lun 2>/dev/null | grep -q '127\.0\.0\.1:5353' && \
                ss -lun 2>/dev/null | grep -q '127\.0\.0\.53:53'; then
                return 0
            fi
        fi
        sleep 0.2
    done
    return 1
}

restore_services() {
    local failed=0

    # Restaurar primero la configuración de unbound y validar antes de
    # arrancarlo. Así no se deja el servicio con el puerto 5353 accidental.
    restore_path "$UNBOUND_CONF" unbound.conf
    restore_path "$UNBOUND_DROPIN" unbound-forward.conf
    restore_path "$RESOLVED_CONF" resolved.conf
    restore_path "$RESOLV_CONF" resolv.conf

    if command -v unbound-checkconf >/dev/null 2>&1 && \
        ! unbound-checkconf >/dev/null 2>&1; then
        echo "Aviso: la configuración restaurada de unbound no es válida." >&2
        unbound-checkconf >&2 || true
        failed=1
    fi

    if [ "${UNBOUND_ACTIVE:-0}" = 1 ]; then
        systemctl restart "$UNBOUND_UNIT" >/dev/null 2>&1 || {
            echo "Aviso: no se pudo reiniciar unbound." >&2
            failed=1
        }
    else
        systemctl stop "$UNBOUND_UNIT" >/dev/null 2>&1 || true
    fi
    if [ "${UNBOUND_ENABLED:-0}" = 1 ]; then
        systemctl enable "$UNBOUND_UNIT" >/dev/null 2>&1 || true
    else
        systemctl disable "$UNBOUND_UNIT" >/dev/null 2>&1 || true
    fi

    if [ "${RESOLVED_ACTIVE:-0}" = 1 ]; then
        systemctl restart "$RESOLVED_UNIT" >/dev/null 2>&1 || {
            echo "Aviso: no se pudo reiniciar systemd-resolved." >&2
            failed=1
        }
    else
        systemctl stop "$RESOLVED_UNIT" >/dev/null 2>&1 || true
    fi
    if [ "${RESOLVED_ENABLED:-0}" = 1 ]; then
        systemctl enable "$RESOLVED_UNIT" >/dev/null 2>&1 || true
    else
        systemctl disable "$RESOLVED_UNIT" >/dev/null 2>&1 || true
    fi
    return "$failed"
}

rollback() {
    echo "Error durante la activación; restaurando el estado anterior…" >&2
    if [ -f "${STATE_DIR}/state.env" ]; then
        # shellcheck disable=SC1090
        . "${STATE_DIR}/state.env"
        restore_services || true
    fi
    rm -rf "$STATE_DIR"
}

activate() {
    require_root "$@"
    require_systemd

    if [ -e "${STATE_DIR}/active" ]; then
        echo "El modo temporal ya está activo."
        return 0
    fi
    if [ -d "$STATE_DIR" ] && [ -n "$(find "$STATE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        echo "Hay un estado anterior en ${STATE_DIR}; ejecuta revert primero." >&2
        exit 1
    fi
    [ -f "$UNBOUND_CONF" ] || {
        echo "No existe ${UNBOUND_CONF}. Instala unbound primero." >&2
        exit 1
    }
    command -v ss >/dev/null 2>&1 || {
        echo "Falta ss; instala iproute2." >&2
        exit 1
    }

    mkdir -p "$STATE_DIR"
    chmod 0700 "$STATE_DIR"
    trap 'rollback' ERR

    RESOLVED_ENABLED=0; RESOLVED_ACTIVE=0
    UNBOUND_ENABLED=0; UNBOUND_ACTIVE=0
    unit_enabled "$RESOLVED_UNIT" && RESOLVED_ENABLED=1
    unit_active "$RESOLVED_UNIT" && RESOLVED_ACTIVE=1
    unit_enabled "$UNBOUND_UNIT" && UNBOUND_ENABLED=1
    unit_active "$UNBOUND_UNIT" && UNBOUND_ACTIVE=1
    save_state

    backup_path "$RESOLVED_CONF" resolved.conf
    backup_path "$RESOLV_CONF" resolv.conf
    backup_path "$UNBOUND_CONF" unbound.conf
    backup_path "$UNBOUND_DROPIN" unbound-forward.conf

    cat > "$UNBOUND_DROPIN" <<EOF
# Generado temporalmente por WayViewer. Se elimina al ejecutar revert.
server:
    interface: 127.0.0.1
    port: 5353
    do-ip4: yes
EOF
    chmod 0644 "$UNBOUND_DROPIN"

    if ! has_forward_include; then
        cat >> "$UNBOUND_CONF" <<EOF

# WayViewer: unbound temporal para systemd-resolved
include-toplevel: "${UNBOUND_DROPIN}"
EOF
    fi

    if command -v unbound-checkconf >/dev/null 2>&1 && \
        ! unbound-checkconf >/dev/null 2>&1; then
        unbound-checkconf >&2 || true
        echo "La configuración de unbound es inválida." >&2
        exit 1
    fi

    cat > "$RESOLVED_CONF" <<'EOF'
# Generado temporalmente por WayViewer. Se restaura con revert.
[Resolve]
DNS=127.0.0.1:5353
FallbackDNS=
DNSStubListener=yes
EOF
    chmod 0644 "$RESOLVED_CONF"

    systemctl restart "$UNBOUND_UNIT"
    systemctl restart "$RESOLVED_UNIT"
    wait_for_listeners || {
        echo "Unbound o systemd-resolved no están escuchando en los puertos esperados." >&2
        ss -lunp >&2 || true
        exit 1
    }

    touch "${STATE_DIR}/active"
    trap - ERR
    echo "Activado: systemd-resolved -> unbound 127.0.0.1:5353"
    echo "Revertir con: sudo $0 revert"
}

revert() {
    require_root "$@"
    require_systemd
    [ -f "${STATE_DIR}/state.env" ] || {
        echo "No hay estado temporal guardado en ${STATE_DIR}."
        exit 0
    }
    # shellcheck disable=SC1090
    . "${STATE_DIR}/state.env"
    if restore_services; then
        rm -rf "$STATE_DIR"
        echo "Configuración anterior restaurada."
    else
        echo "La restauración quedó incompleta; se conserva ${STATE_DIR}." >&2
        exit 1
    fi
}

status() {
    require_systemd
    echo "Modo temporal: $([ -e "${STATE_DIR}/active" ] && echo ACTIVO || echo inactivo)"
    echo "systemd-resolved: $(systemctl is-active "$RESOLVED_UNIT" 2>/dev/null || true)"
    echo "unbound:          $(systemctl is-active "$UNBOUND_UNIT" 2>/dev/null || true)"
    echo
    echo "resolv.conf:"
    readlink -f "$RESOLV_CONF" 2>/dev/null || true
    cat "$RESOLV_CONF" 2>/dev/null || true
    echo
    ss -lunp 2>/dev/null | grep -E ':(53|5353)\b' || true
}

case "${1:-}" in
    activate|on|activar) activate ;;
    revert|off|restore|revertir) revert ;;
    status|estado) status ;;
    -h|--help|help|"") usage ;;
    *) echo "Comando desconocido: $1" >&2; usage >&2; exit 2 ;;
esac
