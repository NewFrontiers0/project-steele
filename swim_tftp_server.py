"""Read-only TFTP file server for SWIM image pulls."""
from __future__ import annotations

import os
import socket
import struct
import threading
import time
import urllib.parse
from typing import Callable, Optional

from jobs import FIRMWARE_DIR, list_firmware_files


DEFAULT_TFTP_PORT = 69
DEFAULT_BLKSIZE = 1468
MAX_BLKSIZE = 8192
TFTP_TIMEOUT_SECONDS = 5
TFTP_RETRIES = 8

_server = None
_server_port: Optional[int] = None
_server_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress_callbacks = {}


def register_tftp_progress(filename: str, callback: Callable[[int, int, str], None]):
    """Register a callback for bytes sent by the SWIM TFTP server."""
    safe_name = os.path.basename(filename)
    token = object()
    with _progress_lock:
        callbacks = _progress_callbacks.setdefault(safe_name, {})
        callbacks[token] = callback
    return safe_name, token


def unregister_tftp_progress(registration):
    safe_name, token = registration
    with _progress_lock:
        callbacks = _progress_callbacks.get(safe_name)
        if not callbacks:
            return
        callbacks.pop(token, None)
        if not callbacks:
            _progress_callbacks.pop(safe_name, None)


def _emit_progress(filename: str, sent: int, total: int, event: str):
    safe_name = os.path.basename(filename)
    with _progress_lock:
        callbacks = list(_progress_callbacks.get(safe_name, {}).values())
    for callback in callbacks:
        try:
            callback(sent, total, event)
        except Exception:
            pass


