#!/usr/bin/env python3
"""Helper root para mantener la sección de WayViewer en /etc/hosts."""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import domain_control  # noqa: E402

HOSTS = os.environ.get("WAYVIEWER_HOSTS_FILE", "/etc/hosts")
BEGIN = "# BEGIN WAYVIEWER BLOCKED DOMAINS"
END = "# END WAYVIEWER BLOCKED DOMAINS"
MAX_INPUT = 1024 * 1024


def atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=".wayviewer-hosts-", delete=False
    )
    temporary = handle.name
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def entries(domains: list[str]) -> list[str]:
    lines = [BEGIN]
    for domain in domains:
        lines.append(f"127.0.0.1 {domain}")
        lines.append(f"::1 {domain}")
    lines.append(END)
    return lines


def read_hosts() -> list[str]:
    try:
        with open(HOSTS, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def read_domains() -> list[str]:
    lines = read_hosts()
    try:
        start = lines.index(BEGIN) + 1
        end = lines.index(END, start)
    except ValueError:
        return []
    found = []
    for line in lines[start:end]:
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("127.0.0.1", "::1"):
            domain = domain_control.normalize(parts[1])
            if domain and domain not in found:
                found.append(domain)
    valid, error = domain_control.validate_list(found)
    return valid if not error else []


def set_domains(items: object) -> dict:
    domains, error = domain_control.validate_list(items)
    if error:
        return {"ok": False, "detail": error, "domains": read_domains()}
    old = read_hosts()
    kept = []
    inside = False
    for line in old:
        if line == BEGIN:
            inside = True
            continue
        if inside and line == END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    while kept and kept[-1] == "":
        kept.pop()
    if domains:
        if kept:
            kept.append("")
        kept.extend(entries(domains))
    atomic_write(HOSTS, "\n".join(kept) + "\n")
    return {"ok": True, "detail": f"{len(domains)} dominios aplicados en /etc/hosts", "domains": domains}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("set", "get"):
        print("uso: wayviewer-hosts set|get", file=sys.stderr)
        return 2
    if args[0] == "get":
        response = {"ok": True, "detail": f"{len(read_domains())} dominios", "domains": read_domains()}
    else:
        raw = sys.stdin.read(MAX_INPUT + 1)
        try:
            items = json.loads(raw) if len(raw) <= MAX_INPUT else None
        except ValueError:
            items = None
        response = set_domains(items) if items is not None else {
            "ok": False, "detail": "JSON inválido o lista demasiado grande", "domains": read_domains()
        }
    print(json.dumps(response, ensure_ascii=True))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
