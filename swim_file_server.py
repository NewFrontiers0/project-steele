"""Tiny HTTP file server for SWIM image pulls.

IOS-XE can be picky about HTTP downloads from application frameworks. This
server intentionally behaves like a simple static file server: one request,
one Content-Length, then close the connection.
"""
from __future__ import annotations

import os
import posixpath
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from jobs import FIRMWARE_DIR, list_firmware_files


DEFAULT_SWIM_FILE_PORT = 9000
DEFAULT_HTTP_PROFILE = "balanced"
HTTP_PROFILES = {
    # Original Proxmox/Catalyst workaround. Very reliable, very slow.
    "safe": {
        "chunk_bytes": 512,
        "chunk_delay_ms": 5.0,
        "accelerate_after_bytes": 0,
        "accelerated_chunk_bytes": 512,
        "accelerated_chunk_delay_ms": 5.0,
        "initial_delay_ms": 500.0,
        "tcp_maxseg": 536,
        "send_buffer_bytes": 4096,
        "tcp_notsent_lowat": 0,
    },
    # Faster default: gently start the switch client, then open up the stream.
    "balanced": {
        "chunk_bytes": 512,
        "chunk_delay_ms": 1.0,
        "accelerate_after_bytes": 64 * 1024,
        "accelerated_chunk_bytes": 64 * 1024,
        "accelerated_chunk_delay_ms": 0.0,
        "initial_delay_ms": 50.0,
        "tcp_maxseg": 1460,
        "send_buffer_bytes": 131072,
        "tcp_notsent_lowat": 32768,
    },
    # Lab-only option for clean networks and switches that tolerate faster bursts.
    "fast": {
        "chunk_bytes": 16384,
        "chunk_delay_ms": 0.0,
        "accelerate_after_bytes": 0,
        "accelerated_chunk_bytes": 16384,
        "accelerated_chunk_delay_ms": 0.0,
        "initial_delay_ms": 100.0,
        "tcp_maxseg": 1460,
        "send_buffer_bytes": 65536,
        "tcp_notsent_lowat": 16384,
    },
}

_server = None
_server_port: Optional[int] = None
_server_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress_callbacks = {}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_float_ms(name: str, default_ms: float, minimum_ms: float, maximum_ms: float) -> float:
    raw = os.environ.get(name, str(default_ms)).strip()
    try:
        value = float(raw)
    except ValueError:
        value = default_ms
    return max(minimum_ms, min(maximum_ms, value)) / 1000.0


def http_streaming_profile() -> dict:
    """Return the HTTP streaming profile used for IOS-XE pulls."""
    raw_profile = os.environ.get("SWIM_HTTP_PROFILE", DEFAULT_HTTP_PROFILE).strip().lower()
    profile_name = raw_profile if raw_profile in HTTP_PROFILES or raw_profile == "custom" else DEFAULT_HTTP_PROFILE
    defaults = HTTP_PROFILES.get(profile_name, HTTP_PROFILES[DEFAULT_HTTP_PROFILE])
    if profile_name == "custom":
        return {
            "profile": profile_name,
            "chunk_bytes": _env_int("SWIM_HTTP_CHUNK_BYTES", defaults["chunk_bytes"], 256, 1024 * 1024),
            "chunk_delay_ms": _env_float_ms("SWIM_HTTP_CHUNK_DELAY_MS", defaults["chunk_delay_ms"], 0.0, 1000.0) * 1000,
            "accelerate_after_bytes": _env_int(
                "SWIM_HTTP_ACCELERATE_AFTER_BYTES",
                defaults["accelerate_after_bytes"],
                0,
                1024 * 1024 * 1024,
            ),
            "accelerated_chunk_bytes": _env_int(
                "SWIM_HTTP_ACCELERATED_CHUNK_BYTES",
                defaults["accelerated_chunk_bytes"],
                256,
                1024 * 1024,
            ),
            "accelerated_chunk_delay_ms": _env_float_ms(
                "SWIM_HTTP_ACCELERATED_CHUNK_DELAY_MS",
                defaults["accelerated_chunk_delay_ms"],
                0.0,
                1000.0,
            ) * 1000,
            "initial_delay_ms": _env_float_ms("SWIM_HTTP_INITIAL_DELAY_MS", defaults["initial_delay_ms"], 0.0, 5000.0) * 1000,
            "tcp_maxseg": _env_int("SWIM_HTTP_TCP_MAXSEG", defaults["tcp_maxseg"], 536, 8960),
            "send_buffer_bytes": _env_int("SWIM_HTTP_SNDBUF_BYTES", defaults["send_buffer_bytes"], 2048, 1024 * 1024),
            "tcp_notsent_lowat": _env_int("SWIM_HTTP_TCP_NOTSENT_LOWAT", defaults["tcp_notsent_lowat"], 0, 1024 * 1024),
        }
    return {
        "profile": profile_name,
        **defaults,
    }


