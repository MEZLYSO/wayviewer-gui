#!/usr/bin/env python3
"""Cliente del helper nftables para bloquear ICMP saliente."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

HELPER = os.environ.get("WAYVIEWER_NFT_HELPER", "/usr/local/libexec/wayviewer-nft")


def set_blocked(blocked: bool, password: str | None) -> tuple[bool, str]:
    if password is None:
        return False, "se necesita la contraseña del cliente"
    sudo = shutil.which("sudo")
    if sudo is None:
        return False, "sudo no está instalado"
    command = "ping-block" if blocked else "ping-unblock"
    try:
        result = subprocess.run(
            [sudo, "-k", "-S", "-p", "", HELPER, command],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=30,
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
    return False, (result.stderr or "el helper nftables falló").strip()[:300]
