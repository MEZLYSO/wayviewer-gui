#!/usr/bin/env python3
"""Panel tkinter para transmitir con wayviewer/relay SRT y generar el
comando de conexión para el cliente.

Sección «Transmitir imagen»:
  arranca wayviewer + python/wayviewer_relay.py.

Sección «Conectar»:
  genera el comando ffplay para el equipo cliente, permitiendo elegir IP,
  puerto y varios parámetros. Incluye un chat TCP bidireccional y envío de
  archivos; lo recibido se guarda en ~/wayviewer.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

try:
    import tkinter as tk
    from tkinter import messagebox
except ModuleNotFoundError:  # pragma: no cover - depende del sistema
    sys.stderr.write(
        "Falta el módulo tkinter.\n"
        "En Void Linux instálalo con:\n"
        "  sudo xbps-install -S python3-tkinter\n"
        "Después ejecuta el panel con el Python del sistema:\n"
        "  python3 python/wayviewer_gui.py\n"
    )
    raise SystemExit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY_SCRIPT = os.path.join(ROOT, "python", "wayviewer_relay.py")
try:
    CHAT_PORT = int(os.environ.get("WAYVIEWER_CHAT_PORT", "9500"))
except ValueError:
    CHAT_PORT = 9500

FILE_DIR = os.path.join(os.path.expanduser("~"), "wayviewer")
CHAT_CHUNK = 24 * 1024
MAX_FILE_SIZE = 8 * 1024**3
FILE_MAGIC = "\x01WV"

# Paleta oscura.
BG = "#0f1115"
HEADER = "#0b0e13"
PANEL = "#161a21"
CHIP = "#1f2630"
BORDER = "#30363d"
FG = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#2f81f7"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"

MONO = ("DejaVu Sans Mono", 10)
MONO_SMALL = ("DejaVu Sans Mono", 9)
SANS_BOLD = ("DejaVu Sans", 13, "bold")
CARD_TITLE = ("DejaVu Sans", 11, "bold")


def default_socket_path() -> str:
    """Mismo criterio que wayviewer_relay.py."""
    env = os.environ.get("WAYVIEWER_UNIX_SOCKET")
    if env:
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "wayviewer.sock")
    return "/tmp/wayviewer-%d.sock" % os.getuid()


def find_wayviewer() -> str | None:
    """Busca el binario, aceptando build/wayviewer y ./wayviewer."""
    env = os.environ.get("WAYVIEWER_BIN")
    if env:
        return env if os.access(env, os.X_OK) else None
    for rel in ("build/wayviewer", "wayviewer", "bin/wayviewer"):
        candidate = os.path.join(ROOT, rel)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("wayviewer")


def run_ip(*args: str) -> str:
    try:
        result = subprocess.run(
            ["ip", *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def default_interface() -> str:
    for line in run_ip("route", "show", "default").splitlines():
        parts = line.split()
        if "dev" in parts:
            index = parts.index("dev")
            if index + 1 < len(parts):
                return parts[index + 1]
    return ""


def list_interfaces() -> list[tuple[str, str]]:
    """Devuelve [(interfaz, IPv4)] sin loopback ni direcciones link-local."""
    interfaces: list[tuple[str, str]] = []
    seen: set[str] = set()

    for line in run_ip("-o", "-4", "addr", "show").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        name = parts[1].rstrip(":")
        if name == "lo":
            continue
        ip_text = parts[3].split("/", 1)[0]
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or ip_text in seen:
            continue
        seen.add(ip_text)
        interfaces.append((name, ip_text))

    if not interfaces:
        # Alternativa sin el comando ip.
        try:
            infos = socket.getaddrinfo(
                socket.gethostname(), None, family=socket.AF_INET
            )
        except OSError:
            infos = []
        for info in infos:
            ip_text = info[4][0]
            try:
                address = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if address.is_loopback or address.is_link_local or ip_text in seen:
                continue
            seen.add(ip_text)
            interfaces.append(("red", ip_text))

    default_name = default_interface()

    def score(item: tuple[str, str]) -> tuple[int, str]:
        name, _ = item
        if name == default_name:
            return (0, name)
        if name.startswith(("wlan", "wl", "en", "eth")):
            return (1, name)
        if name.startswith(("tun", "tap", "wg", "tailscale")):
            return (3, name)
        return (2, name)

    interfaces.sort(key=score)
    return interfaces


class Cancelled(Exception):
    """El usuario pidió detener mientras se iniciaba."""


class GtkUnavailable(Exception):
    """El selector de archivos de GTK no está disponible."""


def ensure_file_dir() -> str:
    os.makedirs(FILE_DIR, exist_ok=True)
    return FILE_DIR


def format_size(size: int | str) -> str:
    try:
        value = float(max(0, int(size)))
    except (TypeError, ValueError):
        return str(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = "".join(char for char in name if char.isprintable())
    if name in ("", ".", ".."):
        return "archivo"
    return name[:180]


def unique_path(directory: str, name: str) -> str:
    base, extension = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}-{index}{extension}")
        index += 1
    return candidate


GTK_PICKER_SCRIPT = r"""
import os
import sys

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except Exception:
    sys.exit(11)

title = sys.argv[1] if len(sys.argv) > 1 else "Seleccionar archivo"
initial_dir = sys.argv[2] if len(sys.argv) > 2 else ""

try:
    initialized, _argv = Gtk.init_check([])
except Exception:
    initialized = False
if not initialized:
    sys.exit(11)

dialog = Gtk.FileChooserDialog(
    title=title, action=Gtk.FileChooserAction.OPEN
)
dialog.add_buttons(
    "Cancelar",
    Gtk.ResponseType.CANCEL,
    "Enviar",
    Gtk.ResponseType.OK,
)
dialog.set_modal(True)
dialog.set_default_response(Gtk.ResponseType.OK)
if initial_dir and os.path.isdir(initial_dir):
    try:
        dialog.set_current_folder(initial_dir)
    except Exception:
        pass

response = dialog.run()
path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
dialog.destroy()

if path:
    sys.stdout.write(path)
    sys.exit(0)