def register_http_progress(filename: str, callback: Callable[[int, int, str, str], None]):
    """Register a callback for bytes sent by the SWIM HTTP file server."""
    safe_name = os.path.basename(filename)
    token = object()
    with _progress_lock:
        callbacks = _progress_callbacks.setdefault(safe_name, {})
        callbacks[token] = callback
    return safe_name, token


def unregister_http_progress(registration):
    safe_name, token = registration
    with _progress_lock:
        callbacks = _progress_callbacks.get(safe_name)
        if not callbacks:
            return
        callbacks.pop(token, None)
        if not callbacks:
            _progress_callbacks.pop(safe_name, None)


def _emit_progress(filename: str, position: int, total: int, event: str, detail: str = ""):
    safe_name = os.path.basename(filename)
    with _progress_lock:
        callbacks = list(_progress_callbacks.get(safe_name, {}).values())
    for callback in callbacks:
        try:
            callback(position, total, event, detail)
        except Exception:
            pass


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        profile = http_streaming_profile()
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, profile["send_buffer_bytes"])
        except OSError:
            pass
        if hasattr(socket, "TCP_MAXSEG"):
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, profile["tcp_maxseg"])
            except OSError:
                pass
        if hasattr(socket, "TCP_NOTSENT_LOWAT") and profile["tcp_notsent_lowat"] > 0:
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NOTSENT_LOWAT, profile["tcp_notsent_lowat"])
            except OSError:
                pass
        super().server_bind()


class _SwimFirmwareHandler(BaseHTTPRequestHandler):
    server_version = "SWIMFirmwareHTTP/1.0"
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        profile = http_streaming_profile()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, profile["send_buffer_bytes"])
        except OSError:
            pass
        if hasattr(socket, "TCP_MAXSEG"):
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, profile["tcp_maxseg"])
            except OSError:
                pass
        if hasattr(socket, "TCP_NOTSENT_LOWAT") and profile["tcp_notsent_lowat"] > 0:
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NOTSENT_LOWAT, profile["tcp_notsent_lowat"])
            except OSError:
                pass

    def log_message(self, _fmt, *_args):
        # Keep switch download chatter out of the uvicorn console.
        return

    def do_HEAD(self):
        self._serve_file(head_only=True)

    def do_GET(self):
        self._serve_file(head_only=False)

    def _send_plain_error(self, status: HTTPStatus, message: str):
        body = (message + "\n").encode("utf-8")
        self.send_response(status.value, status.phrase)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _resolve_firmware_path(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = urllib.parse.unquote(parsed.path or "")
        clean_path = posixpath.normpath(raw_path)
        filename = clean_path.lstrip("/")

        if "/" in filename or not filename:
            return None, "Firmware file not found"
        if filename != os.path.basename(filename):
            return None, "Firmware file not found"
        if filename not in list_firmware_files():
            return None, "Firmware file not found"

        path = os.path.join(FIRMWARE_DIR, filename)
        if not os.path.isfile(path):
            return None, "Firmware file not found"
        return path, None

    def _serve_file(self, head_only: bool):
        path, error = self._resolve_firmware_path()
        if error:
            self._send_plain_error(HTTPStatus.NOT_FOUND, error)
            return

        size = os.path.getsize(path)
        filename = os.path.basename(path)
        range_header = self.headers.get("Range")
        byte_range = self._parse_range(range_header, size)
        if range_header and byte_range is None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.value)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            _emit_progress(filename, 0, size, "error", f"unsatisfied range {range_header}")
            return

        start, end = byte_range if byte_range else (0, size - 1)
        length = max(0, end - start + 1)
        status = HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK
        detail = f"{self.command} {start}-{end}/{size}" if byte_range else f"{self.command} full/{size}"
        _emit_progress(filename, start, size, "request", detail)

        self.send_response(status.value, status.phrase)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        if head_only:
            return

        profile = http_streaming_profile()
        base_chunk_bytes = profile["chunk_bytes"]
        base_chunk_delay = profile["chunk_delay_ms"] / 1000
        accelerate_after = profile["accelerate_after_bytes"]
        fast_chunk_bytes = profile["accelerated_chunk_bytes"]
        fast_chunk_delay = profile["accelerated_chunk_delay_ms"] / 1000
        initial_delay = profile["initial_delay_ms"] / 1000
        _emit_progress(
            filename,
            start,
            size,
            "profile",
            (
                f"chunk={base_chunk_bytes}B delay={profile['chunk_delay_ms']:.1f}ms "
                f"accelerate_after={accelerate_after}B "
                f"fast_chunk={fast_chunk_bytes}B "
                f"fast_delay={profile['accelerated_chunk_delay_ms']:.1f}ms "
                f"initial_delay={profile['initial_delay_ms']:.1f}ms "
                f"tcp_maxseg={profile['tcp_maxseg']} sndbuf={profile['send_buffer_bytes']}B "
                f"notsent_lowat={profile['tcp_notsent_lowat']}B "
                f"profile={profile['profile']}"
            ),
        )
        if initial_delay > 0:
            time.sleep(initial_delay)

        sent = 0
        last_emit = 0.0
        with open(path, "rb") as fh:
            fh.seek(start)
            while sent < length:
                accelerated = accelerate_after > 0 and sent >= accelerate_after
                chunk_bytes = fast_chunk_bytes if accelerated else base_chunk_bytes
                chunk_delay = fast_chunk_delay if accelerated else base_chunk_delay
                chunk = fh.read(min(chunk_bytes, length - sent))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    _emit_progress(filename, start + sent, size, "closed", detail)
                    return
                sent += len(chunk)
                now = time.time()
                if now - last_emit >= 1.0 or sent >= length:
                    last_emit = now
                    _emit_progress(filename, start + sent, size, "progress", detail)
                if sent == accelerate_after and fast_chunk_delay < base_chunk_delay:
                    _emit_progress(
                        filename,
                        start + sent,
                        size,
                        "profile",
                        f"accelerated HTTP stream after {accelerate_after} bytes",
                    )
                if chunk_delay > 0:
                    time.sleep(chunk_delay)
        event = "complete" if sent >= length else "closed"
        _emit_progress(filename, start + sent, size, event, detail)

    @staticmethod
    def _parse_range(range_header: Optional[str], size: int):
        if not range_header:
            return None
        value = range_header.strip()
        if not value.lower().startswith("bytes=") or "," in value:
            return None
        spec = value.split("=", 1)[1].strip()
        if "-" not in spec:
            return None
        start_s, end_s = spec.split("-", 1)
        try:
            if start_s == "":
                suffix_len = int(end_s)
                if suffix_len <= 0:
                    return None
                start = max(0, size - suffix_len)
                end = size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
        except ValueError:
            return None
        if start < 0 or end < start or start >= size:
            return None
        return start, min(end, size - 1)


