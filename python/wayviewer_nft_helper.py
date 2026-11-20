#!/usr/bin/env python3
"""Helper root para bloquear o permitir ICMP saliente con nftables."""
from __future__ import annotations

import json
import subprocess
import sys

FAMILY = "inet"
TABLE = "wayviewer_ping"


def run(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, input=input_text, capture_output=True, text=True, timeout=30, check=False
    )


def set_blocked(blocked: bool) -> dict:
    if not blocked:
        result = run(["nft", "delete", "table", FAMILY, TABLE])
        if result.returncode not in (0, 1):
            return {"ok": False, "detail": (result.stderr or result.stdout).strip()[:300]}
        return {"ok": True, "detail": "ping permitido"}

    run(["nft", "delete", "table", FAMILY, TABLE])
    script = "\n".join(
        [
            f"add table {FAMILY} {TABLE}",
            f"add chain {FAMILY} {TABLE} output {{ type filter hook output priority -100; policy accept; }}",
            f"add rule {FAMILY} {TABLE} output icmp type echo-request drop",
            f"add rule {FAMILY} {TABLE} output icmpv6 type echo-request drop",
            "",
        ]
    )
    result = run(["nft", "-f", "-"], script)
    if result.returncode != 0:
        return {"ok": False, "detail": (result.stderr or result.stdout).strip()[:300]}
    return {"ok": True, "detail": "ping bloqueado"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("ping-block", "ping-unblock"):
        print("uso: wayviewer-nft ping-block|ping-unblock", file=sys.stderr)
        return 2
    response = set_blocked(args[0] == "ping-block")
    print(json.dumps(response, ensure_ascii=True))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
