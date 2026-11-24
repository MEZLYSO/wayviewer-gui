#!/usr/bin/env python3
"""Cliente no privilegiado del helper de suspensión WayViewer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

HELPER = os.environ.get("WAYVIEWER_POWER_HELPER", "/usr/local/libexec/wayviewer-power")


def suspend(password: str | None) -> tuple[bool, str]:
    if password is None:
        return False, "se necesita la contraseña del cliente"
    sudo = shutil.which("sudo")
    if sudo is None:
        return False, "sudo no está instalado"
    try:
        process = subprocess.Popen(
            [sudo, "-S", "-p", "", HELPER, "suspend"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(password + "\n")
        process.stdin.close()
        assert process.stdout is not None
        line = process.stdout.readline()
        process.stdout.close()
        if line:
            try:
                response = json.loads(line)
            except ValueError:
                response = None
            if isinstance(response, dict):
                return bool(response.get("ok")), str(response.get("detail") or "")
        # Si sudo/helper terminó sin enviar respuesta, ya no habrá suspensión.
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return False, "el helper no confirmó la suspensión"
        return False, "sudo no aceptó la contraseña o systemctl no pudo suspender"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
