"""Background firmware download tracking for the web UI."""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html import unescape
from typing import Dict, List, Optional

from jobs import FIRMWARE_DIR, list_firmware_files


CHUNK_SIZE = 1024 * 1024
DEFAULT_FIRMWARE_BASE_URL = "http://firmware.shelton.digital:8999"
DEFAULT_FIRMWARE_FILENAMES = (
    "cat9k_iosxe.17.12.04.SPA.bin",
    "cat9k_iosxe.17.15.04d.SPA.bin",
    "cat9k_iosxe.17.18.02.SPA.bin",
)
FIRMWARE_LINK_RE = re.compile(r'href=["\']?([^"\'\s>]+)', re.IGNORECASE)
CAT9K_BIN_RE = re.compile(r"cat9k[^\"'<>\s/]*\.bin", re.IGNORECASE)


@dataclass
class FirmwareDownload:
    url: str
    filename: str
    status: str = "pending"
    progress: int = 0
    bytes_downloaded: int = 0
    total_bytes: Optional[int] = None
    message: str = ""
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class FirmwareDownloadManager:
    def __init__(self):
        self._downloads: Dict[str, FirmwareDownload] = {}
        self._lock = threading.Lock()
        self._started = False
        self._worker: Optional[threading.Thread] = None

    def start_from_env(self):
        raw = os.environ.get("FIRMWARE_URLS", "")
        if self._downloads_disabled():
            with self._lock:
                self._started = True
            return
        configured = [self._normalize_configured_url(url) for url in raw.split() if url.strip()]
        discovery_bases = self._repository_bases_from_configured(configured)
        discover_repositories = not configured or bool(discovery_bases)
        if not configured:
            discovery_bases = [DEFAULT_FIRMWARE_BASE_URL]
        with self._lock:
            if self._started:
                return
            self._started = True
            if discover_repositories:
                self._queue_discovery_locked(discovery_bases)
            if not discover_repositories:
                self._queue_urls_locked(configured)
        if not discover_repositories:
            self._ensure_worker()
        else:
            self._ensure_discovery_worker(discovery_bases)

    @staticmethod
    def _downloads_disabled() -> bool:
        return os.environ.get("FIRMWARE_URLS", "").strip().lower() in {
            "none", "off", "false", "disabled", "skip"
        }

    def _record_discovery_failure(self, error: str, base_urls: Optional[List[str]] = None):
        with self._lock:
            self._started = True
            self._downloads["__discovery__"] = FirmwareDownload(
                url=", ".join(base_urls or [DEFAULT_FIRMWARE_BASE_URL]),
                filename="Firmware discovery",
                status="failed",
                message="Discovery failed",
                error=error,
                finished_at=time.time(),
            )

    def rescan(self):
        raw = os.environ.get("FIRMWARE_URLS", "")
        if self._downloads_disabled():
            return self.serialize()
        configured = [self._normalize_configured_url(url) for url in raw.split() if url.strip()]
        discovery_bases = self._repository_bases_from_configured(configured)
        discover_repositories = not configured or bool(discovery_bases)
        if not configured:
            discovery_bases = [DEFAULT_FIRMWARE_BASE_URL]
        with self._lock:
            self._recover_stopped_worker_locked()
            self._started = True
            if not discover_repositories:
                self._queue_urls_locked(configured)
            else:
                current = self._downloads.get("__discovery__")
                if current and current.status == "downloading":
                    return self.serialize()
                self._queue_discovery_locked(discovery_bases)
        if not discover_repositories:
            self._ensure_worker()
        else:
            self._ensure_discovery_worker(discovery_bases)
        return self.serialize()

    def _discover_defaults_and_download(self, base_urls: List[str]):
        try:
            urls = self._discover_repository_urls(base_urls)
        except Exception as e:
            self._record_discovery_failure(
                f"Could not discover cat9k*.bin firmware files: {e}",
                base_urls,
            )
            return
        if not urls:
            self._record_discovery_failure(
                f"No cat9k*.bin firmware files found at {', '.join(base_urls)}",
                base_urls,
            )
            return
        with self._lock:
            self._downloads.pop("__discovery__", None)
            self._queue_urls_locked(urls)
        self._download_all()

    @staticmethod
    def _configured_urls() -> List[str]:
        raw = os.environ.get("FIRMWARE_URLS", "")
        if raw.strip().lower() in {"none", "off", "false", "disabled", "skip"}:
            return []
        configured = [FirmwareDownloadManager._normalize_configured_url(url) for url in raw.split() if url.strip()]
        discovery_bases = FirmwareDownloadManager._repository_bases_from_configured(configured)
        if configured and not discovery_bases:
            return configured
        return FirmwareDownloadManager._discover_repository_urls(
            discovery_bases or [DEFAULT_FIRMWARE_BASE_URL]
        )

    @staticmethod
    def _repository_bases_from_configured(urls: List[str]) -> List[str]:
        if not urls:
            return []
        bases = []
        for url in urls:
            filename = FirmwareDownloadManager._filename_from_url(url)
            if filename and not FirmwareDownloadManager._is_cat9k_bin(filename):
                return []
            base_url = FirmwareDownloadManager._repository_base_from_url(url)
            if base_url not in bases:
                bases.append(base_url)
        return bases

    @staticmethod
    def _discover_repository_urls(base_urls: List[str]) -> List[str]:
        urls = set()
        errors = []
        for base_url in base_urls:
            index_url = base_url.rstrip("/") + "/"
            request = urllib.request.Request(
                index_url,
                headers={"User-Agent": "Meraki-Catalyst-Onboarder/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    html = unescape(response.read().decode("utf-8", errors="replace"))
            except Exception as e:
                errors.append(f"{index_url}: {e}")
                urls.update(FirmwareDownloadManager._default_fallback_urls(base_url))
                continue

            for href in FIRMWARE_LINK_RE.findall(html):
                filename = FirmwareDownloadManager._filename_from_url(href)
                if FirmwareDownloadManager._is_cat9k_bin(filename):
                    urls.add(urllib.parse.urljoin(index_url, href))

            # Some index pages expose filenames in text or query strings rather
            # than direct hrefs. Queue any visible cat9k*.bin filename.
            for filename in CAT9K_BIN_RE.findall(html):
                urls.add(urllib.parse.urljoin(index_url, filename))

            # Some simple index pages are plaintext rather than HTML.
            for token in re.split(r"\s+", html):
                filename = FirmwareDownloadManager._filename_from_url(token)
                if FirmwareDownloadManager._is_cat9k_bin(filename):
                    urls.add(urllib.parse.urljoin(index_url, token))

            urls.update(FirmwareDownloadManager._default_fallback_urls(base_url, urls))

        if not urls and errors:
            raise RuntimeError("; ".join(errors))

        return sorted(urls, key=lambda url: FirmwareDownloadManager._filename_from_url(url).lower())

    def _queue_discovery_locked(self, base_urls: List[str]):
        self._downloads["__discovery__"] = FirmwareDownload(
            url=", ".join(base_urls),
            filename="Firmware discovery",
            status="downloading",
            message="Discovering cat9k*.bin firmware",
            started_at=time.time(),
        )

    def _queue_urls_locked(self, urls: List[str]):
        for url in urls:
            filename = self._filename_from_url(url)
            key = filename or url
            existing = self._downloads.get(key)
            if existing and existing.status in {"pending", "downloading", "cached", "done"}:
                continue
            self._downloads[key] = FirmwareDownload(
                url=url,
                filename=filename or "(unknown file)",
                message="Queued",
            )

    def serialize(self):
        with self._lock:
            self._recover_stopped_worker_locked()
            downloads = [asdict(d) for d in self._downloads.values()]
        active = any(d["status"] in {"pending", "downloading"} for d in downloads)
        return {
            "active": active,
            "downloads": downloads,
            "images": list_firmware_files(),
        }

    def retry(self, filename: Optional[str] = None, force: bool = False):
        retry_discovery = False
        with self._lock:
            self._recover_stopped_worker_locked()
            matched = False
            for key, download in list(self._downloads.items()):
                if filename and filename not in {key, download.filename}:
                    continue
                matched = True
                if key == "__discovery__":
                    if download.status == "downloading":
                        continue
                    retry_discovery = True
                    del self._downloads[key]
                    self._started = False
                    continue
                if download.status == "downloading":
                    continue
                if force:
                    self._remove_firmware_files(download.filename)
                elif download.status not in {"failed", "pending"}:
                    continue
                download.status = "pending"
                download.progress = 0
                download.bytes_downloaded = 0
                download.total_bytes = None
                download.message = "Queued"
                download.error = None
                download.started_at = None
                download.finished_at = None
            if filename and not matched:
                raise ValueError(f"Firmware download not found: {filename}")
        if retry_discovery:
            self.start_from_env()
            return self.serialize()
        self._ensure_worker()
        return self.serialize()

    def _ensure_worker(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._recover_stopped_worker_locked()
            self._worker = threading.Thread(target=self._download_all, name="firmware-downloads", daemon=True)
            self._worker.start()

    def _ensure_discovery_worker(self, base_urls: List[str]):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._discover_defaults_and_download,
                args=(base_urls,),
                name="firmware-discovery",
                daemon=True,
            )
            self._worker.start()

    def _download_all(self):
        os.makedirs(FIRMWARE_DIR, exist_ok=True)
        while True:
            with self._lock:
                next_item = next(
                    ((key, d.url) for key, d in self._downloads.items() if d.status == "pending"),
                    None,
                )
            if not next_item:
                return
            key, url = next_item
            self._download_one(key, url)

    def _download_one(self, key: str, url: str):
        filename = self._filename_from_url(url)
        if not filename:
            self._mark_failed(key, "Could not determine a filename from the firmware URL")
            return

        dest = os.path.join(FIRMWARE_DIR, filename)
        part = dest + ".part"
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            size = os.path.getsize(dest)
            with self._lock:
                d = self._downloads[key]
                d.filename = filename
                d.status = "cached"
                d.progress = 100
                d.bytes_downloaded = size
                d.total_bytes = size
                d.message = "Already downloaded"
                d.finished_at = time.time()
            return

        with self._lock:
            d = self._downloads[key]
            d.filename = filename
            d.status = "downloading"
            d.progress = 0
            d.message = "Starting download"
            d.started_at = time.time()

        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Meraki-Catalyst-Onboarder/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                total = self._content_length(response)
                with self._lock:
                    d = self._downloads[key]
                    d.total_bytes = total
                    d.message = "Downloading"
                downloaded = 0
                with open(part, "wb") as fh:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        fh.write(chunk)
                        downloaded += len(chunk)
                        progress = int(downloaded / total * 100) if total else 0
                        with self._lock:
                            d = self._downloads[key]
                            d.bytes_downloaded = downloaded
                            d.progress = min(progress, 99) if total else 0
                            d.message = "Downloading"
            os.replace(part, dest)
            size = os.path.getsize(dest)
            with self._lock:
                d = self._downloads[key]
                d.status = "done"
                d.progress = 100
                d.bytes_downloaded = size
                d.total_bytes = d.total_bytes or size
                d.message = "Download complete"
                d.finished_at = time.time()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as e:
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
            self._mark_failed(key, str(e))
        except Exception as e:
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
            self._mark_failed(key, str(e))

    def _mark_failed(self, key: str, error: str):
        with self._lock:
            d = self._downloads[key]
            d.status = "failed"
            d.message = "Download failed"
            d.error = error
            d.finished_at = time.time()

    def _recover_stopped_worker_locked(self):
        if self._worker and self._worker.is_alive():
            return
        now = time.time()
        for download in self._downloads.values():
            if download.status != "downloading":
                continue
            download.status = "failed"
            download.message = "Download interrupted"
            download.error = "Download stopped before completing. Use Retry or Rescan to start it again."
            download.finished_at = now

    @staticmethod
    def _default_fallback_urls(base_url: str, discovered: Optional[set] = None) -> set:
        if FirmwareDownloadManager._canonical_base(base_url) != FirmwareDownloadManager._canonical_base(DEFAULT_FIRMWARE_BASE_URL):
            return set()
        discovered_names = {
            FirmwareDownloadManager._filename_from_url(url).lower()
            for url in (discovered or set())
        }
        index_url = DEFAULT_FIRMWARE_BASE_URL.rstrip("/") + "/"
        return {
            urllib.parse.urljoin(index_url, filename)
            for filename in DEFAULT_FIRMWARE_FILENAMES
            if filename.lower() not in discovered_names
        }

    @staticmethod
    def _canonical_base(url: str) -> str:
        parsed = urllib.parse.urlparse(FirmwareDownloadManager._normalize_configured_url(url))
        path = parsed.path.rstrip("/")
        return urllib.parse.urlunparse((
            parsed.scheme or "http",
            parsed.netloc.lower(),
            path,
            "",
            "",
            "",
        ))

    @staticmethod
    def _remove_firmware_files(filename: str):
        if not filename or filename in {".", "..", "(unknown file)"}:
            return
        for suffix in ("", ".part"):
            path = os.path.join(FIRMWARE_DIR, filename + suffix)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _normalize_configured_url(url: str) -> str:
        url = url.strip()
        if "://" in url:
            return url
        if url.startswith("//"):
            return "http:" + url
        return "http://" + url

    @staticmethod
    def _repository_base_from_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        filename = FirmwareDownloadManager._filename_from_url(url)
        if not filename:
            return url.rstrip("/")
        base_path = parsed.path.rsplit("/", 1)[0]
        if base_path and not base_path.endswith("/"):
            base_path += "/"
        return urllib.parse.urlunparse((
            parsed.scheme or "http",
            parsed.netloc,
            base_path or "/",
            "",
            "",
            "",
        )).rstrip("/")

    @staticmethod
    def _is_cat9k_bin(filename: str) -> bool:
        lower = filename.lower()
        return lower.startswith("cat9k") and lower.endswith(".bin")

    @staticmethod
    def _filename_from_url(url: str) -> str:
        path = urllib.parse.urlparse(url).path
        filename = os.path.basename(urllib.parse.unquote(path))
        return filename if filename and filename not in {".", ".."} else ""

    @staticmethod
    def _content_length(response) -> Optional[int]:
        value = response.headers.get("Content-Length")
        try:
            return int(value) if value else None
        except ValueError:
            return None


firmware_downloads = FirmwareDownloadManager()
