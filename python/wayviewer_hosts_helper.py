#!/usr/bin/env python3
"""Helper root que mantiene una sección WayViewer dentro de /etc/hosts."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hosts_control  # noqa: E402

HOSTS = os.environ.get("WAYVIEWER_HOSTS_FILE", "/etc/hosts")
BEGIN = "# BEGIN WAYVIEWER BLOCKED HOSTS"
END = "# END WAYVIEWER BLOCKED HOSTS"
MAX_INPUT = 1024 * 1024


def read_lines() -> list[str]:
    try:
        with open(HOSTS, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def read_domains() -> list[str]:
    lines = read_lines()
    try:
        start = lines.index(BEGIN) + 1
        end = lines.index(END, start)
    except ValueError:
        return []
    found = []
    for line in lines[start:end]:
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("127.0.0.1", "::1"):
            domain = hosts_control.normalize(parts[1])
            if domain and domain not in found:
                found.append(domain)
    valid, error = hosts_control.validate_list(found)
    return valid if not error else []


def set_domains(items: object) -> dict:
    domains, error = hosts_control.validate_list(items)
    if error:
        return {"ok": False, "detail": error, "domains": read_domains()}
    old = read_lines()
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
        kept.extend([BEGIN])
        for domain in domains:
            kept.append(f"127.0.0.1 {domain}")
            kept.append(f"::1 {domain}")
        kept.append(END)
    directory = os.path.dirname(HOSTS) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = HOSTS + ".wayviewer.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, HOSTS)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return {"ok": False, "detail": str(exc), "domains": read_domains()}
    return {"ok": True, "detail": f"{len(domains)} dominios aplicados en /etc/hosts", "domains": domains}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("set", "get"):
        print("uso: wayviewer-hosts set|get", file=sys.stderr)
        return 2
    if args[0] == "get":
        domains = read_domains()
        response = {"ok": True, "detail": f"{len(domains)} dominios", "domains": domains}
    else:
        raw = sys.stdin.read(MAX_INPUT + 1)
        try:
            payload = raw
            try:
                items = json.loads(payload)
            except (ValueError, json.JSONDecodeError):
                _password, separator, payload = raw.partition("\n")
                items = json.loads(payload) if separator else None
            items = items if len(payload) <= MAX_INPUT else None
        except (ValueError, json.JSONDecodeError):
            items = None
        response = set_domains(items) if items is not None else {"ok": False, "detail": "JSON inválido o lista demasiado grande", "domains": read_domains()}
    print(json.dumps(response, ensure_ascii=True))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
