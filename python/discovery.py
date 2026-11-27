#!/usr/bin/env python3
"""Descubrimiento de clientes WayViewer por UDP.

El cliente emite un anuncio pequeño cada pocos segundos. Lo envía por broadcast
para la LAN y, si se configura ``WAYVIEWER_SERVER_IP``, también directamente a
esa IP para redes VPN donde el broadcast no cruza. El panel de administración
escucha en UDP 9501 y mantiene la tabla de clientes. Sin autenticación: usar
solo en redes de confianza.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable

PORT = int(os.environ.get("WAYVIEWER_DISCOVERY_PORT", "9501"))
SERVER_IP = os.environ.get("WAYVIEWER_SERVER_IP", "").strip()
MAGIC = "WVCLIENT1"
INTERVAL = 5.0
EXPIRE_AFTER = 18.0
MAX_PACKET = 4096
queue_like = Callable[[tuple[str, str]], None]


def client_id() -> str:
    """Identificador estable del equipo, sin depender de root."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value:
            return value[:64]
    return f"node-{uuid.getnode():012x}"[:64]


def server_targets() -> list[str]:
    """IPs del admin a las que enviar el anuncio de forma dirigida."""
    targets = []
    for item in SERVER_IP.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            continue
        targets.append(value)
    return targets


def build(name: str, ip: str, port: int, version: str = "1") -> str:
    """Construye el anuncio que envía el cliente."""
    return json.dumps(
        {
            "m": MAGIC,
            "id": client_id(),
            "name": str(name)[:80].strip() or "cliente",
            "ip": ip,
            "port": int(port),
            "v": str(version)[:20],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def parse(payload: bytes) -> dict | None:
    """Valida un anuncio recibido y lo convierte en diccionario."""
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("m") != MAGIC:
        return None
    identifier = str(data.get("id") or "")[:64].strip()
    name = str(data.get("name") or "")[:80].strip() or "cliente"
    ip_text = str(data.get("ip") or "").strip()
    if not identifier:
        return None
    try:
        ipaddress.ip_address(ip_text)
        port = int(data.get("port") or 0)
    except (ValueError, TypeError):
        return None
    if not 1 <= port <= 65535:
        return None
    return {
        "id": identifier,
        "name": name,
        "ip": ip_text,
        "port": port,
        "version": str(data.get("v") or "")[:20],
        "seen": time.time(),
    }


def start_broadcaster(
    state: Callable[[], tuple[str, str, int, str]],
    stop: threading.Event,
    interval: float = INTERVAL,
) -> threading.Thread:
    """Publica el anuncio en LAN y, si aplica, hacia el admin por VPN."""

    def loop() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        targets = ["<broadcast>", *server_targets()]
        try:
            while not stop.wait(interval):
                try:
                    name, ip, port, version = state()
                except Exception:  # noqa: BLE001 - el anuncio nunca debe romper la GUI
                    continue
                if not ip:
                    continue
                packet = build(name, ip, port, version).encode("utf-8")
                for target in targets:
                    try:
                        sock.sendto(packet, (target, PORT))
                    except OSError:
                        # Un destino puede fallar sin impedir los demás.
                        continue
        finally:
            sock.close()

    thread = threading.Thread(
        target=loop, name="wayviewer-discovery-send", daemon=True
    )
    thread.start()
    return thread


class Listener:
    """Escucha anuncios y expone la lista de clientes vistos recientemente."""

    def __init__(self, events: queue_like) -> None:
        self.events = events
        self._clients: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._last_event = 0.0

    def start(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", PORT))
        except OSError as exc:
            sock.close()
            self.events.put(
                (
                    "chat_system",
                    f"Descubrimiento no disponible en UDP {PORT}: {exc}",
                )
            )
            return False
        sock.settimeout(0.5)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._loop, name="wayviewer-discovery-listen", daemon=True
        )
        self._thread.start()
        return True

    def _loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                payload, _address = sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:
                break
            client = parse(payload)
            if client is None:
                continue
            with self._lock:
                previous = self._clients.get(client["id"])
                changed = (
                    previous is None
                    or previous.get("ip") != client["ip"]
                    or previous.get("name") != client["name"]
                )
                self._clients[client["id"]] = client
            now = time.time()
            if changed or now - self._last_event >= 1.0:
                self._last_event = now
                self.events.put(("discovery_update", ""))

    def clients(self) -> list[dict]:
        now = time.time()
        with self._lock:
            self._clients = {
                key: value
                for key, value in self._clients.items()
                if now - value["seen"] <= EXPIRE_AFTER
            }
            return [dict(value) for value in self._clients.values()]

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
