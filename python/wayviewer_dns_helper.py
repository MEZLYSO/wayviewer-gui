#!/usr/bin/env python3
"""Helper privilegiado para aplicar la lista de dominios bloqueados.

Se instala como ``/usr/local/libexec/wayviewer-dns`` y se invoca con
``sudo -n`` desde la GUI. Solo acepta los comandos ``set`` y ``get``:

    echo '["example.com"]' | sudo -n /usr/local/libexec/wayviewer-dns set
    sudo -n /usr/local/libexec/wayviewer-dns get

Escribe la configuración de unbound y recarga el servicio. No ejecuta
comandos recibidos por la entrada estándar.

Para pruebas se pueden usar las variables de entorno
``WAYVIEWER_DNS_CONFIG``, ``WAYVIEWER_DNS_STATE`` y
``WAYVIEWER_DNS_RELOAD``. ``sudo`` las elimina por defecto.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import domain_control  # noqa: E402

DEFAULT_CONFIG = "/etc/unbound/wayviewer-blocklist.conf"
DEFAULT_STATE = "/var/lib/wayviewer/blocked-domains.json"
MAX_INPUT = 1024 * 1024
# Las variables de entorno solo se aceptan sin privilegios (pruebas). Así una
# regla sudoers con env_keep no puede redirigir ficheros ni el comando de
# recarga a un valor controlado por el usuario.
TEST_MODE = os.geteuid() != 0
CONFIG = (
    os.environ.get("WAYVIEWER_DNS_CONFIG", DEFAULT_CONFIG)
    if TEST_MODE
    else DEFAULT_CONFIG
)
STATE = (
    os.environ.get("WAYVIEWER_DNS_STATE", DEFAULT_STATE)
    if TEST_MODE
    else DEFAULT_STATE
)


def atomic_write(path: str, content: str, mode: int = 0o644) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o755, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".wayviewer-",
        delete=False,
    )
    temporary = handle.name
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render(domains: list[str]) -> str:
    # Se incluye desde unbound.conf con include-toplevel, así que el fichero
    # necesita abrir su propia cláusula server.
    lines = ["# Generado por wayviewer-dns. No editar a mano.", "", "server:"]
    for domain in domains:
        lines.append(f'    local-zone: "{domain}." always_nxdomain')
    lines.append("")
    return "\n".join(lines)


def read_state() -> list[str]:
    try:
        with open(STATE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    domains, error = domain_control.validate_list(data)
    return domains if not error else []


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _restore(path: str, content: str | None) -> None:
    if content is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    else:
        atomic_write(path, content)


def reload_command() -> list[str] | None:
    override = (
        os.environ.get("WAYVIEWER_DNS_RELOAD") if TEST_MODE else None
    )
    if override is not None:
        return shlex.split(override) if override.strip() else None
    if os.path.isdir("/run/systemd/system") and shutil.which("systemctl"):
        return ["systemctl", "restart", "unbound"]
    if shutil.which("sv"):
        return ["sv", "restart", "unbound"]
    if shutil.which("unbound-control"):
        return ["unbound-control", "reload"]
    return None


def check_config() -> tuple[bool, str]:
    checker = shutil.which("unbound-checkconf")
    if not checker:
        return True, ""
    try:
        result = subprocess.run(
            [checker], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"unbound-checkconf: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        return False, f"unbound-checkconf: {detail}"
    return True, ""


def reload_unbound() -> tuple[bool, str]:
    argv = reload_command()
    if not argv:
        return False, (
            "no se encontró cómo recargar unbound "
            "(systemctl, sv o unbound-control)"
        )
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        if not detail:
            detail = " ".join(argv)
        return False, f"recarga falló ({result.returncode}): {detail}"
    return True, ""


def set_domains(items: object) -> dict:
    domains, error = domain_control.validate_list(items)
    if error:
        return {"ok": False, "detail": error, "domains": read_state()}

    previous_state = read_state()
    previous_config = _read_text(CONFIG)
    try:
        atomic_write(CONFIG, render(domains))
        atomic_write(STATE, json.dumps(domains, ensure_ascii=True) + "\n")
    except OSError as exc:
        try:
            _restore(CONFIG, previous_config)
        except OSError:
            pass
        return {
            "ok": False,
            "detail": f"no se pudo escribir la configuración: {exc}",
            "domains": previous_state,
        }

    ok, detail = check_config()
    if ok:
        ok, detail = reload_unbound()
    if not ok:
        # La recarga no ha surtido efecto: deja el estado anterior para que la
        # GUI muestre lo que unbound sigue aplicando.
        try:
            _restore(CONFIG, previous_config)
            _restore(
                STATE,
                json.dumps(previous_state, ensure_ascii=True) + "\n"
                if previous_state
                else None,
            )
        except OSError as exc:
            detail = f"{detail} (no se pudo restaurar: {exc})"
        return {"ok": False, "detail": detail, "domains": previous_state}

    return {
        "ok": True,
        "detail": f"{len(domains)} dominios aplicados",
        "domains": domains,
    }


def get_domains() -> dict:
    domains = read_state()
    return {
        "ok": True,
        "detail": f"{len(domains)} dominios",
        "domains": domains,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("set", "get"):
        print(
            "uso: wayviewer-dns set|get", file=sys.stderr
        )
        return 2

    if args[0] == "set":
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            response = {
                "ok": False,
                "detail": "lista demasiado grande",
                "domains": read_state(),
            }
        else:
            try:
                items = json.loads(raw)
            except ValueError:
                response = {
                    "ok": False,
                    "detail": "JSON inválido",
                    "domains": read_state(),
                }
            else:
                response = set_domains(items)
    else:
        response = get_domains()

    print(json.dumps(response, ensure_ascii=True))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
