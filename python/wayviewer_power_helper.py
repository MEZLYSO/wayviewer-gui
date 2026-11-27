#!/usr/bin/env python3
"""Helper root limitado a solicitar systemctl suspend."""
from __future__ import annotations

import json
import subprocess
import sys

SYSTEMCTL = "/usr/bin/systemctl"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["suspend"]:
        print("uso: wayviewer-power suspend", file=sys.stderr)
        return 2
    try:
        result = subprocess.run(
            [SYSTEMCTL, "suspend"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "detail": str(exc)}), flush=True)
        return 1
    if result.returncode != 0:
        detail = (result.stderr or "systemctl suspend falló").strip()
        print(json.dumps({"ok": False, "detail": detail}), flush=True)
        return result.returncode
    print(json.dumps({"ok": True, "detail": "suspensión completada"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
