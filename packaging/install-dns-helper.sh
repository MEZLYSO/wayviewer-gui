#!/bin/sh
# Instala el helper DNS de WayViewer y su regla de sudoers.
#
# Uso:
#   sudo ./packaging/install-dns-helper.sh [usuario]
#
# El usuario es la cuenta que ejecuta la GUI (por defecto SUDO_USER). El
# helper solo podrá ejecutarse como root con los argumentos exactos «set» y
# «get»; no se concede sudo general.
set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "${SELF_DIR}/.." && pwd)
HELPER=/usr/local/libexec/wayviewer-dns
MODULE=/usr/local/libexec/domain_control.py
SUDOERS=/etc/sudoers.d/wayviewer-dns
STATE_DIR=/var/lib/wayviewer
CONFIG=/etc/unbound/unbound.conf
BLOCKLIST=/etc/unbound/wayviewer-blocklist.conf
OLD_BLOCKLIST=/etc/unbound/unbound.conf.d/wayviewer-blocklist.conf

TARGET_USER=${1:-${SUDO_USER:-}}

if [ "$(id -u)" -ne 0 ]; then
    echo "Ejecuta este instalador como root: sudo $0 [usuario]" >&2
    exit 1
fi
if [ -z "${TARGET_USER}" ] || [ "${TARGET_USER}" = "root" ]; then
    echo "Indica el usuario de la GUI: sudo $0 usuario" >&2
    exit 1
fi
if ! id "${TARGET_USER}" >/dev/null 2>&1; then
    echo "El usuario ${TARGET_USER} no existe." >&2
    exit 1
fi
case "${TARGET_USER}" in
    *[!A-Za-z0-9._-]*)
        echo "Nombre de usuario no válido: ${TARGET_USER}" >&2
        exit 1
        ;;
esac

if [ ! -f "${ROOT}/python/wayviewer_dns_helper.py" ]; then
    echo "No se encuentra python/wayviewer_dns_helper.py en ${ROOT}" >&2
    exit 1
fi
if [ ! -f "${ROOT}/python/domain_control.py" ]; then
    echo "No se encuentra python/domain_control.py en ${ROOT}" >&2
    exit 1
fi

echo "Instalando helper DNS en ${HELPER}"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
    "${ROOT}/python/wayviewer_dns_helper.py" "${HELPER}"
install -o root -g root -m 0644 \
    "${ROOT}/python/domain_control.py" "${MODULE}"
install -d -o root -g root -m 0755 "${STATE_DIR}"

echo "Instalando regla de sudoers en ${SUDOERS}"
if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo no está instalado; instálalo antes de usar el bloqueo web." >&2
    exit 1
fi
SUDOERS_TMP=$(mktemp)
trap 'rm -f "${SUDOERS_TMP}"' EXIT INT TERM
cat > "${SUDOERS_TMP}" <<EOF
# WayViewer: la GUI puede aplicar/consultar la lista de dominios bloqueados.
# Solo se permiten estos dos comandos, con argumentos exactos.
${TARGET_USER} ALL=(root) NOPASSWD: ${HELPER} set, ${HELPER} get
EOF
chmod 0440 "${SUDOERS_TMP}"
if ! command -v visudo >/dev/null 2>&1; then
    echo "No se encontró visudo; no se instala la regla para no romper sudo." >&2
    exit 1
fi
if ! visudo -cf "${SUDOERS_TMP}" >/dev/null; then
    echo "La regla de sudoers no es válida; no se instala." >&2
    exit 1
fi
install -o root -g root -m 0440 "${SUDOERS_TMP}" "${SUDOERS}"

if [ ! -d /etc/unbound ]; then
    cat >&2 <<'EOF'
Aviso: no se encontró /etc/unbound. Instala y configura unbound para que el
bloqueo DNS tenga efecto; el helper y la regla de sudoers ya están listos.
En Void Linux:
  sudo xbps-install -S unbound
  sudo ln -s /etc/sv/unbound /var/service/
EOF
    echo "Instalación terminada (unbound pendiente)."
    exit 0
fi

echo "Preparando la inclusión de la lista en unbound"
if [ -f "${OLD_BLOCKLIST}" ] && \
    grep -q "Generado por wayviewer-dns" "${OLD_BLOCKLIST}"; then
    rm -f "${OLD_BLOCKLIST}"
    echo "Eliminada la lista antigua ${OLD_BLOCKLIST}"
fi
if [ ! -e "${BLOCKLIST}" ]; then
    cat > "${BLOCKLIST}" <<'EOF'
# Generado por wayviewer-dns. No editar a mano.

server:
EOF
    chmod 0644 "${BLOCKLIST}"
fi

BACKUP=""
if [ -f "${CONFIG}" ] && ! grep -Eq \
    "^[[:space:]]*include-toplevel:[[:space:]]*[\"']?${BLOCKLIST}[\"']?[[:space:]]*$" \
    "${CONFIG}"; then
    BACKUP="${CONFIG}.wayviewer.bak"
    cp -p "${CONFIG}" "${BACKUP}"
    cat >> "${CONFIG}" <<EOF

# Añadido por WayViewer para cargar la lista de dominios bloqueados
include-toplevel: "${BLOCKLIST}"
EOF
    echo "Inclusión añadida a ${CONFIG} (copia: ${BACKUP})"
elif [ ! -f "${CONFIG}" ]; then
    echo "Aviso: no existe ${CONFIG}; revisa dónde está la configuración de" >&2
    echo "unbound y añade: include-toplevel: \"${BLOCKLIST}\"" >&2
fi

if command -v unbound-checkconf >/dev/null 2>&1; then
    if ! unbound-checkconf; then
        if [ -n "${BACKUP}" ] && [ -f "${BACKUP}" ]; then
            cp -p "${BACKUP}" "${CONFIG}"
            echo "Se restauró ${CONFIG} porque unbound-checkconf falló." >&2
        fi
        echo "Revisa la configuración de unbound antes de continuar." >&2
        exit 1
    fi
fi

if command -v sv >/dev/null 2>&1 && sv status unbound >/dev/null 2>&1; then
    if ! sv restart unbound; then
        echo "Aviso: no se pudo reiniciar unbound con sv." >&2
    fi
elif command -v systemctl >/dev/null 2>&1 && \
    systemctl is-active --quiet unbound 2>/dev/null; then
    if ! systemctl restart unbound; then
        echo "Aviso: no se pudo reiniciar unbound con systemctl." >&2
    fi
elif command -v unbound-control >/dev/null 2>&1; then
    if ! unbound-control reload; then
        echo "Aviso: no se pudo recargar unbound con unbound-control." >&2
    fi
else
    echo "Aviso: inicia o recarga unbound manualmente para aplicar la lista."
fi

if [ -r /etc/resolv.conf ] && ! grep -Eq \
    '^[[:space:]]*nameserver[[:space:]]+127\.0\.0\.1([[:space:]]|$)' \
    /etc/resolv.conf; then
    echo "Aviso: /etc/resolv.conf no usa 127.0.0.1; el bloqueo DNS no se" >&2
    echo "aplicará hasta que unbound sea el resolver del sistema." >&2
fi

cat <<EOF
Instalación terminada.

  Usuario autorizado: ${TARGET_USER}
  Helper:             ${HELPER}
  Regla sudoers:      ${SUDOERS}
  Lista de unbound:   ${BLOCKLIST}

Abre de nuevo la GUI y usa la sección «Dominios bloqueados». La primera vez
pulsa «Recibir lista» para leer el estado aplicado.
EOF