sys.exit(10)
"""


def pick_file_with_gtk(
    initial_dir: str | None = None,
    register=None,
) -> str | None:
    """Abre el selector de GTK en un proceso aparte para no bloquear tkinter."""
    interpreter = sys.executable or shutil.which("python3") or "python3"
    command = [
        interpreter,
        "-c",
        GTK_PICKER_SCRIPT,
        "Seleccionar archivo para enviar",
        initial_dir or "",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise GtkUnavailable(str(exc)) from exc

    if callable(register):
        register(process)
    try:
        stdout, stderr = process.communicate()
    finally:
        if callable(register):
            register(None)

    if process.returncode == 0:
        path = (stdout or "").strip()
        return path or None
    if process.returncode == 10:
        return None
    detail = (stderr or "").strip().splitlines()
    raise GtkUnavailable(detail[-1] if detail else "GTK no disponible")


class TcpChat:
    """Chat TCP mínimo: escucha en un puerto y permite una conexión a la vez."""

    def __init__(self, port: int, events: queue.Queue[tuple[str, str]]):
        self.port = port
        self.events = events
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.sock: socket.socket | None = None
        self.server: socket.socket | None = None
        self.listener_thread: threading.Thread | None = None
        self.reader_thread: threading.Thread | None = None
        self.connecting = False
        self.incoming: dict[str, dict] = {}
        self.outgoing_cancel: set[str] = set()
        self.sending = False
        self.send_threads: list[threading.Thread] = []
        self._start_server()

    @property
    def connected(self) -> bool:
        with self.lock:
            return self.sock is not None

    def _start_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.port))
            server.listen(4)
            server.settimeout(0.5)
        except OSError as exc:
            server.close()
            self.events.put(
                (
                    "chat_system",
                    f"Servidor de chat no disponible en el puerto {self.port}: "
                    f"{exc}. Aun así puedes conectar a otro equipo.",
                )
            )
            return
        self.server = server
        self.listener_thread = threading.Thread(
            target=self._accept_loop, name="chat-server", daemon=True
        )
        self.listener_thread.start()

    def _accept_loop(self) -> None:
        server = self.server
        if server is None:
            return
        while not self.stop_event.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            peer = f"{address[0]}:{address[1]}"
            if not self._adopt(conn, peer):
                try:
                    conn.sendall("Servidor de chat ocupado.\n".encode("utf-8"))
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass

    def _adopt(self, conn: socket.socket, peer: str) -> bool:
        with self.lock:
            if self.stop_event.is_set() or self.sock is not None or self.connecting:
                return False
            self.sock = conn
        self.events.put(("chat_connected", f"Conexión entrante de {peer}"))
        self._start_reader(conn)
        return True

    def connect(self, host: str) -> bool:
        with self.lock:
            if self.stop_event.is_set() or self.sock is not None or self.connecting:
                return False
            self.connecting = True
        threading.Thread(
            target=self._connect_worker,
            args=(host,),
            name="chat-connect",
            daemon=True,
        ).start()
        return True

    def _connect_worker(self, host: str) -> None:
        try:
            conn = socket.create_connection((host, self.port), timeout=5.0)
            conn.settimeout(None)
        except OSError as exc:
            with self.lock:
                self.connecting = False
            self.events.put(
                (
                    "chat_error",
                    f"No se pudo conectar el chat a {host}:{self.port}: {exc}",
                )
            )
            return

        with self.lock:
            self.connecting = False
            if self.stop_event.is_set() or self.sock is not None:
                try:
                    conn.close()
                except OSError:
                    pass
                self.events.put(
                    ("chat_system", "Ya hay una conexión de chat activa.")
                )
                return
            self.sock = conn
        self.events.put(("chat_connected", f"Chat conectado a {host}:{self.port}"))
        self._start_reader(conn)

    def _start_reader(self, conn: socket.socket) -> None:
        self.reader_thread = threading.Thread(
            target=self._read_loop,
            args=(conn,),
            name="chat-reader",
            daemon=True,
        )
        self.reader_thread.start()

    def _read_loop(self, conn: socket.socket) -> None:
        try:
            reader = conn.makefile(
                "r", encoding="utf-8", errors="replace", newline="\n"
            )
        except OSError:
            self._drop(conn, "No se pudo leer el chat.")
            return

        reason = "El otro equipo cerró el chat."
        try:
            for line in reader:
                if self.stop_event.is_set():
                    return
                text = line.rstrip("\r\n")
                if not text:
                    continue
                if text.startswith("\x01\x01"):
                    self.events.put(("chat_incoming", text[1:][:4000]))
                elif text.startswith(FILE_MAGIC):
                    self._handle_file_line(text[len(FILE_MAGIC):])
                else:
                    self.events.put(("chat_incoming", text[:4000]))
        except OSError:
            reason = "Conexión de chat interrumpida."
        finally:
            try:
                reader.close()
            except OSError:
                pass
            self._abort_all_incoming()

        if self.stop_event.is_set():
            self._clear(conn)
        else:
            self._drop(conn, reason)

    def _send_line(self, payload: str) -> bool:
        with self.write_lock:
            with self.lock:
                conn = self.sock
            if conn is None:
                return False
            try:
                conn.sendall(payload.encode("utf-8"))
            except OSError as exc:
                self._drop(conn, f"Error al enviar: {exc}")
                return False
        return True

    def send(self, text: str) -> bool:
        if text.startswith("\x01"):
            text = "\x01" + text
        return self._send_line(text + "\n")

    # ------------------------------------------------------ archivos
    def send_file(self, path: str) -> bool:
        with self.lock:
            if self.sock is None or self.sending:
                return False
            self.sending = True
        thread = threading.Thread(
            target=self._send_file_worker,
            args=(path,),
            name="chat-send-file",
            daemon=True,
        )
        self.send_threads.append(thread)
        thread.start()
        return True

    def _send_file_worker(self, path: str) -> None:
        name = os.path.basename(path) or "archivo"
        transfer_id = ""
        try:
            size = os.path.getsize(path)
            if size > MAX_FILE_SIZE:
                raise OSError(
                    f"el archivo supera el límite de {format_size(MAX_FILE_SIZE)}"
                )
            transfer_id = uuid.uuid4().hex[:12]
            encoded_name = base64.b64encode(name.encode("utf-8")).decode("ascii")
            total = (size + CHAT_CHUNK - 1) // CHAT_CHUNK
            self.events.put(("file_send_start", f"{name}\t{size}"))
            if not self._send_line(
                f"{FILE_MAGIC}F\t{transfer_id}\t{total}\t{size}\t{encoded_name}\n"
            ):
                raise OSError("chat desconectado")

            sent = 0
            sequence = 0
            last_emit = 0.0
            with open(path, "rb") as handle:
                while True:
                    if transfer_id in self.outgoing_cancel:
                        raise OSError("el receptor canceló la transferencia")
                    chunk = handle.read(CHAT_CHUNK)
                    if not chunk:
                        break
                    encoded = base64.b64encode(chunk).decode("ascii")
                    if not self._send_line(
                        f"{FILE_MAGIC}D\t{transfer_id}\t{sequence}\t{encoded}\n"
                    ):
                        raise OSError("chat desconectado")
                    sent += len(chunk)
                    sequence += 1
                    now = time.time()
                    if now - last_emit >= 0.4 or sent >= size:
                        last_emit = now
                        percent = int(sent * 100 / size) if size else 100
                        self.events.put(
                            ("file_send_progress", f"{name}\t{percent}")
                        )

            if not self._send_line(f"{FILE_MAGIC}E\t{transfer_id}\n"):
                raise OSError("chat desconectado")
            self.events.put(("file_send_done", name))
        except (OSError, ValueError) as exc:
            self.events.put(("file_send_error", f"{name}: {exc}"))
            if transfer_id:
                self._send_line(f"{FILE_MAGIC}X\t{transfer_id}\n")
        finally:
            self.outgoing_cancel.discard(transfer_id)
            with self.lock:
                self.sending = False

    def _handle_file_line(self, payload: str) -> None:
        parts = payload.split("\t")
        kind = parts[0]
        if kind == "F" and len(parts) >= 5:
            self._begin_incoming(parts[1], parts[2], parts[3], parts[4])
        elif kind == "D" and len(parts) >= 4:
            self._chunk_incoming(parts[1], parts[2], parts[3])
        elif kind == "E" and len(parts) >= 2:
            self._finish_incoming(parts[1])
        elif kind == "X" and len(parts) >= 2:
            self.outgoing_cancel.add(parts[1])

    def _begin_incoming(
        self, transfer_id: str, total_text: str, size_text: str, name_b64: str
    ) -> None:
        try:
            total = int(total_text)
            size = int(size_text)
        except ValueError:
            return
        if not 0 <= size <= MAX_FILE_SIZE:
            self._send_line(f"{FILE_MAGIC}X\t{transfer_id}\n")
            return
        expected_total = (size + CHAT_CHUNK - 1) // CHAT_CHUNK
        if total != expected_total:
            self._send_line(f"{FILE_MAGIC}X\t{transfer_id}\n")
            return
        try:
            raw_name = base64.b64decode(name_b64.encode("ascii"), validate=True)
            name = raw_name.decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            name = "archivo"
        name = safe_filename(name)

        directory = ensure_file_dir()
        part_path = os.path.join(directory, f".part-{transfer_id}")
        try:
            handle = open(part_path, "wb")
        except OSError as exc:
            self.events.put(("file_recv_error", f"{name}: {exc}"))
            self._send_line(f"{FILE_MAGIC}X\t{transfer_id}\n")
            return

        self.incoming[transfer_id] = {
            "name": name,
            "directory": directory,
            "size": size,
            "total": total,
            "received": 0,
            "next": 0,
            "part": part_path,
            "handle": handle,
            "last": 0.0,
        }
        self.events.put(("file_recv_start", f"{name}\t{size}"))

    def _chunk_incoming(
        self, transfer_id: str, sequence_text: str, chunk_b64: str
    ) -> None:
        state = self.incoming.get(transfer_id)
        if state is None:
            return
        try:
            sequence = int(sequence_text)
            data = base64.b64decode(chunk_b64.encode("ascii"), validate=True)
        except (ValueError, binascii.Error):
            self._abort_incoming(transfer_id, "datos inválidos")
            return
        if (
            sequence != state["next"]
            or state["received"] + len(data) > state["size"]
        ):
            self._abort_incoming(transfer_id, "transferencia corrupta")
            return
        try:
            state["handle"].write(data)
        except OSError as exc:
            self._abort_incoming(transfer_id, str(exc))
            return
        state["received"] += len(data)
        state["next"] += 1
        now = time.time()
        if now - state["last"] >= 0.4 or state["received"] >= state["size"]:
            state["last"] = now
            percent = (
                int(state["received"] * 100 / state["size"])
                if state["size"]
                else 100
            )
            self.events.put(
                ("file_recv_progress", f"{state['name']}\t{percent}")
            )

    def _finish_incoming(self, transfer_id: str) -> None:
        state = self.incoming.pop(transfer_id, None)
        if state is None:
            return
        try:
            state["handle"].close()
            if (
                state["received"] != state["size"]
                or state["next"] != state["total"]
            ):
                raise OSError("transferencia incompleta")
            target = unique_path(state["directory"], state["name"])
            os.replace(state["part"], target)
        except OSError as exc:
            try:
                os.remove(state["part"])
            except OSError:
                pass
            self.events.put(("file_recv_error", f"{state['name']}: {exc}"))
            self._send_line(f"{FILE_MAGIC}X\t{transfer_id}\n")
            return
        self.events.put(("file_recv_done", target))

    def _abort_incoming(self, transfer_id: str, reason: str) -> None:
        state = self.incoming.pop(transfer_id, None)
        if state is None:
            return
        try:
            state["handle"].close()
        except OSError:
            pass
        try:
            os.remove(state["part"])
        except OSError:
            pass
        self.events.put(("file_recv_error", f"{state['name']}: {reason}"))
        self._send_line(f"{FILE_MAGIC}X\t{transfer_id}\n")

    def _abort_all_incoming(self) -> None:
        for transfer_id in list(self.incoming):
            state = self.incoming.pop(transfer_id, None)
            if state is None:
                continue
            try:
                state["handle"].close()
            except OSError:
                pass
            try:
                os.remove(state["part"])
            except OSError:
                pass

    def _clear(self, conn: socket.socket) -> bool:
        with self.lock:
            if self.sock is not conn:
                return False
            self.sock = None
        try:
            conn.close()
        except OSError:
            pass
        return True

    def _drop(self, conn: socket.socket, reason: str) -> None:
        if self._clear(conn):
            self.events.put(("chat_disconnected", reason))

    def disconnect(self) -> None:
        with self.lock:
            conn = self.sock
            self.sock = None
            self.connecting = False
        if conn is None:
            return
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
        self.events.put(("chat_disconnected", "Chat desconectado."))

    def stop(self) -> None:
        self.stop_event.set()
        self.disconnect()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        threads = [self.listener_thread, self.reader_thread, *self.send_threads]
        for thread in threads:
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.5)
        self._abort_all_incoming()


class WayViewerPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = "stopped"
        self.proc_wv: subprocess.Popen | None = None
        self.proc_relay: subprocess.Popen | None = None
        self.ffplay_proc: subprocess.Popen | None = None
        self.ffplay_stopping = False
        self.socket_owned = False
        self.stop_requested = threading.Event()
        self.term_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.last_output: dict[str, deque[str]] = {
            "wayviewer": deque(maxlen=8),
            "relay": deque(maxlen=8),
            "ffplay": deque(maxlen=12),
        }
        self.event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.chat_events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.chat_connected = False
        self.chat_connecting = False
        self.picking_file = False
        self.picker_proc: subprocess.Popen | None = None
        self.closing = False
        self.socket_path = default_socket_path()
        self.interfaces: list[tuple[str, str]] = []
        self.chip_signature: tuple[tuple[str, str], ...] = ()
        self.primary_ip = ""
        self.srt_port = 9000
        self.notify_after: str | None = None

        self.var_host = tk.StringVar(value="0.0.0.0")
        self.var_port = tk.StringVar(value="12401")
        self.var_srt_port = tk.StringVar(value="9000")
        self.var_latency = tk.StringVar(value="120")
        self.var_maxbw = tk.StringVar(value="8000")

        self.var_client_ip = tk.StringVar(value="")
        self.var_client_latency = tk.StringVar(value="200")
        self.var_probesize = tk.StringVar(value="65536")
        self.var_analyzeduration = tk.StringVar(value="100000")
        self.var_nobuffer = tk.BooleanVar(value=True)
        self.var_low_delay = tk.BooleanVar(value=True)
        self.var_framedrop = tk.BooleanVar(value=True)
        self.var_tlpktdrop = tk.BooleanVar(value=True)
        self.var_chat_message = tk.StringVar(value="")

        self._configure_window()
        self._build_header()
        self._build_body()
        self.chat = TcpChat(CHAT_PORT, self.chat_events)
        self._apply_chat_state(False)
        self._refresh_interfaces(force=True)
        self._apply_state("stopped")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _event: self._refresh_interfaces(force=True))
        self.root.after(100, self._poll)
        self.root.after(8000, self._auto_refresh_interfaces)

    # ------------------------------------------------------------------ UI
    def _configure_window(self) -> None:
        self.root.title("WayViewer · Panel de transmisión")
        self.root.configure(bg=BG)
        self.root.minsize(980, 620)
        width, height = 1180, 820
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=HEADER)
        header.pack(fill="x")
        tk.Frame(header, bg=ACCENT, height=3).pack(fill="x")

        inner = tk.Frame(header, bg=HEADER)
        inner.pack(fill="x", padx=20, pady=(12, 12))

        top = tk.Frame(inner, bg=HEADER)
        top.pack(fill="x")

        brand = tk.Frame(top, bg=HEADER)
        brand.pack(side="left")
        mark = tk.Frame(brand, bg=ACCENT, width=4, height=40)
        mark.pack(side="left", padx=(0, 12))
        mark.pack_propagate(False)
        words = tk.Frame(brand, bg=HEADER)
        words.pack(side="left")
        tk.Label(
            words, text="WayViewer", bg=HEADER, fg=FG,
            font=("DejaVu Sans", 17, "bold"),
        ).pack(anchor="w")
        self.header_subtitle = tk.Label(
            words,
            text="Panel de transmisión, chat y archivos",
            bg=HEADER,
            fg=MUTED,
            font=("DejaVu Sans", 9),
        )
        self.header_subtitle.pack(anchor="w")

        self.refresh_btn = tk.Button(
            top,
            text="↻  Actualizar IPs",
            command=lambda: self._refresh_interfaces(force=True),
            bg=CHIP,
            fg=FG,
            activebackground=BORDER,
            activeforeground=FG,
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            font=("DejaVu Sans", 9, "bold"),
        )
        self.refresh_btn.pack(side="right")

        row = tk.Frame(inner, bg=HEADER)
        row.pack(fill="x", pady=(14, 0))
        self.interfaces_label = tk.Label(
            row, text="RED LOCAL", bg=HEADER, fg=MUTED,
            font=("DejaVu Sans", 8, "bold"),
        )
        self.interfaces_label.pack(side="left", padx=(0, 14))
        self.chips = tk.Frame(row, bg=HEADER)
        self.chips.pack(side="left", fill="x", expand=True)

    def _make_card(
        self, parent: tk.Widget, eyebrow: str, title: str, accent: str
    ) -> tuple[tk.Frame, tk.Frame]:
        card = tk.Frame(
            parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER
        )
        tk.Frame(card, bg=accent, height=3).pack(fill="x")
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        tk.Label(
            inner, text=eyebrow, bg=PANEL, fg=MUTED,
            font=("DejaVu Sans", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            inner, text=title, bg=PANEL, fg=FG,
            font=("DejaVu Sans", 14, "bold"),
        ).pack(anchor="w", pady=(2, 0))
        return card, inner

    def _field_cell(
        self, parent: tk.Widget, column: int, label: str, row: int = 0
    ) -> tk.Frame:
        cell = tk.Frame(parent, bg=PANEL)
        cell.grid(row=row, column=column, sticky="w", padx=(0, 18), pady=(0, 10))
        tk.Label(
            cell, text=label, bg=PANEL, fg=MUTED,
            font=("DejaVu Sans", 8, "bold"),
        ).pack(anchor="w")
        return cell

    def _dark_entry(
        self, parent: tk.Widget, variable: tk.StringVar, width: int
    ) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            bg="#0b0e13",
            fg=FG,
            insertbackground=FG,
            disabledbackground="#0b0e13",
            disabledforeground=MUTED,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=MONO,
        )

    def _sync_scroll_region(self, _event: object = None) -> None:
        if not hasattr(self, "body_canvas"):
            return
        region = self.body_canvas.bbox("all")
        if region is not None:
            self.body_canvas.configure(scrollregion=region)

    def _on_body_wheel(self, event: tk.Event) -> None:
        if not hasattr(self, "body_canvas"):
            return
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if getattr(self, "chat_log", None) is widget:
                return
            widget = getattr(widget, "master", None)
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if getattr(event, "delta", 0) > 0 else 1
        self.body_canvas.yview_scroll(step, "units")

    # ------------------------------------------------------ disposición
    def _on_body_resize(self, event: tk.Event) -> None:
        content_width = min(event.width, 1240)
        self.body_canvas.itemconfigure(self.body_window, width=content_width)
        self.body_canvas.coords(
            self.body_window, max(0, (event.width - content_width) // 2), 0
        )
        self._apply_responsive_layout(content_width)

    def _bind_wrap(self, label: tk.Label, minimum: int = 120) -> None:
        def _update(event: tk.Event) -> None:
            label.configure(wraplength=max(minimum, event.width - 2))

        label.bind("<Configure>", _update)

    def _apply_responsive_layout(self, width: int) -> None:
        if not hasattr(self, "tx_card"):
            return
        if width >= 900:
            mode = "wide"
        elif width >= 620:
            mode = "medium"
        else:
            mode = "narrow"
        if mode == self.layout_mode:
            return
        self.layout_mode = mode
        self._arrange_cards()
        self._arrange_client_fields()
        self._arrange_footer()
        self._arrange_file_row()
        self._arrange_header()
        self._arrange_chat_head()
        self._arrange_chips()

        self.status_detail.configure(
            wraplength=240 if mode == "narrow" else 300
        )
        self.client_ip_entry.configure(
            width=13 if mode == "narrow" else 18
        )
        self.root.after_idle(self._sync_scroll_region)

    def _arrange_cards(self) -> None:
        columns = self.columns_frame
        self.tx_card.grid_forget()
        self.cx_card.grid_forget()
        if self.layout_mode == "wide":
            columns.grid_columnconfigure(0, weight=0, minsize=362)
            columns.grid_columnconfigure(1, weight=1)
            columns.grid_rowconfigure(0, weight=1)
            columns.grid_rowconfigure(1, weight=0)
            self.tx_card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
            self.cx_card.grid(row=0, column=1, sticky="nsew")
        else:
            columns.grid_columnconfigure(0, weight=1, minsize=0)
            columns.grid_columnconfigure(1, weight=0, minsize=0)
            columns.grid_rowconfigure(0, weight=0)
            columns.grid_rowconfigure(1, weight=0)
            self.tx_card.grid(
                row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14)
            )
            self.cx_card.grid(row=1, column=0, columnspan=2, sticky="ew")

    def _arrange_client_fields(self) -> None:
        if self.layout_mode == "narrow":
            positions = ((0, 0), (1, 0), (1, 1))
        else:
            positions = ((0, 0), (0, 1), (0, 2))
        cells = (
            self.client_ip_cell,
            self.client_port_cell,
            self.client_latency_cell,
        )
        for cell, (row, column) in zip(cells, positions):
            cell.grid_configure(row=row, column=column)

    def _arrange_footer(self) -> None:
        footer = self.footer
        self.connect_btn.grid_forget()
        self.client_status.grid_forget()
        footer.grid_columnconfigure(1, weight=1)
        if self.layout_mode == "narrow":
            self.connect_btn.grid(row=0, column=0, sticky="w")
            self.client_status.grid(
                row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0)
            )
        else:
            self.connect_btn.grid(row=0, column=0, sticky="w")
            self.client_status.grid(
                row=0, column=1, sticky="ew", padx=(14, 0)
            )

    def _arrange_file_row(self) -> None:
        row = self.file_row
        self.file_btn.grid_forget()
        self.file_status.grid_forget()
        row.grid_columnconfigure(1, weight=1)
        if self.layout_mode == "narrow":
            self.file_btn.grid(row=0, column=0, sticky="w")
            self.file_status.grid(
                row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )
        else:
            self.file_btn.grid(row=0, column=0, sticky="w")
            self.file_status.grid(
                row=0, column=1, sticky="ew", padx=(12, 0)
            )

    def _arrange_header(self) -> None:
        narrow = self.layout_mode == "narrow"
        self.header_subtitle.configure(
            text=(
                "Transmisión · chat y archivos"
                if narrow
                else "Panel de transmisión, chat y archivos"
            )
        )
        self.refresh_btn.configure(
            text="↻ IPs" if narrow else "↻  Actualizar IPs"
        )
        self.interfaces_label.configure(
            text="RED" if narrow else "RED LOCAL"
        )

    def _arrange_chat_head(self) -> None:
        self.chat_btn.pack_forget()
        self.chat_titles.pack_forget()
        if self.layout_mode == "narrow":
            self.chat_titles.pack(anchor="w")
            self.chat_btn.pack(anchor="w", pady=(6, 0))
        else:
            self.chat_titles.pack(side="left")
            self.chat_btn.pack(side="right")

    def _arrange_chips(self) -> None:
        chips = getattr(self, "chip_widgets", None)
        if not chips:
            return
        per_row = 2 if self.layout_mode == "narrow" else len(chips)
        for index, widget in enumerate(chips):
            widget.grid_configure(
                row=index // per_row,
                column=index % per_row,
                padx=(0, 8),
                pady=(0, 6) if self.layout_mode == "narrow" else (0, 0),
            )

    def _build_body(self) -> None:
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True)

        self.body_canvas = tk.Canvas(
            container, bg=BG, highlightthickness=0, bd=0
        )
        self.body_scroll = tk.Scrollbar(
            container,
            orient="vertical",
            command=self.body_canvas.yview,
            bg=PANEL,
            troughcolor=BG,
            activebackground=BORDER,
            relief="flat",
            width=12,
        )
        self.body_canvas.configure(yscrollcommand=self.body_scroll.set)
        self.body_scroll.pack(side="right", fill="y")
        self.body_canvas.pack(side="left", fill="both", expand=True)

        holder = tk.Frame(self.body_canvas, bg=BG)
        self.body_window = self.body_canvas.create_window(
            (0, 0), window=holder, anchor="nw"
        )
        holder.bind("<Configure>", self._sync_scroll_region)
        self.body_canvas.bind("<Configure>", self._on_body_resize)
        self.root.bind_all("<MouseWheel>", self._on_body_wheel)
        self.root.bind_all("<Button-4>", self._on_body_wheel)
        self.root.bind_all("<Button-5>", self._on_body_wheel)

        body = tk.Frame(holder, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=14)

        self.layout_mode = ""
        columns = tk.Frame(body, bg=BG)
        self.columns_frame = columns
        columns.pack(fill="both", expand=True)
        columns.grid_columnconfigure(0, weight=0, minsize=362)
        columns.grid_columnconfigure(1, weight=1)
        columns.grid_rowconfigure(0, weight=1)

        # --- Columna izquierda: Transmitir imagen -------------------------
        tx_card, tx = self._make_card(
            columns, "EMISIÓN", "Transmitir imagen", GREEN
        )
        self.tx_card = tx_card
        tx_card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        grid = tk.Frame(tx, bg=PANEL)
        grid.pack(fill="x", pady=(10, 0))
        fields = [
            ("Puerto wayviewer", self.var_port, 9, 0, 0),
            ("Puerto SRT", self.var_srt_port, 9, 0, 1),
            ("Latencia (ms)", self.var_latency, 9, 1, 0),
        ]
        for label, variable, width, row, column in fields:
            cell = self._field_cell(grid, column, label, row=row)
            self._dark_entry(cell, variable, width).pack(
                anchor="w", pady=(4, 0), ipady=3
            )

        self.action_btn = tk.Button(
            tx,
            text="Transmitir imagen",
            command=self._on_action,
            bg=GREEN,
            fg="#04150a",
            activebackground="#2ea043",
            activeforeground="#04150a",
            disabledforeground="#6e7681",
            relief="flat",
            padx=20,
            pady=10,
            cursor="hand2",
            font=SANS_BOLD,
        )
        self.action_btn.pack(fill="x", pady=(14, 0))

        status = tk.Frame(
            tx, bg=CHIP, highlightthickness=1, highlightbackground=BORDER
        )
        status.pack(fill="x", pady=(10, 0))
        self.status_dot = tk.Label(
            status, text="●", bg=CHIP, fg=MUTED, font=("DejaVu Sans", 15)
        )
        self.status_dot.pack(side="left", padx=(10, 0), pady=8)
        status_text = tk.Frame(status, bg=CHIP)
        status_text.pack(side="left", padx=(6, 10), pady=8, fill="x", expand=True)
        self.status_title = tk.Label(
            status_text, text="", bg=CHIP, fg=FG,
            font=("DejaVu Sans", 10, "bold"), anchor="w",
        )
        self.status_title.pack(fill="x")
        self.status_detail = tk.Label(
            status_text, text="", bg=CHIP, fg=MUTED,
            font=("DejaVu Sans", 9), anchor="w", justify="left",
            wraplength=300,
        )
        self.status_detail.pack(fill="x")

        # --- Columna derecha: Conectar ------------------------------------
        cx_card, cx = self._make_card(columns, "RECEPCIÓN", "Conectar", ACCENT)
        self.cx_card = cx_card
        cx_card.grid(row=0, column=1, sticky="nsew")

        client_grid = tk.Frame(cx, bg=PANEL)
        self.client_grid = client_grid
        client_grid.pack(fill="x", pady=(10, 0))

        ip_cell = self._field_cell(client_grid, 0, "IP de destino")
        self.client_ip_cell = ip_cell
        self.client_ip_entry = self._dark_entry(
            ip_cell, self.var_client_ip, 18
        )
        self.client_ip_entry.pack(anchor="w", pady=(4, 0), ipady=3)

        port_cell = self._field_cell(client_grid, 1, "Puerto SRT")
        self.client_port_cell = port_cell
        self._dark_entry(port_cell, self.var_srt_port, 9).pack(
            anchor="w", pady=(4, 0), ipady=3
        )

        latency_cell = self._field_cell(client_grid, 2, "Latencia (ms)")
        self.client_latency_cell = latency_cell
        self._dark_entry(latency_cell, self.var_client_latency, 9).pack(
            anchor="w", pady=(4, 0), ipady=3
        )

        self.option_buttons = []

        footer = tk.Frame(cx, bg=PANEL)
        self.footer = footer
        footer.pack(fill="x", pady=(10, 0))
        footer.grid_columnconfigure(1, weight=1)
        self.connect_btn = tk.Button(
            footer,
            text="Conectar a transmisión",
            command=self._toggle_connection,
            bg=ACCENT,
            fg="#ffffff",
            activebackground="#1f6feb",
            activeforeground="#ffffff",
            disabledforeground="#6e7681",
            relief="flat",
            padx=20,
            pady=8,
            cursor="hand2",
            font=("DejaVu Sans", 11, "bold"),
        )
        self.connect_btn.grid(row=0, column=0, sticky="w")

        self.client_status = tk.Label(
            footer,
            text="Escribe la IP de destino y pulsa el botón.",
            bg=PANEL,
            fg=MUTED,
            font=("DejaVu Sans", 9),
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self.client_status.grid(row=0, column=1, sticky="ew", padx=(14, 0))
        self._bind_wrap(self.client_status, 110)

        # --- Chat TCP -----------------------------------------------------
        tk.Frame(cx, bg=BORDER, height=1).pack(fill="x", pady=(14, 0))
        chat_head = tk.Frame(cx, bg=PANEL)
        self.chat_head = chat_head
        chat_head.pack(fill="x", pady=(10, 0))
        chat_titles = tk.Frame(chat_head, bg=PANEL)
        self.chat_titles = chat_titles
        chat_titles.pack(side="left")
        tk.Label(
            chat_titles, text="CHAT Y ARCHIVOS", bg=PANEL, fg=MUTED,
            font=("DejaVu Sans", 8, "bold"),
        ).pack(anchor="w")
        self.chat_btn = tk.Button(
            chat_head,
            text="Conectar chat",
            command=self._toggle_chat,
            bg=ACCENT,
            fg="#ffffff",
            activebackground="#1f6feb",
            activeforeground="#ffffff",
            disabledforeground="#6e7681",
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            font=("DejaVu Sans", 9, "bold"),
        )
        self.chat_btn.pack(side="right")

        chat_box = tk.Frame(
            cx, bg="#0b0e13", highlightthickness=1, highlightbackground=BORDER
        )
        chat_box.pack(fill="both", expand=True, pady=(8, 0))
        chat_scroll = tk.Scrollbar(
            chat_box,
            bg=PANEL,
            troughcolor="#0b0e13",
            activebackground=BORDER,
            relief="flat",
            width=12,
        )
        chat_scroll.pack(side="right", fill="y")
        self.chat_log = tk.Text(
            chat_box,
            bg="#0b0e13",
            fg="#c9d1d9",
            insertbackground=FG,
            selectbackground=ACCENT,
            relief="flat",
            wrap="word",
            height=7,
            font=MONO_SMALL,
            padx=10,
            pady=8,
            yscrollcommand=chat_scroll.set,
            state="disabled",
        )
        self.chat_log.pack(side="left", fill="both", expand=True)
        chat_scroll.configure(command=self.chat_log.yview)
        self.chat_log.tag_configure("incoming", foreground=FG)
        self.chat_log.tag_configure("outgoing", foreground=ACCENT)
        self.chat_log.tag_configure("system", foreground=MUTED)
        self.chat_log.tag_configure("error", foreground=RED)

        message_row = tk.Frame(cx, bg=PANEL)
        message_row.pack(fill="x", pady=(8, 0))
        self.chat_entry = self._dark_entry(
            message_row, self.var_chat_message, 40
        )
        self.chat_entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.chat_entry.bind("<Return>", self._send_chat)
        self.chat_send_btn = tk.Button(
            message_row,
            text="Enviar",
            command=self._send_chat,
            bg=CHIP,
            fg=FG,
            activebackground=BORDER,
            activeforeground=FG,
            disabledforeground="#6e7681",
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            font=("DejaVu Sans", 9, "bold"),
        )
        self.chat_send_btn.pack(side="left", padx=(8, 0))

        file_row = tk.Frame(cx, bg=PANEL)
        self.file_row = file_row
        file_row.pack(fill="x", pady=(8, 0))
        file_row.grid_columnconfigure(1, weight=1)
        self.file_btn = tk.Button(
            file_row,
            text="Enviar archivo…",
            command=self._pick_and_send_file,
            bg=CHIP,
            fg=FG,
            activebackground=BORDER,
            activeforeground=FG,
            disabledforeground="#6e7681",
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
            font=("DejaVu Sans", 9, "bold"),
        )
        self.file_btn.grid(row=0, column=0, sticky="w")
        self.file_status = tk.Label(
            file_row,
            text=f"Se guardan en {FILE_DIR}",
            bg=PANEL,
            fg=MUTED,
            font=("DejaVu Sans", 9),
            anchor="w",
            justify="left",
            wraplength=380,
        )
        self.file_status.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        self._bind_wrap(self.file_status, 110)

    # ------------------------------------------------------- interfaz de red
    def _refresh_interfaces(self, force: bool = False) -> None:
        interfaces = list_interfaces()
        signature = tuple(interfaces)
        if not force and signature == self.chip_signature:
            return
        self.chip_signature = signature
        self.interfaces = interfaces

        old_primary = self.primary_ip
        default_name = default_interface()
        self.primary_ip = interfaces[0][1] if interfaces else ""

        for child in self.chips.winfo_children():
            child.destroy()

        self.chip_widgets = []
        if not interfaces:
            no_interfaces = tk.Label(
                self.chips,
                text="No se detectaron interfaces IPv4",
                bg=HEADER,
                fg=MUTED,
                font=("DejaVu Sans", 9),
            )
            no_interfaces.grid(row=0, column=0, sticky="w")
            self.chip_widgets.append(no_interfaces)
        else:
            for index, (name, ip_text) in enumerate(interfaces):
                default_mark = " ★" if name == default_name else ""
                chip = tk.Frame(
                    self.chips,
                    bg=CHIP,
                    highlightthickness=1,
                    highlightbackground=BORDER,
                    padx=10,
                    pady=5,
                    cursor="hand2",
                )
                chip.grid(row=0, column=index, sticky="w", padx=(0, 8))
                name_label = tk.Label(
                    chip,
                    text=name + default_mark,
                    bg=CHIP,
                    fg=MUTED,
                    font=("DejaVu Sans", 8),
                    cursor="hand2",
                )
                name_label.pack(anchor="w")
                ip_label = tk.Label(
                    chip,
                    text=ip_text,
                    bg=CHIP,
                    fg=FG,
                    font=MONO,
                    cursor="hand2",
                )
                ip_label.pack(anchor="w")

                for widget in (chip, name_label, ip_label):
                    widget.bind(
                        "<Button-1>",
                        lambda _event, ip=ip_text: self._copy_text(
                            ip, f"IP copiada: {ip}"
                        ),
                    )
                self.chip_widgets.append(chip)

        self._arrange_chips()

        if self.state == "running":
            self._show_running_detail()
        elif old_primary != self.primary_ip and self.state == "stopped":
            self._apply_state("stopped")

    def _auto_refresh_interfaces(self) -> None:
        if self.closing:
            return
        self._refresh_interfaces()
        self.root.after(8000, self._auto_refresh_interfaces)

    # ------------------------------------------------------- comando cliente
    def _validated_client_values(self) -> dict | None:
        ip_text = self.var_client_ip.get().strip()
        if not ip_text:
            self._client_error("La IP de destino no puede estar vacía.")
            return None
        if any(char.isspace() for char in ip_text):
            self._client_error("La IP de destino no puede contener espacios.")
            return None

        try:
            port = int(self.var_srt_port.get())
        except ValueError:
            self._client_error("El puerto SRT debe ser un número entero.")
            return None
        if not 1 <= port <= 65535:
            self._client_error("El puerto SRT debe estar entre 1 y 65535.")
            return None

        try:
            latency = int(self.var_client_latency.get())
        except ValueError:
            self._client_error("La latencia del cliente debe ser un número entero.")
            return None
        if latency < 1:
            self._client_error("La latencia del cliente debe ser mayor que cero.")
            return None

        probesize_text = self.var_probesize.get().strip()
        analyzeduration_text = self.var_analyzeduration.get().strip()
        for label, text in (
            ("probesize", probesize_text),
            ("analyzeduration", analyzeduration_text),
        ):
            if not text:
                continue
            try:
                value = int(text)
            except ValueError:
                self._client_error(f"{label} debe ser un número entero.")
                return None
            if value < 0:
                self._client_error(f"{label} no puede ser negativo.")
                return None

        return {
            "ip": ip_text,
            "port": port,
            "latency": latency,
            "probesize": probesize_text or None,
            "analyzeduration": analyzeduration_text or None,
            "nobuffer": self.var_nobuffer.get(),
            "low_delay": self.var_low_delay.get(),
            "framedrop": self.var_framedrop.get(),
            "tlpktdrop": self.var_tlpktdrop.get(),
        }

    def _client_error(self, message: str) -> None:
        messagebox.showerror(
            "Parámetros del cliente inválidos", message, parent=self.root
        )

    def _build_client_command(self, values: dict) -> list[str]:
        url = (
            f"srt://{values['ip']}:{values['port']}"
            f"?mode=caller&latency={values['latency']}"
        )
        if values["tlpktdrop"]:
            url += "&tlpktdrop=1"

        args = ["ffplay"]
        if values["probesize"] is not None:
            args += ["-probesize", str(values["probesize"])]
        if values["analyzeduration"] is not None:
            args += ["-analyzeduration", str(values["analyzeduration"])]
        if values["nobuffer"]:
            args += ["-fflags", "nobuffer"]
        if values["low_delay"]:
            args += ["-flags", "low_delay"]
        if values["framedrop"]:
            args.append("-framedrop")
        args.append(url)
        return args

    def _toggle_connection(self) -> None:
        if self.ffplay_proc is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        values = self._validated_client_values()
        if values is None:
            return
        args = self._build_client_command(values)
        ffplay = shutil.which("ffplay")
        if not ffplay:
            messagebox.showerror(
                "ffplay no encontrado",
                "No se encontró ffplay en PATH. Instala FFmpeg para reproducir "
                "la transmisión.",
                parent=self.root,
            )
            return
        args[0] = ffplay
        with self.output_lock:
            self.last_output["ffplay"].clear()
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
                cwd=ROOT,
            )
        except OSError as exc:
            messagebox.showerror(
                "No se pudo conectar",
                f"No se pudo ejecutar ffplay:\n{exc}",
                parent=self.root,
            )
            return

        self.ffplay_proc = proc
        self.ffplay_stopping = False
        self._reader_thread("ffplay", proc.stdout)
        self._apply_connect_state(True)
        self._set_client_status(f"Reproduciendo {args[-1]}", GREEN)

    def _disconnect(self) -> None:
        if self.ffplay_proc is None:
            return
        self.ffplay_stopping = True
        self.connect_btn.configure(
            text="Desconectando…",
            bg=AMBER,
            fg="#1f1600",
            state="disabled",
        )
        self._set_client_status("Cerrando ffplay…", AMBER)
        threading.Thread(
            target=self._disconnect_worker,
            args=(self.ffplay_proc,),
            name="ffplay-stop",
            daemon=True,
        ).start()

    def _disconnect_worker(self, proc: subprocess.Popen) -> None:
        self._signal_process(proc, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            self._signal_process(proc, signal.SIGKILL)

    def _apply_connect_state(self, running: bool) -> None:
        if running:
            self.connect_btn.configure(
                text="Desconectar",
                bg=RED,
                fg="#ffffff",
                activebackground="#da3633",
                state="normal",
            )
        else:
            self.connect_btn.configure(
                text="Conectar a transmisión",
                bg=ACCENT,
                fg="#ffffff",
                activebackground="#1f6feb",
                state="normal",
            )

    def _terminate_ffplay(self, grace: float = 1.2) -> None:
        proc = self.ffplay_proc
        self.ffplay_proc = None
        self.ffplay_stopping = False
        if proc is None:
            return
        self._signal_process(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal_process(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    # ----------------------------------------------------------------- chat
    def _toggle_chat(self) -> None:
        if self.chat_connected:
            self.chat.disconnect()
            self.chat_btn.configure(
                text="Desconectando…",
                bg=AMBER,
                fg="#1f1600",
                state="disabled",
            )
            return
        if self.chat_connecting:
            return

        ip_text = self.var_client_ip.get().strip()
        if not ip_text:
            messagebox.showerror(
                "IP de destino",
                "Escribe la IP de destino para conectar el chat.",
                parent=self.root,
            )
            return
        if any(char.isspace() for char in ip_text):
            messagebox.showerror(
                "IP de destino",
                "La IP de destino no puede contener espacios.",
                parent=self.root,
            )
            return

        if not self.chat.connect(ip_text):
            self._chat_append("error", "El chat ya está conectado o conectándose.")
            return

        self.chat_connecting = True
        self.client_ip_entry.configure(state="disabled")
        self.chat_btn.configure(
            text="Conectando…",
            bg=AMBER,
            fg="#1f1600",
            state="disabled",
        )
        self.chat_entry.configure(state="disabled")
        self.chat_send_btn.configure(state="disabled")
        self.file_btn.configure(state="disabled")
        self._chat_append(
            "system", f"Conectando chat a {ip_text}:{CHAT_PORT}…"
        )

    def _apply_chat_state(self, connected: bool) -> None:
        self.chat_connected = connected
        if connected:
            self.chat_connecting = False
            self.client_ip_entry.configure(state="disabled")
            self.chat_btn.configure(
                text="Desconectar chat",
                bg=RED,
                fg="#ffffff",
                activebackground="#da3633",
                state="normal",
            )
            self.chat_entry.configure(state="normal")
            self.chat_send_btn.configure(state="normal")
            self.file_btn.configure(state="normal")
            self.chat_entry.focus_set()
        else:
            self.client_ip_entry.configure(state="normal")
            self.chat_btn.configure(
                text="Conectar chat",
                bg=ACCENT,
                fg="#ffffff",
                activebackground="#1f6feb",
                state="normal",
            )
            self.chat_entry.configure(state="disabled")
            self.chat_send_btn.configure(state="disabled")
            self.file_btn.configure(state="disabled")
            self.var_chat_message.set("")
            self._set_file_status(f"Se guardan en {FILE_DIR}")

    def _send_chat(self, _event: object = None) -> str:
        if not self.chat_connected:
            return "break"
        text = self.var_chat_message.get().strip()
        if not text:
            return "break"
        if self.chat.send(text):
            self._chat_append("outgoing", text)
            self.var_chat_message.set("")
        else:
            self.chat_connected = False
            self.chat_connecting = False
            self._apply_chat_state(False)
            self._chat_append(
                "error", "No se pudo enviar el mensaje: chat desconectado."
            )
        return "break"

    # ---------------------------------------------------------- archivos
    def _set_file_status(self, message: str, color: str = MUTED) -> None:
        if hasattr(self, "file_status"):
            self.file_status.configure(text=message, fg=color)

    def _pick_and_send_file(self) -> None:
        if not self.chat_connected or self.picking_file:
            return
        self.picking_file = True
        self.file_btn.configure(state="disabled")
        self._set_file_status("Abriendo selector de archivos…", AMBER)
        threading.Thread(
            target=self._pick_file_worker, name="file-picker", daemon=True
        ).start()

    def _pick_file_worker(self) -> None:
        try:
            path = pick_file_with_gtk(
                ensure_file_dir(), register=self._register_picker
            )
        except GtkUnavailable as exc:
            self.chat_events.put(("file_pick_error", str(exc)))
            return
        self.chat_events.put(("file_pick_done", path or ""))

    def _register_picker(self, process: subprocess.Popen | None) -> None:
        self.picker_proc = process

    def _handle_selected_file(self, path: str) -> None:
        if not path:
            self._set_file_status(f"Se guardan en {FILE_DIR}")
            return
        if not os.path.isfile(path):
            messagebox.showerror(
                "Archivo no válido",
                f"No se encontró el archivo:\n{path}",
                parent=self.root,
            )
            return
        if not self.chat_connected:
            self._set_file_status(f"Se guardan en {FILE_DIR}")
            return
        if not self.chat.send_file(path):
            self._set_file_status(
                "No se pudo iniciar el envío. ¿Ya hay otro en curso?", RED
            )
            return
        self._set_file_status(
            f"Preparando envío de {os.path.basename(path)}…", AMBER
        )

    def _fallback_file_dialog(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self.root,
            title="Seleccionar archivo para enviar",
            initialdir=ensure_file_dir(),
        )
        self._handle_selected_file(path)

    def _chat_append(self, kind: str, text: str) -> None:
        if not hasattr(self, "chat_log"):
            return
        stamp = time.strftime("%H:%M")
        if kind == "outgoing":
            line = f"[{stamp}] Tú: {text}\n"
            tag = "outgoing"
        elif kind == "incoming":
            line = f"[{stamp}] Remoto: {text}\n"
            tag = "incoming"
        elif kind == "error":
            line = f"[{stamp}] {text}\n"
            tag = "error"
        else:
            line = f"[{stamp}] {text}\n"
            tag = "system"

        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", line, tag)
        self.chat_log.configure(state="disabled")
        self.chat_log.see("end")

        try:
            total = int(self.chat_log.index("end-1c").split(".", 1)[0])
        except (tk.TclError, ValueError):
            return
        if total > 1200:
            self.chat_log.configure(state="normal")
            self.chat_log.delete("1.0", f"{total - 1000}.0")
            self.chat_log.configure(state="disabled")

    def _drain_chat_events(self) -> None:
        try:
            while True:
                kind, payload = self.chat_events.get_nowait()
                if kind == "chat_connected":
                    self.chat_connecting = False
                    if payload.startswith("Conexión entrante de "):
                        peer = payload.split("Conexión entrante de ", 1)[1].strip()
                        ip_text = peer.rsplit(":", 1)[0]
                        if ip_text:
                            self.var_client_ip.set(ip_text)
                    self._apply_chat_state(True)
                    self._chat_append("system", payload)
                elif kind == "chat_disconnected":
                    self.chat_connecting = False
                    self._apply_chat_state(False)
                    self._chat_append("system", payload)
                elif kind == "chat_error":
                    self.chat_connecting = False
                    if not self.chat_connected:
                        self._apply_chat_state(False)
                    self._chat_append("error", payload)
                elif kind == "chat_incoming":
                    self._chat_append("incoming", payload)
                elif kind == "chat_system":
                    self._chat_append("system", payload)
                elif kind == "file_pick_done":
                    self.picking_file = False
                    self.file_btn.configure(
                        state="normal" if self.chat_connected else "disabled"
                    )
                    if payload:
                        self._handle_selected_file(payload)
                    else:
                        self._set_file_status(f"Se guardan en {FILE_DIR}")
                elif kind == "file_pick_error":
                    self.picking_file = False
                    if self.chat_connected:
                        self.file_btn.configure(state="normal")
                        self._set_file_status(
                            "Selector GTK no disponible; usando el de tkinter…",
                            AMBER,
                        )
                        self._fallback_file_dialog()
                    else:
                        self._set_file_status(f"Se guardan en {FILE_DIR}")
                elif kind == "file_send_start":
                    name, size_text = self._split_payload(payload)
                    self._set_file_status(
                        f"Enviando {name} ({format_size(size_text)})…", AMBER
                    )
                    self._chat_append("system", f"Enviando archivo: {name}")
                elif kind == "file_send_progress":
                    name, percent = self._split_payload(payload)
                    self._set_file_status(
                        f"Enviando {name}: {percent}%", AMBER
                    )
                elif kind == "file_send_done":
                    self._set_file_status(f"Archivo enviado: {payload}", GREEN)
                    self._chat_append("system", f"Archivo enviado: {payload}")
                elif kind == "file_send_error":
                    self._set_file_status(f"Error al enviar: {payload}", RED)
                    self._chat_append("error", f"Error al enviar: {payload}")
                elif kind == "file_recv_start":
                    name, size_text = self._split_payload(payload)
                    self._set_file_status(
                        f"Recibiendo {name} ({format_size(size_text)})…", AMBER
                    )
                    self._chat_append("system", f"Recibiendo archivo: {name}")
                elif kind == "file_recv_progress":
                    name, percent = self._split_payload(payload)
                    self._set_file_status(
                        f"Recibiendo {name}: {percent}%", AMBER
                    )
                elif kind == "file_recv_done":
                    self._set_file_status(f"Recibido en {payload}", GREEN)
                    self._chat_append("system", f"Archivo recibido: {payload}")
                elif kind == "file_recv_error":
                    self._set_file_status(f"Error al recibir: {payload}", RED)
                    self._chat_append("error", f"Error al recibir: {payload}")
        except queue.Empty:
            pass

    @staticmethod
    def _split_payload(payload: str) -> tuple[str, str]:
        parts = payload.split("\t", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return payload, ""

    # ------------------------------------------------------------ utilidades
    def _set_client_status(self, message: str, color: str = MUTED) -> None:
        if self.notify_after is not None:
            try:
                self.root.after_cancel(self.notify_after)
            except tk.TclError:
                pass
            self.notify_after = None
        if hasattr(self, "client_status"):
            self.client_status.configure(text=message, fg=color)

    def _default_client_status(self) -> tuple[str, str]:
        if self.ffplay_proc is not None:
            return ("Reproduciendo transmisión SRT.", GREEN)
        return ("Configura la IP y los parámetros para conectar.", MUTED)

    def _notify_client(self, message: str, color: str = MUTED) -> None:
        if not hasattr(self, "client_status"):
            return
        self._set_client_status(message, color)
        self.notify_after = self.root.after(3500, self._reset_client_status)

    def _reset_client_status(self) -> None:
        self.notify_after = None
        if self.closing:
            return
        text, color = self._default_client_status()
        self.client_status.configure(text=text, fg=color)

    def _copy_text(self, text: str, message: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._notify_client(message, color=GREEN)

    def _srt_url(self) -> str:
        ip_text = self.primary_ip or "127.0.0.1"
        if self.state in ("starting", "running", "stopping"):
            port = self.srt_port
        else:
            try:
                port = int(self.var_srt_port.get())
            except ValueError:
                port = self.srt_port
        return f"srt://{ip_text}:{port}"

    def _show_running_detail(self) -> None:
        try:
            latency = self.var_latency.get().strip() or "120"
        except tk.TclError:
            latency = "120"
        self.status_detail.configure(
            text=f"{self._srt_url()}  ·  latencia {latency} ms"
        )

    def _apply_state(self, state: str) -> None:
        self.state = state
        if state == "stopped":
            self.action_btn.configure(
                text="Transmitir imagen",
                bg=GREEN,
                fg="#04150a",
                activebackground="#2ea043",
                state="normal",
            )
            self.status_dot.configure(fg=MUTED)
            self.status_title.configure(text="Detenido")
            self.status_detail.configure(
                text="Pulsa «Transmitir imagen» para iniciar wayviewer y el relay SRT."
            )
        elif state == "starting":
            self.action_btn.configure(
                text="Iniciando…", bg=AMBER, fg="#1f1600", state="disabled"
            )
            self.status_dot.configure(fg=AMBER)
            self.status_title.configure(text="Iniciando transmisión")
            self.status_detail.configure(
                text="Arrancando wayviewer y el relay SRT…"
            )
        elif state == "running":
            self.action_btn.configure(
                text="Detener transmisión",
                bg=RED,
                fg="#ffffff",
                activebackground="#da3633",
                state="normal",
            )
            self.status_dot.configure(fg=GREEN)
            self.status_title.configure(text="Transmitiendo")
            self._show_running_detail()
        elif state == "stopping":
            self.action_btn.configure(
                text="Deteniendo…", bg=AMBER, fg="#1f1600", state="disabled"
            )
            self.status_dot.configure(fg=AMBER)
            self.status_title.configure(text="Deteniendo")
            self.status_detail.configure(text="Cerrando wayviewer y el relay…")

    # -------------------------------------------------------------- procesos
    def _on_action(self) -> None:
        if self.state == "running":
            self._stop()
        elif self.state == "stopped":
            self._start()

    def _start(self) -> None:
        if self.state != "stopped":
            return
        try:
            host = self.var_host.get().strip()
            port = int(self.var_port.get())
            srt_port = int(self.var_srt_port.get())
            latency = int(self.var_latency.get())
            maxbw = int(self.var_maxbw.get())
        except ValueError:
            messagebox.showerror(
                "Parámetros inválidos",
                "El host no puede estar vacío y los puertos, la latencia y el "
                "ancho de banda deben ser números enteros.",
                parent=self.root,
            )
            return
        if not host:
            messagebox.showerror(
                "Parámetros inválidos", "El host de wayviewer no puede estar vacío.",
                parent=self.root,
            )
            return
        if not (1 <= port <= 65535 and 1 <= srt_port <= 65535):
            messagebox.showerror(
                "Parámetros inválidos",
                "Los puertos deben estar entre 1 y 65535.",
                parent=self.root,
            )
            return
        if latency < 1 or maxbw < 1:
            messagebox.showerror(
                "Parámetros inválidos",
                "La latencia y el ancho de banda deben ser mayores que cero.",
                parent=self.root,
            )
            return

        binary = find_wayviewer()
        if not binary:
            messagebox.showerror(
                "No se encontró wayviewer",
                "Busca el binario en build/wayviewer o en la raíz del proyecto, "
                "o define la variable WAYVIEWER_BIN.",
                parent=self.root,
            )
            return
        if not os.path.isfile(RELAY_SCRIPT):
            messagebox.showerror(
                "No se encontró el relay",
                f"Falta el script:\n{RELAY_SCRIPT}",
                parent=self.root,
            )
            return

        python = shutil.which("python3") or sys.executable
        self.srt_port = srt_port
        config = {
            "host": host,
            "port": port,
            "srt_port": srt_port,
            "latency": latency,
            "maxbw": maxbw,
            "binary": binary,
            "python": python,
            "socket": self.socket_path,
        }

        with self.output_lock:
            for lines in self.last_output.values():
                lines.clear()
        self.stop_requested.clear()
        self._apply_state("starting")
        threading.Thread(
            target=self._start_worker, args=(config,), name="wayviewer-start", daemon=True
        ).start()

    def _start_worker(self, config: dict) -> None:
        try:
            self._prepare_socket(config["socket"])
            if self.stop_requested.is_set():
                raise Cancelled

            env = os.environ.copy()
            env["WAYVIEWER_UNIX_SOCKET"] = config["socket"]
            cmd_wv = [
                config["binary"],
                "--host",
                config["host"],
                "--port",
                str(config["port"]),
            ]
            with self.term_lock:
                if self.stop_requested.is_set() or self.closing:
                    raise Cancelled
                wv = subprocess.Popen(
                    cmd_wv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    cwd=ROOT,
                    env=env,
                )
                self.proc_wv = wv
                self.socket_owned = True
            self._reader_thread("wayviewer", wv.stdout)

            if self.stop_requested.is_set() or self.closing:
                raise Cancelled

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self.stop_requested.is_set():
                    raise Cancelled
                returncode = wv.poll()
                if returncode is not None:
                    time.sleep(0.1)
                    detail = self._last_output("wayviewer")
                    message = (
                        f"wayviewer terminó al iniciar (código {returncode})."
                    )
                    if detail:
                        message += "\n\n" + detail
                    raise RuntimeError(message)
                if os.path.exists(config["socket"]):
                    break
                time.sleep(0.05)

            if self.stop_requested.is_set():
                raise Cancelled

            cmd_relay = [
                config["python"],
                RELAY_SCRIPT,
                "--unix-socket",
                config["socket"],
                "--srt-port",
                str(config["srt_port"]),
                "--latency",
                str(config["latency"]),
                "--maxbw",
                str(config["maxbw"]),
            ]
            with self.term_lock:
                if self.stop_requested.is_set() or self.closing:
                    raise Cancelled
                relay = subprocess.Popen(
                    cmd_relay,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    cwd=ROOT,
                )
                self.proc_relay = relay
            self._reader_thread("relay", relay.stdout)

            self.event_queue.put(("state", "running"))
        except Cancelled:
            self._terminate_all(grace=1.5)
            self.event_queue.put(("state", "stopped"))
        except Exception as exc:  # noqa: BLE001 - se muestra al usuario
            self._terminate_all(grace=1.5)
            self.event_queue.put(("error", str(exc)))
            self.event_queue.put(("state", "stopped"))

    def _reader_thread(self, name: str, stream) -> None:
        if stream is None:
            return
        threading.Thread(
            target=self._read_output,
            args=(name, stream),
            name=f"reader-{name}",
            daemon=True,
        ).start()

    def _read_output(self, name: str, stream) -> None:
        """Conserva las últimas líneas solo para diagnosticar errores."""
        try:
            for line in iter(stream.readline, ""):
                with self.output_lock:
                    self.last_output[name].append(line.rstrip())
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _last_output(self, name: str) -> str:
        with self.output_lock:
            lines = list(self.last_output[name])
        return "\n".join(lines)

    def _prepare_socket(self, path: str) -> None:
        if not os.path.exists(path):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.4)
        try:
            try:
                probe.connect(path)
            except (ConnectionRefusedError, FileNotFoundError):
                try:
                    os.unlink(path)
                except OSError as exc:
                    raise RuntimeError(
                        f"No se pudo eliminar el socket obsoleto {path}: {exc}"
                    ) from exc
            except socket.timeout:
                raise RuntimeError(
                    f"El socket {path} está en uso por otro proceso."
                )
            else:
                raise RuntimeError(
                    f"Ya hay un wayviewer escuchando en {path}. "
                    "Detén esa instancia antes de transmitir."
                )
        finally:
            probe.close()

    def _stop(self) -> None:
        if self.state != "running":
            return
        self.stop_requested.set()
        self._apply_state("stopping")
        threading.Thread(
            target=self._stop_worker, name="wayviewer-stop", daemon=True
        ).start()

    def _stop_worker(self) -> None:
        self._terminate_all(grace=2.5)
        self.event_queue.put(("state", "stopped"))

    def _signal_process(self, process: subprocess.Popen, sig: int) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.send_signal(sig)
            except OSError:
                pass

    def _terminate_all(self, grace: float = 2.0) -> None:
        with self.term_lock:
            processes = [
                process
                for process in (self.proc_relay, self.proc_wv)
                if process is not None
            ]
            for process in processes:
                self._signal_process(process, signal.SIGTERM)

            deadline = time.monotonic() + grace
            for process in processes:
                remaining = max(0.05, deadline - time.monotonic())
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    pass

            for process in processes:
                if process.poll() is None:
                    self._signal_process(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass

            self.proc_wv = None
            self.proc_relay = None
            if self.socket_owned:
                try:
                    if os.path.exists(self.socket_path):
                        os.unlink(self.socket_path)
                except OSError:
                    pass
                self.socket_owned = False

    # ------------------------------------------------------------ bucle GUI
    def _poll(self) -> None:
        if self.closing:
            return
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "state":
                    self._apply_state(payload)
                elif kind == "error":
                    messagebox.showerror(
                        "Error de transmisión", payload, parent=self.root
                    )
        except queue.Empty:
            pass

        if self.state == "running":
            for name, process in (
                ("relay", self.proc_relay),
                ("wayviewer", self.proc_wv),
            ):
                if process is None:
                    continue
                returncode = process.poll()
                if returncode is not None:
                    detail = self._last_output(name)
                    message = (
                        f"{name} terminó inesperadamente "
                        f"(código {returncode})."
                    )
                    if detail:
                        message += "\n\n" + detail
                    self.stop_requested.set()
                    self._apply_state("stopping")
                    self.event_queue.put(("error", message))
                    threading.Thread(
                        target=self._stop_worker,
                        name="wayviewer-stop",
                        daemon=True,
                    ).start()
                    break

        if self.ffplay_proc is not None:
            returncode = self.ffplay_proc.poll()
            if returncode is not None:
                was_stopping = self.ffplay_stopping
                self.ffplay_proc = None
                self.ffplay_stopping = False
                self._apply_connect_state(False)
                if was_stopping:
                    self._set_client_status("Desconectado.", MUTED)
                elif returncode == 0:
                    self._set_client_status("Reproducción finalizada.", MUTED)
                else:
                    detail = self._last_output("ffplay")
                    message = f"ffplay terminó con código {returncode}."
                    if detail:
                        message += "\n\n" + detail
                    self._set_client_status("No se pudo conectar.", RED)
                    messagebox.showerror(
                        "Error de conexión", message, parent=self.root
                    )

        self._drain_chat_events()
        self.root.after(100, self._poll)

    def _on_close(self) -> None:
        if (
            self.state in ("running", "starting", "stopping")
            or self.ffplay_proc is not None
            or self.chat_connected
            or self.chat_connecting
        ):
            if not messagebox.askokcancel(
                "Salir",
                "Hay una transmisión, reproducción o chat activo. "
                "¿Detenerlo y salir?",
                parent=self.root,
            ):
                return
        self.closing = True
        self.stop_requested.set()
        self._terminate_ffplay()
        self.chat.stop()
        if self.picker_proc is not None:
            try:
                self.picker_proc.terminate()
            except OSError:
                pass
        self._terminate_all(grace=1.2)
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.stderr.write(f"No se pudo abrir la ventana tkinter: {exc}\n")
        return 1
    WayViewerPanel(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
