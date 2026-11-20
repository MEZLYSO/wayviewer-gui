#!/usr/bin/env python3
"""Lista de dominios bloqueados y cliente del helper de /etc/hosts.

La GUI se ejecuta como el usuario de la sesión y nunca escribe directamente
en ``/etc/hosts``. Para aplicar la lista invoca con sudo el helper
``/usr/local/libexec/wayviewer-hosts``, que conserva el resto del archivo.

El filtro es DNS, no de puertos: se bloquea la resolución del dominio y sus
subdominios, pero el resto de HTTPS sigue funcionando.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

MAX_DOMAINS = 500
MAX_DOMAIN_LENGTH = 253

HELPER = os.environ.get(
    "WAYVIEWER_HOSTS_HELPER", "/usr/local/libexec/wayviewer-hosts"
)
REQUEST_TIMEOUT = 60.0

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


def _request(
    command: str,
    domains: list[str] | None = None,
    password: str | None = None,
) -> tuple[dict | None, str]:
    """Ejecuta el helper y devuelve (respuesta, error).

    Nunca se pasa por shell: los argumentos son fijos (``set``/``get``) y la
    lista viaja por la entrada estándar, así que el contenido enviado no puede
    convertirse en opciones del helper.
    """
    if command not in ("set", "get"):
        return None, "comando interno inválido"

    sudo = shutil.which("sudo")
    if sudo is None:
        return None, "sudo no está instalado"
    if command == "set" and password is None:
        return None, "se necesita la contraseña del cliente"
    # -k fuerza una nueva autenticación cuando el administrador aplica cada
    # cambio; la contraseña nunca se guarda ni se escribe en la línea de
    # comandos.
    argv = [sudo, "-k", "-S", "-p", "", HELPER, command]

    payload = ""
    if command == "set":
        payload = json.dumps(domains or [], ensure_ascii=True) + "\n"
    stdin_payload = ((password or "") + "\n" if command == "set" else "\n")
    stdin_payload += payload

    try:
        result = subprocess.run(
            argv,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return None, (
            f"helper de hosts no instalado ({HELPER}); ejecuta "
            "packaging/install-dns-helper.sh"
        )
    except PermissionError:
        return None, f"sin permiso para ejecutar {HELPER}"
    except subprocess.TimeoutExpired:
        return None, "el helper de hosts no respondió a tiempo"
    except OSError as exc:
        return None, f"no se pudo ejecutar el helper de hosts: {exc}"

    response = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            response = candidate
            break
    if response is not None:
        return response, ""

    stderr = " ".join(result.stderr.split())[:300]
    lowered = stderr.lower()
    if "password is required" in lowered or "no tty present" in lowered:
        return None, (
            "la contraseña del cliente no fue aceptada por sudo"
        )
    if "not allowed" in lowered or "not in the sudoers" in lowered:
        return None, (
            "sudo no autoriza el helper de hosts"
        )
    detail = stderr or f"el helper de hosts terminó con código {result.returncode}"
    return None, f"helper de hosts: {detail}"


def _result(
    response: dict | None, error: str, fallback: list[str]
) -> tuple[bool, list[str], str]:
    if error:
        return False, fallback, error
    assert response is not None

    domains, list_error = validate_list(response.get("domains", []))
    if list_error:
        domains = fallback
    else:
        try:
            save_local(domains)
        except OSError:
            pass

    if not response.get("ok"):
        detail = str(response.get("detail") or "el helper de hosts rechazó la petición")
        return False, domains, detail
    detail = str(response.get("detail") or f"{len(domains)} dominios")
    return True, domains, detail


def apply(
    domains: list[str], password: str | None = None
) -> tuple[bool, list[str], str]:
    """Reemplaza la lista de dominios bloqueados en el cliente."""
    valid, error = validate_list(domains)
    if error:
        return False, load_local(), error
    response, request_error = _request("set", valid, password)
    return _result(response, request_error, load_local())


def get_state() -> tuple[bool, list[str], str]:
    """Devuelve la última lista aplicada guardada por el cliente."""
    domains = load_local()
    return True, domains, f"{len(domains)} dominios"


def available() -> bool:
    """True si el helper está instalado (no garantiza que sudo lo permita)."""
    return os.access(HELPER, os.X_OK)
