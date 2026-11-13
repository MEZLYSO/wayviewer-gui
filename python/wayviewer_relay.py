#!/usr/bin/env python3
"""Fan out wayviewer's MPEG-TS stream to multiple SRT clients.

wayviewer already encodes the screen into MPEG-TS and can expose the encoded
byte stream on a Unix stream socket (WAYVIEWER_UNIX_SOCKET).  This process reads
that stream once and forwards it to any number of SRT callers, without decoding
or re-encoding anything.

Only the Python standard library is used.  The SRT binding is a small ctypes
wrapper around the system libsrt (tested with libsrt 1.5.x).

New clients start on an IDR frame.  wayviewer emits an IDR whenever a new
client connects to its Unix/TCP listener, so the relay opens a short-lived
connection to the Unix socket when an SRT client arrives.  Until the IDR is
seen, no data is queued for that client.  This avoids both a blank screen and
the extra latency that would come from replaying a cached GOP.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque

LOG = logging.getLogger("wayviewer-relay")

TS_PACKET_SIZE = 188
# 7 TS packets = 1316 bytes, the usual SRT live payload size.
CHUNK_SIZE = 7 * TS_PACKET_SIZE
READ_SIZE = 64 * 1024

# SRT option numbers from srtcore/srt.h (libsrt 1.5.x).
SRTO_SNDSYN = 1
SRTO_RCVSYN = 2
SRTO_SNDBUF = 5
SRTO_RCVBUF = 6
SRTO_SNDTIMEO = 13
SRTO_RCVTIMEO = 14
SRTO_REUSEADDR = 15
SRTO_MAXBW = 16
SRTO_TSBPDMODE = 22
SRTO_LATENCY = 23
SRTO_PASSPHRASE = 26
SRTO_TLPKTDROP = 31
SRTO_TRANSTYPE = 50
SRTO_IPV6ONLY = 54
SRTO_PEERIDLETIMEO = 55

SRTT_LIVE = 0


class SrtError(RuntimeError):
    pass


class SrtLibrary:
    """Minimal ctypes binding for the libsrt sender side."""

    def __init__(self, library: str | None = None):
        self.lib = self._load(library)
        self._configure()
        if self.lib.srt_startup() != 0:
            raise SrtError("srt_startup() failed")
        self._started = True

    @staticmethod
    def _load(library: str | None):
        candidates = []
        if library:
            candidates.append(library)
        env = os.environ.get("WAYVIEWER_SRT_LIB")
        if env:
            candidates.append(env)
        candidates.extend(("libsrt.so.1.5", "libsrt.so.1", "libsrt.so"))
        errors = []
        for name in candidates:
            try:
                return ctypes.CDLL(name)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
        raise SrtError("cannot load libsrt (" + "; ".join(errors) + ")")

    def _configure(self):
        lib = self.lib
        lib.srt_startup.restype = ctypes.c_int
        lib.srt_cleanup.restype = ctypes.c_int
        lib.srt_create_socket.restype = ctypes.c_int
        lib.srt_close.argtypes = [ctypes.c_int]
        lib.srt_close.restype = ctypes.c_int
        lib.srt_setsockflag.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.srt_setsockflag.restype = ctypes.c_int
        lib.srt_bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.srt_bind.restype = ctypes.c_int
        lib.srt_listen.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.srt_listen.restype = ctypes.c_int
        lib.srt_accept.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.srt_accept.restype = ctypes.c_int
        lib.srt_sendmsg.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.srt_sendmsg.restype = ctypes.c_int
        lib.srt_getlasterror_str.restype = ctypes.c_char_p

    def close(self):
        if getattr(self, "_started", False):
            self.lib.srt_cleanup()
            self._started = False

    def last_error(self) -> str:
        raw = self.lib.srt_getlasterror_str()
        return raw.decode("utf-8", "replace") if raw else "unknown SRT error"

    def _set(self, sock: int, opt: int, value, size: int):
        rc = self.lib.srt_setsockflag(sock, opt, ctypes.byref(value), size)
        if rc < 0:
            raise SrtError(f"srt_setsockflag({opt}) failed: {self.last_error()}")

    def set_int(self, sock: int, opt: int, value: int):
        self._set(sock, opt, ctypes.c_int(value), ctypes.sizeof(ctypes.c_int))

    def set_i64(self, sock: int, opt: int, value: int):
        self._set(sock, opt, ctypes.c_int64(value), ctypes.sizeof(ctypes.c_int64))

    def set_string(self, sock: int, opt: int, value: str):
        data = value.encode("utf-8")
        buf = ctypes.create_string_buffer(data, len(data))
        rc = self.lib.srt_setsockflag(sock, opt, buf, len(data))
        if rc < 0:
            raise SrtError(f"srt_setsockflag({opt}) failed: {self.last_error()}")

    def create_socket(self) -> int:
        sock = self.lib.srt_create_socket()
        if sock < 0:
            raise SrtError(f"srt_create_socket() failed: {self.last_error()}")
        return sock

    def bind(self, sock: int, host: str, port: int):
        addr = _pack_sockaddr(host, port)
        if self.lib.srt_bind(sock, addr, len(addr)) < 0:
            raise SrtError(
                f"srt_bind({host}:{port}) failed: {self.last_error()}"
            )

    def listen(self, sock: int, backlog: int = 8):
        if self.lib.srt_listen(sock, backlog) < 0:
            raise SrtError(f"srt_listen() failed: {self.last_error()}")

    def accept(self, sock: int) -> int:
        return self.lib.srt_accept(sock, None, None)

    def sendmsg(self, sock: int, data: bytes) -> int:
        return self.lib.srt_sendmsg(sock, data, len(data), -1, 0)

    def close_socket(self, sock: int):
        if sock >= 0:
            self.lib.srt_close(sock)


def _pack_sockaddr(host: str, port: int) -> bytes:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    if not infos:
        raise SrtError(f"cannot resolve {host!r}")
    family, _, _, _, sockaddr = infos[0]
    net_port = socket.htons(port)
    if family == socket.AF_INET:
        return struct.pack(
            "=HH4s8x", socket.AF_INET, net_port, socket.inet_aton(sockaddr[0])
        )
    if family == socket.AF_INET6:
        return struct.pack(
            "=HH4s16sI",
            socket.AF_INET6,
            net_port,
            b"\0" * 4,
            socket.inet_pton(socket.AF_INET6, sockaddr[0]),
            0,
        )
    raise SrtError(f"unsupported address family for {host!r}")


class TsParser:
    """Split MPEG-TS bytes into chunks and find IDR-aligned chunk starts."""

    def __init__(self, on_chunk):
        self.on_chunk = on_chunk
        self.buf = bytearray()
        self.chunk = bytearray()
        self.chunk_is_keyframe = False

        self.pmt_pid: int | None = None
        self.video_pid: int | None = None
        self.codec: str | None = None

        self.pat_packets: list[bytes] = []
        self.pmt_packets: list[bytes] = []
        self.pat_section = bytearray()
        self.pmt_section = bytearray()
        self.pat_expected = 0
        self.pmt_expected = 0

        # Most recent complete PAT/PMT packets, sent once to a client that is
        # starting on a keyframe.  They are *not* embedded in stream chunks:
        # replaying them would make their continuity counters jump.
        self.latest_pat = b""
        self.latest_pmt = b""

        self.keyframes = 0
        self.packets = 0

    def reset(self):
        self.buf.clear()
        self.chunk.clear()
        self.chunk_is_keyframe = False
        self.pmt_pid = None
        self.video_pid = None
        self.codec = None
        self.pat_packets.clear()
        self.pmt_packets.clear()
        self.pat_section.clear()
        self.pmt_section.clear()
        self.pat_expected = 0
        self.pmt_expected = 0
        self.latest_pat = b""
        self.latest_pmt = b""

    def feed(self, data: bytes):
        self.buf += data
        while len(self.buf) >= TS_PACKET_SIZE:
            if self.buf[0] != 0x47:
                idx = self.buf.find(0x47)
                if idx < 0:
                    self.buf.clear()
                    return
                del self.buf[:idx]
                if len(self.buf) < TS_PACKET_SIZE:
                    return
            packet = bytes(self.buf[:TS_PACKET_SIZE])
            del self.buf[:TS_PACKET_SIZE]
            self._packet(packet)

    def _packet(self, packet: bytes):
        if packet[1] & 0x80:  # transport_error_indicator
            return

        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        pusi = bool(packet[1] & 0x40)
        afc = (packet[3] >> 4) & 0x03

        if afc & 0x02:  # adaptation field present
            offset = 5 + packet[4]
        else:
            offset = 4
        if not (afc & 0x01) or offset >= TS_PACKET_SIZE:
            return
        payload = packet[offset:]

        if pid == 0:
            self._handle_pat(packet, payload, pusi)
        elif self.pmt_pid is not None and pid == self.pmt_pid:
            self._handle_pmt(packet, payload, pusi)

        keyframe = False
        if self.video_pid is not None and pid == self.video_pid and pusi:
            if self.pat_packets and self.pmt_packets and self._is_keyframe(payload):
                keyframe = True

        if keyframe:
            # End the previous chunk right before the keyframe so a client can
            # start decoding here.  PAT/PMT are not embedded in the chunk; they
            # are prepended per client by SrtClient.offer().
            self._flush()
            self.chunk_is_keyframe = True
            self.keyframes += 1

        self.packets += 1
        self.chunk += packet
        if len(self.chunk) >= CHUNK_SIZE:
            self._flush()

    def psi(self) -> bytes:
        """Latest PAT+PMT packets, for a client starting at a keyframe."""
        return self.latest_pat + self.latest_pmt

    def _flush(self):
        if not self.chunk:
            return
        chunk = bytes(self.chunk)
        self.chunk.clear()
        keyframe = self.chunk_is_keyframe
        self.chunk_is_keyframe = False
        self.on_chunk(chunk, keyframe)

    def _handle_pat(self, packet: bytes, payload: bytes, pusi: bool):
        if pusi:
            pointer = payload[0]
            section = payload[1 + pointer:]
            if len(section) < 3:
                return
            self.pat_packets = [packet]
            self.pat_section = bytearray(section)
            self.pat_expected = 3 + (((section[1] & 0x0F) << 8) | section[2])
        else:
            if not self.pat_section:
                return
            self.pat_packets.append(packet)
            self.pat_section += payload

        if self.pat_expected and len(self.pat_section) >= self.pat_expected:
            self.pat_section = self.pat_section[: self.pat_expected]
            self.latest_pat = b"".join(self.pat_packets)
            self._parse_pat(self.pat_section)

    def _handle_pmt(self, packet: bytes, payload: bytes, pusi: bool):
        if pusi:
            pointer = payload[0]
            section = payload[1 + pointer:]
            if len(section) < 3:
                return
            self.pmt_packets = [packet]
            self.pmt_section = bytearray(section)
            self.pmt_expected = 3 + (((section[1] & 0x0F) << 8) | section[2])
        else:
            if not self.pmt_section:
                return
            self.pmt_packets.append(packet)
            self.pmt_section += payload

        if self.pmt_expected and len(self.pmt_section) >= self.pmt_expected:
            self.pmt_section = self.pmt_section[: self.pmt_expected]
            self.latest_pmt = b"".join(self.pmt_packets)
            self._parse_pmt(self.pmt_section)

    def _parse_pat(self, section: bytearray):
        if len(section) < 12 or section[0] != 0x00:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        end = 3 + section_length - 4
        i = 8
        while i + 4 <= end:
            program = (section[i] << 8) | section[i + 1]
            pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
            if program != 0:
                self.pmt_pid = pid
                return
            i += 4

    def _parse_pmt(self, section: bytearray):
        if len(section) < 16 or section[0] != 0x02:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        program_info_length = ((section[10] & 0x0F) << 8) | section[11]
        end = 3 + section_length - 4
        i = 12 + program_info_length
        while i + 5 <= end:
            stream_type = section[i]
            pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            es_info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
            if stream_type == 0x1B:
                self.video_pid = pid
                self.codec = "h264"
            elif stream_type == 0x24:
                self.video_pid = pid
                self.codec = "hevc"
            i += 5 + es_info_length

    def _is_keyframe(self, payload: bytes) -> bool:
        if not payload:
            return False
        # For PES packets the payload starts directly with the PES start code;
        # the pointer_field is only used for PSI sections (PAT/PMT).
        pes = payload
        if len(pes) < 9 or pes[0:3] != b"\x00\x00\x01":
            return False
        header_len = pes[8]
        start = 9 + header_len
        if start >= len(pes):
            return False
        return self._scan_nal(pes[start:])

    def _scan_nal(self, data: bytes) -> bool:
        """Return True if the first VCL NAL in the PES is an IDR/IRAP."""
        hevc = self.codec == "hevc"
        i = 0
        limit = min(len(data), 4096)
        while i + 4 < limit:
            if data[i] != 0 or data[i + 1] != 0:
                i += 1
                continue
            if data[i + 2] == 1:
                sc_len = 3
            elif data[i + 2] == 0 and data[i + 3] == 1:
                sc_len = 4
            else:
                i += 1
                continue
            if i + sc_len >= limit:
                return False
            if hevc:
                nal_type = (data[i + sc_len] >> 1) & 0x3F
                if 16 <= nal_type <= 23:  # BLA/IDR/CRA
                    return True
                if nal_type <= 31:  # first VCL NAL is not an IRAP
                    return False
            else:
                nal_type = data[i + sc_len] & 0x1F
                if nal_type == 5:
                    return True
                if nal_type == 1:
                    return False
            i += sc_len + 1
        return False


class SrtClient:
    def __init__(self, relay: "Relay", sock: int):
        self.relay = relay
        self.sock = sock
        self.alive = True
        self.waiting_for_keyframe = True
        self.queue: deque[bytes] = deque()
        self.queued_bytes = 0
        self.cv = threading.Condition()
        self.bytes_sent = 0
        self.drops = 0
        self.connected_at = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, name="srt-client", daemon=True
        )

    def start(self):
        self.thread.start()

    def offer(self, chunk: bytes, keyframe: bool, psi: bytes = b"") -> bool:
        """Queue a chunk. Returns True when a keyframe should be requested."""
        request_keyframe = False
        with self.cv:
            if not self.alive:
                return False
            if self.waiting_for_keyframe:
                if not keyframe:
                    return False
                self.waiting_for_keyframe = False
                # This client's first bytes: current PAT/PMT so the demuxer
                # learns the PIDs immediately, then the IDR PES packet.
                if psi:
                    chunk = psi + chunk

            if (
                self.queue
                and self.queued_bytes + len(chunk) > self.relay.max_queue_bytes
            ):
                # The client is too far behind. Throw the queue away and resume
                # at the next keyframe instead of sending stale video.
                self.queue.clear()
                self.queued_bytes = 0
                self.waiting_for_keyframe = True
                self.drops += 1
                request_keyframe = True
            else:
                self.queue.append(chunk)
                self.queued_bytes += len(chunk)
                self.cv.notify()
        return request_keyframe

    def _run(self):
        try:
            while True:
                with self.cv:
                    while self.alive and not self.queue:
                        self.cv.wait(0.5)
                    if not self.alive:
                        break
                    data = self.queue.popleft()
                    self.queued_bytes -= len(data)
                self._send_all(data)
                self.bytes_sent += len(data)
        except SrtError as exc:
            if self.alive:
                LOG.warning("SRT client send failed: %s", exc)
        finally:
            with self.cv:
                self.alive = False
                self.queue.clear()
                self.queued_bytes = 0
            self.relay.srt.close_socket(self.sock)
            LOG.info(
                "SRT client disconnected after %.1fs (%d bytes, %d resyncs)",
                time.monotonic() - self.connected_at,
                self.bytes_sent,
                self.drops,
            )

    def _send_all(self, data: bytes):
        total = len(data)
        offset = 0
        while offset < total:
            if not self.alive:
                raise SrtError("client stopped")
            # SRT live mode rejects messages larger than 7 TS packets (1316 B),
            # so split the buffer (always a multiple of 188) into messages.
            part = data[offset : offset + CHUNK_SIZE]
            sent = self.relay.srt.sendmsg(self.sock, part)
            if sent < 0:
                raise SrtError(self.relay.srt.last_error())
            if sent == 0:
                time.sleep(0.001)
                continue
            offset += sent

    def stop(self):
        with self.cv:
            self.alive = False
            self.queue.clear()
            self.queued_bytes = 0
            self.cv.notify_all()

    def join(self, timeout: float = 2.0):
        if self.thread.is_alive():
            self.thread.join(timeout)


class Relay:
    def __init__(self, args):
        self.args = args
        self.srt = SrtLibrary(args.srt_lib)
        self.listener = -1
        self.clients: list[SrtClient] = []
        self.clients_lock = threading.Lock()
        self.accept_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        self.parser = TsParser(self._on_chunk)
        self.bytes_in = 0
        self.last_bytes_in = 0
        self.started_at = time.monotonic()
        self.last_stats = self.started_at
        self.last_connect_log = 0.0

        self.max_queue_bytes = args.queue_bytes

        self._kf_lock = threading.Lock()
        self._last_keyframe_request = 0.0

    # ---- keyframe handling -------------------------------------------------
    def request_keyframe(self, force: bool = False):
        if not self.args.trigger_keyframe:
            return
        now = time.monotonic()
        with self._kf_lock:
            if not force and now - self._last_keyframe_request < 0.1:
                return
            self._last_keyframe_request = now
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            sock.connect(self.args.unix_socket)
            sock.close()
            LOG.debug("requested an IDR from wayviewer")
        except OSError as exc:
            LOG.debug("cannot request IDR: %s", exc)

    # ---- SRT listener ------------------------------------------------------
    def _open_listener(self):
        srt = self.srt
        sock = srt.create_socket()
        try:
            srt.set_int(sock, SRTO_TRANSTYPE, SRTT_LIVE)
            srt.set_int(sock, SRTO_LATENCY, self.args.latency)
            srt.set_int(sock, SRTO_TLPKTDROP, 1)
            srt.set_int(sock, SRTO_REUSEADDR, 1)
            if self.args.maxbw:
                srt.set_i64(sock, SRTO_MAXBW, self.args.maxbw * 1000 // 8)
            if self.args.passphrase:
                srt.set_string(sock, SRTO_PASSPHRASE, self.args.passphrase)
            srt.bind(sock, self.args.srt_bind, self.args.srt_port)
            srt.listen(sock, 8)
        except Exception:
            srt.close_socket(sock)
            raise
        self.listener = sock
        LOG.info(
            "SRT listening on srt://%s:%d (latency %d ms, maxbw %s kbit/s)",
            self.args.srt_bind,
            self.args.srt_port,
            self.args.latency,
            self.args.maxbw or "unlimited",
        )
        LOG.info(
            "client: ffplay -probesize 65536 -analyzeduration 100000 "
            "-fflags nobuffer -flags low_delay -framedrop "
            "'srt://<host>:%d?mode=caller&latency=%d&tlpktdrop=1'",
            self.args.srt_port,
            self.args.latency,
        )

    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                conn = self.srt.accept(self.listener)
            except Exception as exc:  # ctypes does not raise on its own
                if self.stop_event.is_set():
                    break
                LOG.warning("SRT accept failed: %s", exc)
                time.sleep(0.2)
                continue
            if conn < 0:
                if self.stop_event.is_set():
                    break
                LOG.warning("SRT accept failed: %s", self.srt.last_error())
                time.sleep(0.2)
                continue

            try:
                self.srt.set_int(conn, SRTO_SNDTIMEO, 1000)
            except SrtError as exc:
                LOG.warning("cannot configure SRT client: %s", exc)

            client = SrtClient(self, conn)
            with self.clients_lock:
                self.clients.append(client)
                total = len(self.clients)
            client.start()
            LOG.info("SRT client connected (%d total)", total)
            self.request_keyframe(force=True)
            self._reap_clients()

    def _reap_clients(self):
        dead = []
        with self.clients_lock:
            for client in list(self.clients):
                if not client.alive:
                    self.clients.remove(client)
                    dead.append(client)
        for client in dead:
            client.join()

    # ---- source stream -----------------------------------------------------
    def _on_chunk(self, chunk: bytes, keyframe: bool):
        with self.clients_lock:
            clients = list(self.clients)
        psi = self.parser.psi() if keyframe else b""
        need_keyframe = False
        for client in clients:
            if client.offer(chunk, keyframe, psi):
                need_keyframe = True
        if need_keyframe:
            self.request_keyframe(force=True)

    def _mark_all_waiting(self):
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            with client.cv:
                client.queue.clear()
                client.queued_bytes = 0
                client.waiting_for_keyframe = True

    def _connect_source(self) -> socket.socket | None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(self.args.unix_socket)
            LOG.info("connected to wayviewer at %s", self.args.unix_socket)
            return sock
        except OSError as exc:
            now = time.monotonic()
            if now - self.last_connect_log > 5.0:
                self.last_connect_log = now
                LOG.warning("waiting for %s: %s", self.args.unix_socket, exc)
            return None

    def _read_loop(self, sock: socket.socket):
        while not self.stop_event.is_set():
            try:
                data = sock.recv(READ_SIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                LOG.warning("Unix socket read failed: %s", exc)
                break
            if not data:
                LOG.warning("wayviewer closed the Unix socket")
                break
            self.bytes_in += len(data)
            self.parser.feed(data)
            self._maybe_stats()

    def _maybe_stats(self):
        now = time.monotonic()
        if now - self.last_stats < self.args.stats_interval:
            return
        elapsed = now - self.last_stats
        self.last_stats = now
        delta = self.bytes_in - self.last_bytes_in
        self.last_bytes_in = self.bytes_in
        with self.clients_lock:
            clients = list(self.clients)
        LOG.info(
            "stats: %.0f kbit/s in, %d client(s)%s",
            delta * 8 / elapsed / 1000,
            len(clients),
            "".join(
                f", queued {c.queued_bytes}B"
                f"/sent {c.bytes_sent}B/resync {c.drops}"
                for c in clients
            ),
        )
        self._reap_clients()

    # ---- lifecycle ---------------------------------------------------------
    def run(self):
        self._open_listener()
        self.accept_thread = threading.Thread(
            target=self._accept_loop, name="srt-accept", daemon=True
        )
        self.accept_thread.start()

        while not self.stop_event.is_set():
            sock = self._connect_source()
            if sock is None:
                self.stop_event.wait(1.0)
                continue
            try:
                self._read_loop(sock)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                self.parser.reset()
                self._mark_all_waiting()
            if not self.stop_event.is_set():
                self.stop_event.wait(1.0)

    def stop(self):
        self.stop_event.set()
        self.srt.close_socket(self.listener)
        self.listener = -1
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.stop()

    def close(self):
        self.stop()
        if self.accept_thread is not None:
            self.accept_thread.join(timeout=2.0)
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.join()
        self.srt.close()


def default_socket_path() -> str:
    env = os.environ.get("WAYVIEWER_UNIX_SOCKET")
    if env:
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "wayviewer.sock")
    return f"/tmp/wayviewer-{os.getuid()}.sock"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fan out wayviewer's MPEG-TS stream to multiple SRT clients."
    )
    parser.add_argument(
        "--unix-socket",
        default=default_socket_path(),
        help="wayviewer Unix stream socket (default: %(default)s)",
    )
    parser.add_argument(
        "--srt-port", type=int, default=9000, help="SRT UDP port (default: 9000)"
    )
    parser.add_argument(
        "--srt-bind",
        default="0.0.0.0",
        help="SRT bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--latency",
        type=int,
        default=120,
        help="SRT latency in ms (default: 120)",
    )
    parser.add_argument(
        "--maxbw",
        type=int,
        default=8000,
        help="SRT max bandwidth in kbit/s per client, 0 = unlimited (default: 8000)",
    )
    parser.add_argument(
        "--passphrase",
        default="",
        help="SRT encryption passphrase (10-79 characters, default: off)",
    )
    parser.add_argument(
        "--queue-bytes",
        type=int,
        default=128 * 1024,
        help="per-client queue limit before resyncing on a keyframe (default: 131072)",
    )
    parser.add_argument(
        "--no-trigger-keyframe",
        dest="trigger_keyframe",
        action="store_false",
        help="do not ask wayviewer for an IDR when a client connects",
    )
    parser.add_argument(
        "--stats-interval",
        type=float,
        default=5.0,
        help="seconds between statistics lines (default: 5)",
    )
    parser.add_argument(
        "--srt-lib", default=None, help="path to libsrt (default: auto-detect)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.latency < 20:
        LOG.warning("SRT latency below 20 ms is not useful on Wi-Fi")
    if args.passphrase and not (10 <= len(args.passphrase) <= 79):
        LOG.error("SRT passphrase must be 10-79 characters")
        return 2

    try:
        relay = Relay(args)
    except SrtError as exc:
        LOG.error("%s", exc)
        return 1

    def handle_signal(signum, _frame):
        LOG.info("received signal %d, stopping", signum)
        relay.stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        relay.run()
    except SrtError as exc:
        LOG.error("%s", exc)
        relay.close()
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
