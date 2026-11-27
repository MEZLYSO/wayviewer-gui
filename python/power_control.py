#!/usr/bin/env python3
"""Cliente no privilegiado del helper de suspensión WayViewer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

HELPER = os.environ.get("WAYVIEWER_POWER_HELPER", "/usr/local/libexec/wayviewer-power")


def validate_password(password: str) -> tuple[bool, str]:
    """Comprueba la contraseña local de sudo sin ejecutar comandos root."""
    sudo = shutil.which("sudo")
    if sudo is None:
        return False, "sudo no está instalado"
    try:
        result = subprocess.run(
            [sudo, "-S", "-p", "", "-v"],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, "contraseña validada"
    detail = (result.stderr or "sudo rechazó la contraseña").strip()
    return False, detail[:300]


def suspend(password: str | None) -> tuple[bool, str]:
    if password is None:
        return False, "se necesita la contraseña del cliente"
    sudo = shutil.which("sudo")
    if sudo is None:
        return False, "sudo no está instalado"
    try:
        result = subprocess.run(
            [sudo, "-S", "-p", "", HELPER, "suspend"],
            input=password + "\n",
            capture_output=True,
            text=True,
            # systemctl suspend puede mantener el proceso detenido hasta que
            # el usuario reactive el equipo; no aplicar timeout aquí.
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    for line in reversed(result.stdout.splitlines()):
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if isinstance(response, dict):
            return bool(response.get("ok")), str(response.get("detail") or "")
    detail = (result.stderr or "sudo/helper no confirmó la suspensión").strip()
    return False, detail[:300]