def _configured_port() -> int:
    raw = os.environ.get("SWIM_FILE_PORT", str(DEFAULT_SWIM_FILE_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid SWIM_FILE_PORT value: {raw}") from e
    if port < 1 or port > 65535:
        raise RuntimeError(f"SWIM_FILE_PORT must be between 1 and 65535: {port}")
    return port


def ensure_swim_file_server(on_log=None) -> int:
    """Start the dedicated firmware file server once and return its port."""
    global _server, _server_port
    with _server_lock:
        if _server is not None and _server_port is not None:
            return _server_port

        port = _configured_port()
        try:
            server = _ReusableThreadingHTTPServer(("0.0.0.0", port), _SwimFirmwareHandler)
        except OSError as e:
            raise RuntimeError(
                f"Could not start dedicated SWIM file server on TCP/{port}: {e}"
            ) from e

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _server = server
        _server_port = port
        if on_log:
            on_log(f"Dedicated SWIM file server listening on TCP/{port}")
            profile = http_streaming_profile()
            on_log(
                "HTTP stream profile: "
                f"{profile['profile']} / {profile['chunk_bytes']}B chunks, "
                f"{profile['chunk_delay_ms']:.1f}ms delay, "
                f"accelerates after {profile['accelerate_after_bytes']}B to "
                f"{profile['accelerated_chunk_bytes']}B chunks / "
                f"{profile['accelerated_chunk_delay_ms']:.1f}ms delay, "
                f"{profile['initial_delay_ms']:.1f}ms initial pause, "
                f"TCP_MAXSEG {profile['tcp_maxseg']}, "
                f"SO_SNDBUF {profile['send_buffer_bytes']}B, "
                f"TCP_NOTSENT_LOWAT {profile['tcp_notsent_lowat']}B"
            )
        return port


def swim_file_url(app_base_url: str, filename: str, on_log=None) -> str:
    """Return a switch-reachable URL for a downloaded firmware image."""
    port = ensure_swim_file_server(on_log=on_log)
    parsed = urllib.parse.urlparse(app_base_url)
    host = (os.environ.get("SWIM_FILE_HOST") or parsed.hostname or "").strip()
    if not host:
        raise RuntimeError("Could not determine app host for SWIM file URL")

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    safe_name = os.path.basename(filename)
    return f"http://{host}:{port}/{urllib.parse.quote(safe_name)}"
