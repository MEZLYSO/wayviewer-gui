#!/usr/bin/env python3
"""Protocolo de control remoto del panel de administración.

Este equipo no necesita Sway ni ``SWAYSOCK``: solo envía líneas de control
por el chat TCP y recibe el estado o el ACK del cliente. El cliente es quien
ejecuta ``swaymsg`` y las reglas locales de nftables.

Formato de una línea de control (terminada en ``\\n``)::

    \\x01WC\\t<campo>\\t<campo>...
"""

from __future__ import annotations

import json

# Prefijo reservado. No empieza por FILE_MAGIC ("\x01WV") para no chocar con
# el protocolo de archivos del chat.
MAGIC = "\x01WC"


# --------------------------------------------------------------- protocolo
def build_command(action: str) -> str:
    """Línea para pedir «block» o «unblock» al cliente."""
    return f"{MAGIC}\t{action}"


def describe(action: str) -> str:
    """Texto legible para las órdenes de entrada enviadas al cliente."""
    return {
        "block": "bloquear la entrada",
        "unblock": "desbloquear la entrada",
    }.get(action, action)


def build_ping_command(blocked: bool) -> str:
    return f"{MAGIC}\t{'ping-block' if blocked else 'ping-unblock'}"


def build_suspend_command() -> str:
    """Pide al cliente suspender su sistema localmente."""
    return f"{MAGIC}\tsuspend"


def describe_ping(blocked: bool) -> str:
    return "bloquear el ping" if blocked else "permitir el ping"


def build_hosts_get() -> str:
    return f"{MAGIC}\thosts\tget"


def build_hosts_set(domains: list[str]) -> str:
    import json
    payload = json.dumps(domains, ensure_ascii=True, separators=(",", ":"))
    return f"{MAGIC}\thosts\tset\t{payload}"


def parse(line: str) -> tuple[dict | None, str]:
    """Interpreta una línea de control recibida del cliente.

    Sin esta función el hilo lector del chat no puede procesar el estado ni el
    ACK de /etc/hosts y termina cerrando la conexión.
    """
    if not line.startswith(MAGIC):
        return None, "no es una línea de control"
    rest = line[len(MAGIC):]
    if rest.startswith("\t"):
        rest = rest[1:]
    if not rest:
        return None, "formato inválido"
    return {"fields": rest.split("\t")}, ""
