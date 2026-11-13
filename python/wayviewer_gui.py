#!/usr/bin/env python3
"""Panel tkinter para transmitir con wayviewer/relay SRT y generar el
comando de conexión para el cliente.

Sección «Transmitir imagen»:
  arranca wayviewer + python/wayviewer_relay.py.

Sección «Conectar»:
  genera el comando ffplay para el equipo cliente, permitiendo elegir IP,
  puerto y varios parámetros.
"""

from __future__ import annotations

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


class TcpChat:
    """Chat TCP mínimo: escucha en un puerto y permite una conexión a la vez."""

    def __init__(self, port: int, events: queue.Queue[tuple[str, str]]):
        self.port = port
        self.events = events
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.sock: socket.socket | None = None
        self.server: socket.socket | None = None
        self.listener_thread: threading.Thread | None = None
        self.reader_thread: threading.Thread | None = None
        self.connecting = False
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
                if text:
                    self.events.put(("chat_incoming", text[:4000]))
        except OSError:
            reason = "Conexión de chat interrumpida."
        finally:
            try:
                reader.close()
            except OSError:
                pass

        if self.stop_event.is_set():
            self._clear(conn)
        else:
            self._drop(conn, reason)

    def send(self, text: str) -> bool:
        with self.lock:
            conn = self.sock
        if conn is None:
            return False
        try:
            conn.sendall((text + "\n").encode("utf-8"))
        except OSError as exc:
            self._drop(conn, f"Error al enviar: {exc}")
            return False
        return True

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
        for thread in (self.listener_thread, self.reader_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.5)


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
        self.closing = False
        self.socket_path = default_socket_path()
        self.interfaces: list[tuple[str, str]] = []
        self.chip_signature: tuple[tuple[str, str], ...] = ()
        self.primary_ip = ""
        self.srt_port = 9000
        self.notify_after: str | None = None

        self.var_host = tk.StringVar(value="127.0.0.1")
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
        self.root.minsize(860, 560)
        width, height = 980, 800
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=HEADER)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=HEADER)
        inner.pack(fill="x", padx=18, pady=(12, 10))

        top = tk.Frame(inner, bg=HEADER)
        top.pack(fill="x")
        tk.Label(
            top, text="WayViewer", bg=HEADER, fg=FG,
            font=("DejaVu Sans", 16, "bold"),
        ).pack(side="left")

        tk.Button(
            top,
            text="Actualizar IPs",
            command=lambda: self._refresh_interfaces(force=True),
            bg=CHIP,
            fg=FG,
            activebackground=BORDER,
            activeforeground=FG,
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
            font=("DejaVu Sans", 9),
        ).pack(side="right")

        row = tk.Frame(inner, bg=HEADER)
        row.pack(fill="x", pady=(12, 0))
        tk.Label(
            row, text="INTERFACES", bg=HEADER, fg=MUTED,
            font=("DejaVu Sans", 8, "bold"),
        ).pack(side="left", padx=(0, 12))
        self.chips = tk.Frame(row, bg=HEADER)
        self.chips.pack(side="left", fill="x", expand=True)

    def _make_card(self, parent: tk.Widget, title: str) -> tuple[tk.Frame, tk.Frame]:
        card = tk.Frame(
            parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER
        )
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(
            inner, text=title, bg=PANEL, fg=FG, font=CARD_TITLE
        ).pack(anchor="w")
        return card, inner

    def _field_cell(
        self, parent: tk.Widget, column: int, label: str
    ) -> tk.Frame:
        cell = tk.Frame(parent, bg=PANEL)
        cell.grid(row=0, column=column, sticky="w", padx=(0, 16))
        tk.Label(
            cell, text=label, bg=PANEL, fg=MUTED, font=("DejaVu Sans", 9)
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
        self.body_canvas.bind(
            "<Configure>",
            lambda event: self.body_canvas.itemconfigure(
                self.body_window, width=event.width
            ),
        )
        self.root.bind_all("<MouseWheel>", self._on_body_wheel)
        self.root.bind_all("<Button-4>", self._on_body_wheel)
        self.root.bind_all("<Button-5>", self._on_body_wheel)

        body = tk.Frame(holder, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # --- Sección Transmitir imagen ------------------------------------
        tx_card, tx = self._make_card(body, "Transmitir imagen")
        tx_card.pack(fill="x")

        grid = tk.Frame(tx, bg=PANEL)
        grid.pack(fill="x", pady=(10, 0))
        fields = [
            ("Host wayviewer", self.var_host, 16),
            ("Puerto wayviewer", self.var_port, 9),
            ("Puerto SRT", self.var_srt_port, 9),
            ("Latencia (ms)", self.var_latency, 9),
        ]
        for column, (label, variable, width) in enumerate(fields):
            cell = self._field_cell(grid, column, label)
            self._dark_entry(cell, variable, width).pack(
                anchor="w", pady=(4, 0), ipady=3
            )

        tk.Label(
            tx,
            text=f"Socket Unix: {self.socket_path}",
            bg=PANEL,
            fg=MUTED,
            font=MONO_SMALL,
        ).pack(anchor="w", pady=(10, 0))

        actions = tk.Frame(tx, bg=PANEL)
        actions.pack(fill="x", pady=(12, 0))

        self.action_btn = tk.Button(
            actions,
            text="Transmitir imagen",
            command=self._on_action,
            bg=GREEN,
            fg="#04150a",
            activebackground="#2ea043",
            activeforeground="#04150a",
            disabledforeground="#6e7681",
            relief="flat",
            padx=26,
            pady=10,
            cursor="hand2",
            font=SANS_BOLD,
        )
        self.action_btn.pack(side="left")

        status = tk.Frame(actions, bg=PANEL)
        status.pack(side="left", padx=(18, 0))
        self.status_dot = tk.Label(
            status, text="●", bg=PANEL, fg=MUTED, font=("DejaVu Sans", 15)
        )
        self.status_dot.pack(side="left", anchor="n", pady=(2, 0))
        status_text = tk.Frame(status, bg=PANEL)
        status_text.pack(side="left", padx=(6, 0))
        self.status_title = tk.Label(
            status_text, text="", bg=PANEL, fg=FG,
            font=("DejaVu Sans", 10, "bold"),
        )
        self.status_title.pack(anchor="w")
        self.status_detail = tk.Label(
            status_text, text="", bg=PANEL, fg=MUTED, font=("DejaVu Sans", 9)
        )
        self.status_detail.pack(anchor="w")

        # --- Sección Conectar ---------------------------------------------
        cx_card, cx = self._make_card(body, "Conectar")
        cx_card.pack(fill="both", expand=True, pady=(14, 0))

        client_grid = tk.Frame(cx, bg=PANEL)
        client_grid.pack(fill="x", pady=(10, 0))

        ip_cell = self._field_cell(client_grid, 0, "IP de destino")
        self.client_ip_entry = self._dark_entry(
            ip_cell, self.var_client_ip, 20
        )
        self.client_ip_entry.pack(anchor="w", pady=(4, 0), ipady=3)

        port_cell = self._field_cell(client_grid, 1, "Puerto SRT")
        self._dark_entry(port_cell, self.var_srt_port, 9).pack(
            anchor="w", pady=(4, 0), ipady=3
        )

        latency_cell = self._field_cell(client_grid, 2, "Latencia (ms)")
        self._dark_entry(latency_cell, self.var_client_latency, 9).pack(
            anchor="w", pady=(4, 0), ipady=3
        )

        options = tk.Frame(cx, bg=PANEL)
        options.pack(fill="x", pady=(10, 0))
        tk.Label(
            options, text="Opciones:", bg=PANEL, fg=MUTED,
            font=("DejaVu Sans", 9),
        ).pack(side="left", padx=(0, 8))

        for label, variable in (
            ("nobuffer", self.var_nobuffer),
            ("low_delay", self.var_low_delay),
            ("framedrop", self.var_framedrop),
            ("tlpktdrop", self.var_tlpktdrop),
        ):
            tk.Checkbutton(
                options,
                text=label,
                variable=variable,
                bg=PANEL,
                fg=FG,
                selectcolor="#0b0e13",
                activebackground=PANEL,
                activeforeground=FG,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
                font=("DejaVu Sans", 9),
            ).pack(side="left", padx=(0, 12))

        footer = tk.Frame(cx, bg=PANEL)
        footer.pack(fill="x", pady=(12, 0))
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
        self.connect_btn.pack(side="left")

        self.client_status = tk.Label(
            footer,
            text="Escribe la IP de destino y pulsa el botón.",
            bg=PANEL,
            fg=MUTED,
            font=("DejaVu Sans", 9),
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.client_status.pack(side="left", padx=(14, 0))

        # --- Chat TCP -----------------------------------------------------
        tk.Frame(cx, bg=BORDER, height=1).pack(fill="x", pady=(12, 0))
        chat_head = tk.Frame(cx, bg=PANEL)
        chat_head.pack(fill="x", pady=(10, 0))
        tk.Label(
            chat_head,
            text=f"Chat TCP · puerto {CHAT_PORT}",
            bg=PANEL,
            fg=FG,
            font=("DejaVu Sans", 9, "bold"),
        ).pack(side="left")
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
            padx=12,
            pady=4,
            cursor="hand2",
            font=("DejaVu Sans", 9, "bold"),
        )
        self.chat_btn.pack(side="right")

        chat_box = tk.Frame(
            cx, bg="#0b0e13", highlightthickness=1, highlightbackground=BORDER
        )
        chat_box.pack(fill="both", expand=True, pady=(6, 0))
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
            height=6,
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

        if not interfaces:
            tk.Label(
                self.chips,
                text="No se detectaron interfaces IPv4",
                bg=HEADER,
                fg=MUTED,
                font=("DejaVu Sans", 9),
            ).pack(side="left")
        else:
            for name, ip_text in interfaces:
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
                chip.pack(side="left", padx=(0, 8))
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
            self.var_chat_message.set("")

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
        except queue.Empty:
            pass

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
