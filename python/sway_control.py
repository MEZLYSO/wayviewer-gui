#!/usr/bin/env python3
"""Protocolo de control remoto del panel de administración.

Este equipo no necesita Sway ni ``SWAYSOCK``: solo envía líneas de control
por el chat TCP y recibe el estado o el ACK del cliente. El cliente es quien
ejecuta ``swaymsg`` y quien aplica la lista de dominios.

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


def build_domains_get() -> str:
    """Pide al cliente su lista de dominios bloqueados."""
    return f"{MAGIC}\tdomains\tget"


def _domains_json(domains: list[str]) -> str:
    return json.dumps(domains, ensure_ascii=True, separators=(",", ":"))


def build_domains_set(domains: list[str]) -> str:
    """Reemplaza la lista de dominios bloqueados del cliente."""
    return f"{MAGIC}\tdomains\tset\t{_domains_json(domains)}"


def parse(line: str) -> tuple[dict | None, str]:
    """Devuelve (datos, error). ``datos["fields"]`` es la lista de campos."""
    if not line.startswith(MAGIC):
        return None, "no es una línea de control"
    rest = line[len(MAGIC):]
    if rest.startswith("\t"):
        rest = rest[1:]
    if not rest:
        return None, "formato inválido"
    return {"fields": rest.split("\t")}, ""


def describe(action: str) -> str:
    return {
        "block": "bloquear la entrada",
        "unblock": "desbloquear la entrada",
    }.get(action, action)


def build_ping_command(blocked: bool) -> str:
    return f"{MAGIC}\t{'ping-block' if blocked else 'ping-unblock'}"


def describe_ping(blocked: bool) -> str:
    return "bloquear el ping" if blocked else "permitir el ping"
