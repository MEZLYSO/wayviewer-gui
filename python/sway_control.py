#!/usr/bin/env python3
"""Bloqueo de entrada en Sway para el cliente WayViewer.

El equipo que recibe la transmisión (el «cliente») es quien ejecuta
``swaymsg`` y necesita ``SWAYSOCK``. El panel de administración no necesita
Sway: solo envía una línea de control por el chat TCP y este equipo ejecuta
el bloqueo/desbloqueo localmente y responde con un ACK.

Formato de una línea de control (terminada en ``\\n``)::

    \\x01WC\\t<campo>\\t<campo>...
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading

# Prefijo reservado. No empieza por FILE_MAGIC ("\x01WV") para no chocar con
# el protocolo de archivos del chat.
MAGIC = "\x01WC"

# Tipos de dispositivo de sway-input(5). Un touchpad puede aparecer como
# "pointer" en algunas versiones, por eso se deshabilitan ambos.
TYPES = (
    "keyboard",
    "touchpad",
    "pointer",
    "touch",
    "tablet_tool",
    "tablet_pad",
)

STATE = os.path.expanduser("~/.cache/wayviewer/input-state.json")

_LOCK = threading.Lock()


def available() -> bool:
    """True si podemos hablar con la sesión Sway local."""
    return bool(os.environ.get("SWAYSOCK")) and shutil.which("swaymsg") is not None


def _swaymsg(command: str) -> bool:
    try:
        result = subprocess.run(
            ["swaymsg", command],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _inputs() -> list[dict]:
    try:
        result = subprocess.run(
            ["swaymsg", "-t", "get_inputs", "-r"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _save(saved: dict[str, str]) -> None:
    directory = os.path.dirname(STATE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(saved, handle)
    os.replace(temporary, STATE)


def _load() -> dict[str, str]:
    try:
        with open(STATE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def block() -> bool:
    """Deshabilita teclado y punteros. Guarda el estado previo una sola vez."""
    if not available():
        return False
    with _LOCK:
        if not os.path.exists(STATE):
            saved = {}
            for device in _inputs():
                mode = device.get("libinput", {}).get("send_events")
                if mode:
                    saved[device["identifier"]] = mode
            _save(saved)
        ok = True
        for kind in TYPES:
            ok = _swaymsg(f"input type:{kind} events disabled") and ok
        return ok


def restore(force: bool = False) -> bool:
    """Restaura los modos guardados.

    ``force=True`` vuelve a habilitar los tipos antes de aplicar el snapshot;
    se usa para un «Desbloquear» explícito aunque se haya perdido el estado.
    """
    if not available():
        return False
    with _LOCK:
        saved = _load()
        if force:
            for kind in TYPES:
                _swaymsg(f"input type:{kind} events enabled")
        for identifier, mode in saved.items():
            _swaymsg(f'input "{identifier}" events {mode}')
        try:
            os.remove(STATE)
        except OSError:
            pass
        return True


def heal() -> bool:
    """Restaura un bloqueo que quedó pendiente tras un cierre brusco."""
    if not os.path.exists(STATE) or not available():
        return False
    return restore(force=True)


def blocked() -> bool:
    return os.path.exists(STATE)


# --------------------------------------------------------------- protocolo
def _domains_json(domains: list[str]) -> str:
    return json.dumps(domains, ensure_ascii=True, separators=(",", ":"))


def build_domains_state(domains: list[str]) -> str:
    """Envía al administrador la lista aplicada actualmente."""
    return f"{MAGIC}\tdomains\tstate\t{_domains_json(domains)}"


def build_ack(action: str, ok: bool, detail: str = "") -> str:
    """Línea de confirmación que devuelve el equipo que ejecutó el cambio."""
    clean = " ".join(str(detail).replace("\t", " ").splitlines())[:200]
    state = "ok" if ok else "error"
    return f"{MAGIC}\tack\t{action}\t{state}\t{clean}"


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
