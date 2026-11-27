#!/usr/bin/env python3
"""Cliente del helper root para gestionar /etc/hosts."""
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
HELPER = os.environ.get("WAYVIEWER_HOSTS_HELPER", "/usr/local/libexec/wayviewer-hosts")
REQUEST_TIMEOUT = 60.0
_STORE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "wayviewer", "blocked-hosts.json")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_LOCK = threading.Lock()


def normalize(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        host = urlsplit(text).hostname if "://" in text else urlsplit("//" + text).hostname
    except ValueError:
        return None
    host = (host or "").strip().strip(".").lower()
    if host.startswith("*."):
        host = host[2:]
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not host or len(host) > MAX_DOMAIN_LENGTH:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    return host


def validate_list(items: object) -> tuple[list[str] | None, str]:
    if not isinstance(items, list):
        return None, "la lista de hosts no es válida"
    if len(items) > MAX_DOMAINS:
        return None, f"máximo {MAX_DOMAINS} dominios"
    valid, invalid = [], []
    for item in items:
        domain = normalize(item)
        if domain is None:
            invalid.append(str(item)[:80])
        elif domain not in valid:
            valid.append(domain)
    if invalid:
        return None, "dominio inválido: " + ", ".join(invalid[:3])
    # Conserva el orden de entrada para que /etc/hosts refleje la lista tal
    # como la administra el usuario.
    return valid, ""


def load_local() -> list[str]:
    try:
        with open(_STORE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    valid, error = validate_list(data)
    return valid if not error else []


def save_local(domains: list[str]) -> None:
    directory = os.path.dirname(_STORE)
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps(domains, ensure_ascii=True) + "\n"
    with _LOCK:
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".hosts-", delete=False)
        temporary = handle.name
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, _STORE)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _request(command: str, domains: list[str] | None = None, password: str | None = None) -> tuple[dict | None, str]:
    if command not in ("set", "get"):
        return None, "comando interno inválido"
    if command == "set" and password is None:
        return None, "se necesita la contraseña del cliente"
    sudo = shutil.which("sudo")
    if sudo is None:
        return None, "sudo no está instalado"
    argv = [sudo, "-S", "-p", "", HELPER, command]
    payload = json.dumps(domains or [], ensure_ascii=True) + "\n" if command == "set" else ""
    stdin = (password + "\n" if password is not None else "") + payload
    try:
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=REQUEST_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    for line in reversed(result.stdout.splitlines()):
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if isinstance(response, dict):
            return response, ""
    return None, (result.stderr or f"helper hosts terminó con código {result.returncode}").strip()[:300]


def apply(domains: list[str], password: str | None) -> tuple[bool, list[str], str]:
    valid, error = validate_list(domains)
    if error:
        return False, load_local(), error
    response, request_error = _request("set", valid, password)
    if request_error:
        return False, load_local(), request_error
    assert response is not None
    domains_out, list_error = validate_list(response.get("domains", []))
    if list_error:
        domains_out = load_local()
    else:
        try:
            save_local(domains_out)
        except OSError:
            pass
    if not response.get("ok"):
        return False, domains_out or [], str(response.get("detail") or "el helper hosts rechazó la petición")
    return True, domains_out or [], str(response.get("detail") or f"{len(domains_out)} dominios")


def get_state(password: str | None = None) -> tuple[bool, list[str], str]:
    response, error = _request("get", password=password)
    if error:
        return False, load_local(), error
    assert response is not None
    domains, list_error = validate_list(response.get("domains", []))
    if list_error:
        domains = load_local()
    return bool(response.get("ok")), domains or [], str(response.get("detail") or f"{len(domains)} dominios")
