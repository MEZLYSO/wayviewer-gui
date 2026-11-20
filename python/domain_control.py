#!/usr/bin/env python3
"""Lista de dominios bloqueados del panel de administración.

La GUI valida y guarda la lista localmente, y la envía al cliente por el
chat. Este equipo no ejecuta el helper DNS ni escribe en ``/etc/unbound``:
el bloqueo se aplica en el equipo cliente.

El filtro es DNS, no de puertos: se bloquea la resolución del dominio y sus
subdominios, pero el resto de HTTPS sigue funcionando.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
from urllib.parse import urlsplit

MAX_DOMAINS = 500
MAX_DOMAIN_LENGTH = 253

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "wayviewer",
)
STORE = os.path.join(CONFIG_DIR, "blocked-domains.json")

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_STORE_LOCK = threading.Lock()


def normalize(value: object) -> str | None:
    """Devuelve el dominio normalizado o None si no es válido.

    Acepta también URLs y dominios con ``*.``; el bloqueo por ``local-zone``
    ya cubre los subdominios.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    try:
        if "://" in text:
            host = urlsplit(text).hostname or ""
        else:
            host = urlsplit("//" + text).hostname or ""
    except ValueError:
        return None

    host = host.strip().strip(".").lower()
    if host.startswith("*."):
        host = host[2:]
    if not host:
        return None

    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    host = host.lower().strip(".")
    if not host or len(host) > MAX_DOMAIN_LENGTH:
        return None

    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass

    labels = host.split(".")
    if len(labels) < 2:
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None
    return host


def validate_list(items: object) -> tuple[list[str] | None, str]:
    """Normaliza y valida una lista completa. Devuelve (dominios, error)."""
    if not isinstance(items, list):
        return None, "la lista de dominios no es válida"
    if len(items) > MAX_DOMAINS:
        return None, f"máximo {MAX_DOMAINS} dominios"

    valid: list[str] = []
    invalid: list[str] = []
    for item in items:
        domain = normalize(item)
        if domain is None:
            invalid.append(str(item)[:80])
        elif domain not in valid:
            valid.append(domain)

    if invalid:
        preview = ", ".join(invalid[:3])
        return None, f"dominio inválido: {preview}"

    # Si está el dominio padre, sus subdominios son redundantes.
    clean = []
    for domain in sorted(valid):
        if any(
            domain != other and domain.endswith("." + other)
            for other in valid
        ):
            continue
        clean.append(domain)
    return clean, ""


def load_local() -> list[str]:
    """Lista guardada en el equipo local (última aplicada o editada)."""
    try:
        with open(STORE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    domains, error = validate_list(data)
    return domains if not error else []


def save_local(domains: list[str]) -> None:
    """Guarda la lista de forma atómica en la configuración del usuario."""
    payload = json.dumps(domains, ensure_ascii=True) + "\n"
    with _STORE_LOCK:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_DIR,
            prefix=".blocked-domains-",
            delete=False,
        )
        temporary = handle.name
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, STORE)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