def _configured_port() -> int:
    raw = os.environ.get("SWIM_TFTP_PORT", str(DEFAULT_TFTP_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid SWIM_TFTP_PORT value: {raw}") from e
    if port < 1 or port > 65535:
        raise RuntimeError(f"SWIM_TFTP_PORT must be between 1 and 65535: {port}")
    return port


def _configured_blksize() -> int:
    raw = os.environ.get("SWIM_TFTP_BLKSIZE", str(DEFAULT_BLKSIZE)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_BLKSIZE
    return max(512, min(MAX_BLKSIZE, value))


def _parse_rrq(packet: bytes):
    if len(packet) < 4:
        raise ValueError("short TFTP packet")
    opcode = struct.unpack("!H", packet[:2])[0]
    if opcode != 1:
        raise ValueError("not a read request")
    parts = packet[2:].split(b"\0")
    if len(parts) < 2:
        raise ValueError("malformed read request")
    filename = parts[0].decode("utf-8", errors="replace")
    mode = parts[1].decode("ascii", errors="ignore").lower() or "octet"
    options = {}
    option_parts = parts[2:]
    for idx in range(0, max(0, len(option_parts) - 1), 2):
        key = option_parts[idx].decode("ascii", errors="ignore").lower()
        value = option_parts[idx + 1].decode("ascii", errors="ignore")
        if key:
            options[key] = value
    return filename, mode, options


def _resolve_firmware_path(filename: str):
    safe_name = os.path.basename(filename.replace("\\", "/"))
    if safe_name != filename.replace("\\", "/").lstrip("/"):
        raise FileNotFoundError("Firmware file not found")
    if safe_name not in list_firmware_files():
        raise FileNotFoundError("Firmware file not found")
    path = os.path.join(FIRMWARE_DIR, safe_name)
    if not os.path.isfile(path):
        raise FileNotFoundError("Firmware file not found")
    return safe_name, path


def _error_packet(code: int, message: str) -> bytes:
    return struct.pack("!HH", 5, code) + message.encode("utf-8") + b"\0"


def _oack_packet(options) -> bytes:
    payload = bytearray(struct.pack("!H", 6))
    for key, value in options.items():
        payload.extend(key.encode("ascii"))
        payload.append(0)
        payload.extend(str(value).encode("ascii"))
        payload.append(0)
    return bytes(payload)


def _data_packet(block: int, data: bytes) -> bytes:
    return struct.pack("!HH", 3, block & 0xFFFF) + data


def _ack_block(packet: bytes):
    if len(packet) < 4:
        return None
    opcode, block = struct.unpack("!HH", packet[:4])
    if opcode != 4:
        return None
    return block


def _serve_rrq(packet: bytes, client_addr, on_log=None):
    transfer_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    transfer_socket.settimeout(TFTP_TIMEOUT_SECONDS)
    try:
        filename, mode, options = _parse_rrq(packet)
        safe_name, path = _resolve_firmware_path(filename)
        if mode not in {"octet", "netascii"}:
            transfer_socket.sendto(_error_packet(0, "Only octet mode is supported"), client_addr)
            return

        size = os.path.getsize(path)
        blksize = _configured_blksize()
        accepted_options = {}
        if "blksize" in options:
            try:
                requested = int(options["blksize"])
            except ValueError:
                requested = blksize
            blksize = max(512, min(MAX_BLKSIZE, requested, blksize))
            accepted_options["blksize"] = blksize
        if "tsize" in options:
            accepted_options["tsize"] = size
        if "timeout" in options:
            accepted_options["timeout"] = TFTP_TIMEOUT_SECONDS

        if on_log:
            on_log(
                f"TFTP RRQ from {client_addr[0]}:{client_addr[1]} for "
                f"{safe_name} ({size} bytes, block {blksize})"
            )

        with open(path, "rb") as fh:
            sent = 0
            block = 1
            last_packet = b""
            last_emit = 0.0

            if accepted_options:
                last_packet = _oack_packet(accepted_options)
                transfer_socket.sendto(last_packet, client_addr)
                ack = _wait_for_ack(transfer_socket, client_addr, 0, last_packet)
                if ack is None:
                    _emit_progress(safe_name, sent, size, "closed")
                    return

            _emit_progress(safe_name, 0, size, "start")
            while True:
                data = fh.read(blksize)
                last_packet = _data_packet(block, data)
                transfer_socket.sendto(last_packet, client_addr)
                ack = _wait_for_ack(transfer_socket, client_addr, block & 0xFFFF, last_packet)
                if ack is None:
                    _emit_progress(safe_name, sent, size, "closed")
                    return
                sent += len(data)
                now = time.time()
                if size and (now - last_emit >= 1.0 or sent >= size):
                    last_emit = now
                    _emit_progress(safe_name, sent, size, "progress")
                if len(data) < blksize:
                    _emit_progress(safe_name, sent, size, "complete")
                    return
                block += 1
    except FileNotFoundError:
        transfer_socket.sendto(_error_packet(1, "Firmware file not found"), client_addr)
    except Exception as e:
        try:
            transfer_socket.sendto(_error_packet(0, str(e)), client_addr)
        except Exception:
            pass
        if on_log:
            on_log(f"TFTP transfer error for {client_addr[0]}:{client_addr[1]}: {e}")
    finally:
        transfer_socket.close()


def _wait_for_ack(transfer_socket, client_addr, expected_block: int, last_packet: bytes):
    for _ in range(TFTP_RETRIES):
        try:
            packet, addr = transfer_socket.recvfrom(4096)
        except socket.timeout:
            transfer_socket.sendto(last_packet, client_addr)
            continue
        if addr != client_addr:
            transfer_socket.sendto(_error_packet(5, "Unknown transfer ID"), addr)
            continue
        block = _ack_block(packet)
        if block == expected_block:
            return block
    return None


class _TftpServer:
    def __init__(self, host: str, port: int, on_log=None):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._on_log = on_log

    def start(self):
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                packet, addr = self._sock.recvfrom(4096)
            except OSError:
                break
            threading.Thread(
                target=_serve_rrq,
                args=(packet, addr, self._on_log),
                daemon=True,
            ).start()


def ensure_swim_tftp_server(on_log=None) -> int:
    """Start the read-only TFTP server once and return its UDP port."""
    global _server, _server_port
    with _server_lock:
        if _server is not None and _server_port is not None:
            return _server_port

        port = _configured_port()
        try:
            server = _TftpServer("0.0.0.0", port, on_log=on_log)
        except PermissionError as e:
            raise RuntimeError(
                "Could not start SWIM TFTP server on UDP/69. Cisco IOS-XE "
                "expects standard TFTP on UDP/69 and does not accept a port "
                "inside the tftp:// URL. Run the app in Docker, run it with "
                "permission to bind UDP/69, or grant that capability to the "
                "Python runtime."
            ) from e
        except OSError as e:
            raise RuntimeError(
                f"Could not start SWIM TFTP server on UDP/{port}: {e}"
            ) from e
        server.start()
        _server = server
        _server_port = port
        if on_log:
            on_log(f"SWIM TFTP server listening on UDP/{port}")
        return port


def swim_tftp_url(app_base_url: str, filename: str, on_log=None) -> str:
    """Return a switch-reachable TFTP URL for a downloaded firmware image."""
    port = ensure_swim_tftp_server(on_log=on_log)
    parsed = urllib.parse.urlparse(app_base_url)
    host = (os.environ.get("SWIM_TFTP_HOST") or parsed.hostname or "").strip()
    if not host:
        raise RuntimeError("Could not determine app host for SWIM TFTP URL")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    safe_name = os.path.basename(filename)
    public_port_raw = os.environ.get("SWIM_TFTP_PUBLIC_PORT", str(port)).strip()
    try:
        public_port = int(public_port_raw)
    except ValueError:
        public_port = port
    if public_port != 69 and on_log:
        on_log(
            f"WARNING: Cisco IOS-XE usually rejects TFTP URLs with explicit "
            f"ports. UDP/69 is recommended; current public port is {public_port}."
        )
    port_part = "" if public_port == 69 else f":{public_port}"
    return f"tftp://{host}{port_part}/{urllib.parse.quote(safe_name)}"
