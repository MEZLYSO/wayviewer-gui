#!/usr/bin/env python3
"""Helper root para bloquear las IPs actuales de dominios con nftables."""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import domain_control  # noqa: E402

FAMILY = "inet"
TABLE = "wayviewer"
PING_TABLE = "wayviewer_ping"
MAX_INPUT = 1024 * 1024


def run(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, input=input_text, capture_output=True, text=True, timeout=30, check=False
    )


def resolve(domain: str, family: str) -> set[str]:
    try:
        result = run(["getent", family, domain])
    except (OSError, subprocess.SubprocessError):
        return set()
    addresses = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        address = parts[0]
        if family == "ahostsv4" and address != "127.0.0.1":
            addresses.add(address)
        elif family == "ahostsv6" and address not in ("::1", "0:0:0:0:0:0:0:1"):
            addresses.add(address)
    return addresses


def nft_base() -> str:
    return "\n".join(
        [
            "add table inet wayviewer",
            "add set inet wayviewer blocked4 { type ipv4_addr; flags interval; }",
            "add set inet wayviewer blocked6 { type ipv6_addr; flags interval; }",
            "add chain inet wayviewer output { type filter hook output priority -100; policy accept; }",
            "add rule inet wayviewer output ip daddr @blocked4 reject",
            "add rule inet wayviewer output ip6 daddr @blocked6 reject",
            "",
        ]
    )


def set_domains(items: object) -> dict:
    domains, error = domain_control.validate_list(items)
    if error:
        return {"ok": False, "detail": error, "domains": domain_control.load_local()}

    ipv4: set[str] = set()
    ipv6: set[str] = set()
    for domain in domains:
        ipv4.update(resolve(domain, "ahostsv4"))
        ipv6.update(resolve(domain, "ahostsv6"))

    old = run(["nft", "delete", "table", FAMILY, TABLE])
    # La tabla puede no existir en la primera ejecución.
    if old.returncode not in (0, 1):
        detail = (old.stderr or old.stdout).strip()[:300]
        return {"ok": False, "detail": f"no se pudo limpiar nftables: {detail}", "domains": domain_control.load_local()}

    created = run(["nft", "-f", "-"], nft_base())
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()[:300]
        return {"ok": False, "detail": f"nftables rechazó la configuración: {detail}", "domains": domain_control.load_local()}

    for set_name, addresses in (("blocked4", ipv4), ("blocked6", ipv6)):
        for address in sorted(addresses):
            result = run(["nft", "add", "element", FAMILY, TABLE, set_name, "{" + address + "}"])
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[:300]
                run(["nft", "delete", "table", FAMILY, TABLE])
                return {"ok": False, "detail": f"no se pudo añadir {address}: {detail}", "domains": domain_control.load_local()}

    return {
        "ok": True,
        "detail": f"{len(domains)} dominios, {len(ipv4)} IPv4 y {len(ipv6)} IPv6 bloqueadas",
        "domains": domains,
    }


def set_ping_blocked(blocked: bool) -> dict:
    if blocked:
        run(["nft", "delete", "table", FAMILY, PING_TABLE])
        script = "\n".join(
            [
                f"add table {FAMILY} {PING_TABLE}",
                f"add chain {FAMILY} {PING_TABLE} output {{ type filter hook output priority -100; policy accept; }}",
                f"add rule {FAMILY} {PING_TABLE} output icmp type echo-request drop",
                f"add rule {FAMILY} {PING_TABLE} output icmpv6 type echo-request drop",
                "",
            ]
        )
        result = run(["nft", "-f", "-"], script)
        detail = (result.stderr or result.stdout).strip()[:300]
        return {"ok": result.returncode == 0, "detail": "ping bloqueado" if result.returncode == 0 else detail, "domains": []}
    result = run(["nft", "delete", "table", FAMILY, PING_TABLE])
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout).strip()[:300]
        return {"ok": False, "detail": detail, "domains": []}
    return {"ok": True, "detail": "ping permitido", "domains": []}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("set", "get", "clear", "ping-block", "ping-unblock"):
        print("uso: wayviewer-nft set|get|clear|ping-block|ping-unblock", file=sys.stderr)
        return 2
    if args[0] == "ping-block":
        response = set_ping_blocked(True)
    elif args[0] == "ping-unblock":
        response = set_ping_blocked(False)
    elif args[0] == "clear":
        result = run(["nft", "delete", "table", FAMILY, TABLE])
        response = {"ok": result.returncode in (0, 1), "detail": "bloqueo nftables eliminado", "domains": []}
    elif args[0] == "get":
        domains = domain_control.load_local()
        response = {"ok": True, "detail": f"{len(domains)} dominios", "domains": domains}
    else:
        raw = sys.stdin.read(MAX_INPUT + 1)
        try:
            items = json.loads(raw) if len(raw) <= MAX_INPUT else None
        except ValueError:
            items = None
        response = set_domains(items) if items is not None else {
            "ok": False, "detail": "JSON inválido o lista demasiado grande", "domains": domain_control.load_local()
        }
    print(json.dumps(response, ensure_ascii=True))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
