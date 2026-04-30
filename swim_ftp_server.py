"""Read-only FTP file server for SWIM image pulls."""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
from typing import Callable, Optional

from jobs import FIRMWARE_DIR


DEFAULT_FTP_PORT = 2121
DEFAULT_PASSIVE_PORTS = "30000-30009"
DEFAULT_FTP_USER = "swim"
DEFAULT_FTP_PASSWORD = "swim"

_server = None
_server_port: Optional[int] = None
_server_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress_callbacks = {}


def _configured_port() -> int:
    raw = os.environ.get("SWIM_FTP_PORT", str(DEFAULT_FTP_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid SWIM_FTP_PORT value: {raw}") from e
    if port < 1 or port > 65535:
        raise RuntimeError(f"SWIM_FTP_PORT must be between 1 and 65535: {port}")
    return port


def _configured_passive_ports():
    raw = os.environ.get("SWIM_FTP_PASSIVE_PORTS", DEFAULT_PASSIVE_PORTS).strip()
    if not raw:
        return list(range(30000, 30010))
    if "-" in raw:
        start_s, end_s = raw.split("-", 1)
        start = int(start_s.strip())
        end = int(end_s.strip())
        if start > end:
            start, end = end, start
        return list(range(start, end + 1))
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _credentials():
    user = os.environ.get("SWIM_FTP_USER", DEFAULT_FTP_USER).strip() or DEFAULT_FTP_USER
    password = os.environ.get("SWIM_FTP_PASSWORD", DEFAULT_FTP_PASSWORD)
    return user, password


def register_ftp_progress(filename: str, callback: Callable[[int, int, str], None]):
    """Register a callback for bytes sent while this firmware file is retrieved."""
    safe_name = os.path.basename(filename)
    token = object()
    with _progress_lock:
        callbacks = _progress_callbacks.setdefault(safe_name, {})
        callbacks[token] = callback
    return safe_name, token


def unregister_ftp_progress(registration):
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


class _ProgressFile:
    def __init__(self, fh, filename: str):
        self._fh = fh
        self._filename = os.path.basename(filename)
        try:
            self._total = os.path.getsize(filename)
        except OSError:
            self._total = 0
        self._sent = 0
        self._last_emit = 0.0
        self._started = False
        self._closed = False

    def read(self, size=-1):
        data = self._fh.read(size)
        if data:
            if not self._started:
                self._started = True
                _emit_progress(self._filename, 0, self._total, "start")
            self._sent += len(data)
            now = time.time()
            if self._total and (now - self._last_emit >= 1.0 or self._sent >= self._total):
                self._last_emit = now
                _emit_progress(self._filename, self._sent, self._total, "progress")
        elif self._started:
            _emit_progress(self._filename, self._sent, self._total, self._finish_event())
        return data

    def close(self):
        if not self._closed:
            self._closed = True
            if self._started:
                _emit_progress(self._filename, self._sent, self._total, self._finish_event())
        return self._fh.close()

    def _finish_event(self):
        if self._total and self._sent >= self._total:
            return "complete"
        return "closed"

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *args):
        self.close()

    def __iter__(self):
        return iter(self._fh)

    def __getattr__(self, name):
        return getattr(self._fh, name)


def ensure_swim_ftp_server(masquerade_address: Optional[str] = None, on_log=None) -> int:
    """Start the read-only FTP server once and return its control port."""
    global _server, _server_port
    with _server_lock:
        if _server is not None and _server_port is not None:
            return _server_port

        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.filesystems import AbstractedFS
            from pyftpdlib.handlers import FTPHandler
            from pyftpdlib.servers import ThreadedFTPServer
        except ImportError as e:
            raise RuntimeError(
                "FTP transfer requires pyftpdlib. Run ./run.sh again so "
                "requirements.txt is installed, or rebuild the Docker image."
            ) from e

        os.makedirs(FIRMWARE_DIR, exist_ok=True)
        user, password = _credentials()
        authorizer = DummyAuthorizer()
        authorizer.add_user(user, password, FIRMWARE_DIR, perm="elr")

        class ProgressFS(AbstractedFS):
            def open(self, filename, mode):
                fh = super().open(filename, mode)
                if "r" in mode and "b" in mode:
                    return _ProgressFile(fh, filename)
                return fh

        handler = FTPHandler
        handler.authorizer = authorizer
        handler.abstracted_fs = ProgressFS
        handler.banner = "SWIM firmware FTP server ready"
        handler.passive_ports = _configured_passive_ports()
        handler.use_sendfile = False
        if masquerade_address:
            handler.masquerade_address = masquerade_address

        port = _configured_port()
        try:
            server = ThreadedFTPServer(("0.0.0.0", port), handler)
        except OSError as e:
            raise RuntimeError(
                f"Could not start SWIM FTP server on TCP/{port}: {e}"
            ) from e

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _server = server
        _server_port = port
        if on_log:
            passive = os.environ.get("SWIM_FTP_PASSIVE_PORTS", DEFAULT_PASSIVE_PORTS)
            on_log(f"SWIM FTP server listening on TCP/{port}, passive ports {passive}")
        return port


def swim_ftp_url(app_base_url: str, filename: str, on_log=None) -> str:
    """Return a switch-reachable FTP URL for a downloaded firmware image."""
    parsed = urllib.parse.urlparse(app_base_url)
    host = (os.environ.get("SWIM_FTP_HOST") or parsed.hostname or "").strip()
    if not host:
        raise RuntimeError("Could not determine app host for SWIM FTP URL")
    port = ensure_swim_ftp_server(masquerade_address=host, on_log=on_log)
    user, password = _credentials()

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    user_q = urllib.parse.quote(user, safe="")
    password_q = urllib.parse.quote(password, safe="")
    filename_q = urllib.parse.quote(os.path.basename(filename))
    return f"ftp://{user_q}:{password_q}@{host}:{port}/{filename_q}"
