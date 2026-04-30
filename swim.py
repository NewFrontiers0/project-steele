"""Single-switch firmware management (SWIM) jobs."""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from jobs import (FIRMWARE_DIR, firmware_version_label, firmware_version_tuple,
                  list_firmware_files)
from switch import SwitchClient, SwitchError, parse_version_tuple, wait_for_reload
from swim_ftp_server import register_ftp_progress, swim_ftp_url, unregister_ftp_progress
from swim_file_server import register_http_progress, swim_file_url, unregister_http_progress
from swim_tftp_server import register_tftp_progress, swim_tftp_url, unregister_tftp_progress


def _redact_url_password(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.password is None:
        return url
    username = urllib.parse.quote(urllib.parse.unquote(parsed.username or ""), safe="")
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{username}:***@{host}" if username else host
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _format_bytes(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024 * 1024):.1f} GB"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} bytes"


@dataclass
class SwimJob:
    id: str
    host: str
    image_filename: str
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    progress_message: str = ""
    version: Optional[str] = None
    error: Optional[str] = None
    log: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class SwimStore:
    def __init__(self):
        self._jobs: Dict[str, SwimJob] = {}
        self._lock = threading.Lock()

    def start_upgrade(self, host: str, username: str, password: str,
                      secret: Optional[str], image_filename: str,
                      app_base_url: Optional[str] = None,
                      copy_vrf: Optional[str] = None,
                      source_interface: Optional[str] = None,
                      transfer_method: str = "install-http",
                      repair_flash: bool = True,
                      allow_downgrade: bool = False) -> SwimJob:
        if image_filename not in list_firmware_files():
            raise ValueError("Image not in firmware folder")
        job = SwimJob(
            id=str(uuid.uuid4())[:8],
            host=host.strip(),
            image_filename=image_filename,
        )
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(
            target=self._run_upgrade,
            args=(job.id, username, password, secret, app_base_url,
                  copy_vrf, source_interface, transfer_method, repair_flash,
                  allow_downgrade),
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> Optional[SwimJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def serialize(self, job: SwimJob) -> dict:
        with self._lock:
            return asdict(job)

    def _set_progress(self, job_id: str, pct: int, message: str):
        with self._lock:
            job = self._jobs[job_id]
            job.progress = max(job.progress, max(0, min(100, int(pct))))
            job.progress_message = message

    def _set_stage(self, job_id: str, stage: str, message: Optional[str] = None):
        with self._lock:
            job = self._jobs[job_id]
            job.stage = stage
            if message is not None:
                job.progress_message = message

    def _log(self, job_id: str, line: str):
        with self._lock:
            job = self._jobs[job_id]
            ts = time.strftime("%H:%M:%S")
            job.log.append(f"[{ts}] {line}")
            if len(job.log) > 500:
                job.log = job.log[-500:]

    def _fail(self, job_id: str, error: str):
        with self._lock:
            job = self._jobs[job_id]
            job.status = "failed"
            job.stage = "failed"
            job.error = error
            job.progress_message = "Upgrade failed"
            job.finished_at = time.time()
        self._log(job_id, f"ERROR: {error}")

    def _run_upgrade(self, job_id: str, username: str, password: str,
                     secret: Optional[str], app_base_url: Optional[str],
                     copy_vrf: Optional[str],
                     source_interface: Optional[str],
                     transfer_method: str,
                     repair_flash: bool,
                     allow_downgrade: bool):
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.stage = "preparing"
            job.started_at = time.time()
            host = job.host
            image_filename = job.image_filename

        try:
            local_path = os.path.join(FIRMWARE_DIR, image_filename)
            if not os.path.isfile(local_path):
                raise SwitchError(f"Image not in {FIRMWARE_DIR}: {image_filename}")

            size_bytes = os.path.getsize(local_path)
            self._log(job_id, f"Starting SWIM upgrade to {image_filename} ({size_bytes // (1024 * 1024)} MB)")

            self._set_stage(job_id, "hashing", "Computing local MD5")
            self._set_progress(job_id, 2, "Computing local MD5")
            h = hashlib.md5()
            with open(local_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            local_md5 = h.hexdigest()
            self._log(job_id, f"Local MD5 = {local_md5}")

            self._set_stage(job_id, "copying", "Opening SSH session")
            self._set_progress(job_id, 5, "Opening SSH session")
            self._log(job_id, f"Opening SSH session to {host}")
            with SwitchClient(host, username, password, secret) as sw:
                self._log(job_id, "SSH connected, entered enable mode")

                self._set_progress(job_id, 6, "Checking current switch version")
                current_version = sw.get_version()
                target_version = firmware_version_tuple(image_filename)
                target_label = firmware_version_label(image_filename) or image_filename
                self._log(job_id, f"Current switch version = {current_version}")
                self._log(job_id, f"Selected firmware version = {target_label}")
                if target_version:
                    current_tuple = parse_version_tuple(current_version)
                    if target_version[:3] <= current_tuple[:3]:
                        if not allow_downgrade:
                            raise SwitchError(
                                f"Selected image {image_filename} is version {target_label}, "
                                f"but the switch is already running {current_version}. "
                                "Enable 'Allow downgrade or reinstall' to run this intentionally."
                            )
                        self._log(
                            job_id,
                            "Downgrade/reinstall allowed by user; continuing with IOS-XE install workflow",
                        )

                if repair_flash:
                    self._set_stage(job_id, "repairing_flash", "Checking flash filesystem")
                    self._set_progress(job_id, 7, "Checking flash filesystem")
                    self._log(job_id, "Running: fsck flash:")
                    fsck_out = sw.fsck_flash()
                    self._log(job_id, f"fsck output:\n{fsck_out[-1200:]}")
                    self._set_progress(job_id, 9, "Flash filesystem check complete")

                def copy_progress(pct, message):
                    self._set_progress(job_id, 5 + int(pct * 0.55), message)

                def switch_log(line):
                    self._log(job_id, f"  {line}")

                method = (transfer_method or "install-http").strip().lower()
                install_started_directly = False
                if method == "install-http":
                    if not app_base_url:
                        raise SwitchError("Direct HTTP install needs the browser app URL")
                    firmware_url = swim_file_url(app_base_url, image_filename, on_log=switch_log)
                    self._log(job_id, f"Using direct install HTTP image source: {firmware_url}")
                    self._log(job_id, "Switch install subsystem will pull and install the remote image directly")
                    http_state = {
                        "last_pct": -1,
                        "last_log": 0.0,
                        "started": False,
                        "complete": False,
                    }

                    def http_progress(position, total, event, detail):
                        total = total or size_bytes
                        pct = int((position / total) * 100) if total else 0
                        pct = max(0, min(100, pct))
                        position_label = _format_bytes(position)
                        total_label = _format_bytes(total)
                        if event == "request":
                            self._log(job_id, f"  HTTP file request: {detail}")
                        if event == "profile":
                            self._log(job_id, f"  HTTP serving profile: {detail}")
                        if event == "progress":
                            http_state["started"] = True
                            now = time.time()
                            if pct != http_state["last_pct"] and (
                                now - http_state["last_log"] >= 10.0
                                or pct in (0, 25, 50, 75, 100)
                            ):
                                http_state["last_pct"] = pct
                                http_state["last_log"] = now
                                self._log(
                                    job_id,
                                    f"  HTTP server sent {position_label}/{total_label} ({pct}%)",
                                )
                        elif event == "complete" and not http_state["complete"]:
                            http_state["complete"] = True
                            self._log(job_id, f"  HTTP file request complete: {detail}")
                        elif event == "closed":
                            self._log(job_id, f"  HTTP client closed request at {position_label}: {detail}")
                        elif event == "error":
                            self._log(job_id, f"  HTTP file request error: {detail}")

                    self._set_stage(job_id, "installing", "Preparing direct remote install")
                    self._set_progress(job_id, 12, "Preparing direct remote install")
                    self._log(job_id, "Running: show install summary")
                    pre_summary = sw.conn.send_command("show install summary", read_timeout=30)
                    self._log(job_id, f"Pre-install summary:\n{pre_summary}")
                    if re.search(r"^IMG\s+I\s+", pre_summary, re.MULTILINE):
                        self._log(job_id, "Found inactive package(s), running install remove inactive")
                        remove_out = sw.install_remove_inactive()
                        self._log(job_id, f"Remove inactive output: {remove_out[-500:]}")
                    else:
                        self._log(job_id, "No inactive packages to remove")

                    self._set_progress(job_id, 16, "Starting install add")
                    self._log(job_id, f"Running: install add file {firmware_url}")
                    http_registration = register_http_progress(image_filename, http_progress)
                    try:
                        add_out = sw.install_add_remote_watch(
                            firmware_url,
                            source_interface=source_interface,
                            on_log=switch_log,
                            on_progress=lambda pct, message: self._set_progress(job_id, pct, message),
                        )
                        self._log(job_id, add_out)
                    except SwitchError as e:
                        self._log(job_id, "Collecting install diagnostics after add failure")
                        try:
                            diagnostics = sw.install_diagnostics()
                            self._log(job_id, f"Install diagnostics:\n{diagnostics[-4000:]}")
                        except Exception as diag_error:
                            self._log(job_id, f"Could not collect install diagnostics: {diag_error}")
                        raise SwitchError(f"Install add failed: {e}") from e
                    finally:
                        unregister_http_progress(http_registration)

                    self._set_progress(job_id, 58, "Install add complete")
                    self._log(job_id, "Running: show install summary")
                    post_add_summary = sw.conn.send_command("show install summary", read_timeout=30)
                    self._log(job_id, f"Post-add summary:\n{post_add_summary}")

                    self._set_progress(job_id, 60, "Starting install activate/commit")
                    self._log(job_id, "Running: install activate commit prompt-level none")
                    try:
                        activate_out = sw.install_activate_commit_watch(
                            on_log=switch_log,
                            on_progress=lambda pct, message: self._set_progress(job_id, pct, message),
                        )
                        self._log(job_id, activate_out)
                    except SwitchError as e:
                        self._log(job_id, "Collecting install diagnostics after activate failure")
                        try:
                            diagnostics = sw.install_diagnostics()
                            self._log(job_id, f"Install diagnostics:\n{diagnostics[-4000:]}")
                        except Exception as diag_error:
                            self._log(job_id, f"Could not collect install diagnostics: {diag_error}")
                        raise SwitchError(f"Install activate/commit failed: {e}") from e
                    install_started_directly = True
                elif method == "tftp":
                    if not app_base_url:
                        raise SwitchError("TFTP transfer needs the browser app URL")
                    firmware_url = swim_tftp_url(app_base_url, image_filename, on_log=switch_log)
                    self._log(job_id, f"Using TFTP image source: {firmware_url}")
                    self._log(job_id, "Switch will pull the image from the app TFTP server")
                    tftp_state = {
                        "last_pct": -1,
                        "last_log": 0.0,
                        "started": False,
                        "complete": False,
                    }

                    def tftp_progress(sent, total, event):
                        total = total or size_bytes
                        pct = int((sent / total) * 100) if total else 0
                        pct = max(0, min(100, pct))
                        sent_label = _format_bytes(sent)
                        total_label = _format_bytes(total)
                        if event == "start" and not tftp_state["started"]:
                            tftp_state["started"] = True
                            self._log(job_id, "  TFTP transfer started; switch is downloading the image")
                        if event == "complete" and not tftp_state["complete"]:
                            tftp_state["complete"] = True
                            self._log(job_id, f"  TFTP transfer complete: {sent_label}/{total_label} sent")
                        elif event == "closed" and not tftp_state["complete"]:
                            self._log(job_id, f"  TFTP transfer ended early: {sent_label}/{total_label} sent")
                        copy_progress(pct, f"TFTP {sent_label}/{total_label}")

                        now = time.time()
                        should_log = (
                            pct != tftp_state["last_pct"]
                            and (now - tftp_state["last_log"] >= 10.0 or pct in (0, 25, 50, 75, 100))
                        )
                        if should_log:
                            tftp_state["last_pct"] = pct
                            tftp_state["last_log"] = now
                            self._log(job_id, f"  TFTP server sent {sent_label}/{total_label} ({pct}%)")

                    progress_registration = register_tftp_progress(image_filename, tftp_progress)
                    try:
                        sw.copy_image_from_tftp_to_flash(
                            image_filename,
                            firmware_url,
                            expected_size=size_bytes,
                            copy_vrf=copy_vrf,
                            source_interface=source_interface,
                            on_progress=copy_progress,
                            on_log=switch_log,
                        )
                    finally:
                        unregister_tftp_progress(progress_registration)
                elif method in {"ftp", "ftp-active", "ftp-passive"}:
                    if not app_base_url:
                        raise SwitchError("FTP transfer needs the browser app URL")
                    ftp_passive = method == "ftp-passive"
                    ftp_mode = "passive" if ftp_passive else "active"
                    firmware_url = swim_ftp_url(app_base_url, image_filename, on_log=switch_log)
                    self._log(job_id, f"Using FTP image source: {_redact_url_password(firmware_url)}")
                    self._log(job_id, f"Switch will pull the image from the app FTP server using {ftp_mode} FTP")
                    ftp_state = {
                        "last_pct": -1,
                        "last_log": 0.0,
                        "last_sent": 0,
                        "last_update": time.time(),
                        "last_stall_log": 0.0,
                        "started": False,
                        "complete": False,
                        "stop": False,
                        "abort_sent": False,
                    }

                    def ftp_progress(sent, total, event):
                        total = total or size_bytes
                        pct = int((sent / total) * 100) if total else 0
                        pct = max(0, min(100, pct))
                        sent_label = _format_bytes(sent)
                        total_label = _format_bytes(total)
                        if sent != ftp_state["last_sent"] or event in ("start", "complete", "closed"):
                            ftp_state["last_sent"] = sent
                            ftp_state["last_update"] = time.time()
                        if event == "start" and not ftp_state["started"]:
                            ftp_state["started"] = True
                            self._log(job_id, "  FTP transfer started; switch is downloading the image")
                        if event == "complete" and not ftp_state["complete"]:
                            ftp_state["complete"] = True
                            self._log(job_id, f"  FTP transfer complete: {sent_label}/{total_label} sent")
                        elif event == "closed" and not ftp_state["complete"]:
                            ftp_state["complete"] = True
                            self._log(job_id, f"  FTP transfer ended early: {sent_label}/{total_label} sent")
                        copy_progress(pct, f"FTP {sent_label}/{total_label}")

                        now = time.time()
                        should_log = (
                            pct != ftp_state["last_pct"]
                            and (now - ftp_state["last_log"] >= 10.0 or pct in (0, 25, 50, 75, 100))
                        )
                        if should_log:
                            ftp_state["last_pct"] = pct
                            ftp_state["last_log"] = now
                            self._log(job_id, f"  FTP server sent {sent_label}/{total_label} ({pct}%)")

                    def ftp_watchdog():
                        passive_ports = os.environ.get("SWIM_FTP_PASSIVE_PORTS", "30000-30009")
                        ftp_port = os.environ.get("SWIM_FTP_PORT", "2121")
                        abort_after_raw = os.environ.get("SWIM_FTP_STALL_ABORT_SECONDS", "300")
                        try:
                            abort_after = max(60, int(abort_after_raw))
                        except ValueError:
                            abort_after = 300
                        if ftp_passive:
                            port_hint = f"TCP/{ftp_port} and passive FTP ports {passive_ports}"
                        else:
                            port_hint = (
                                f"TCP/{ftp_port}, plus app-host initiated active FTP data "
                                "connections back to the switch"
                            )
                        while not ftp_state["stop"]:
                            time.sleep(15)
                            if ftp_state["stop"] or ftp_state["complete"]:
                                break
                            if not ftp_state["started"]:
                                continue
                            idle = time.time() - ftp_state["last_update"]
                            if idle < 30:
                                continue
                            if time.time() - ftp_state["last_stall_log"] < 30:
                                continue
                            ftp_state["last_stall_log"] = time.time()
                            sent_label = _format_bytes(ftp_state["last_sent"])
                            total_label = _format_bytes(size_bytes)
                            pct = int((ftp_state["last_sent"] / size_bytes) * 100) if size_bytes else 0
                            copy_progress(pct, f"FTP stalled at {sent_label}/{total_label}")
                            self._log(
                                job_id,
                                "  FTP transfer has not advanced for "
                                f"{int(idle)}s ({sent_label}/{total_label} sent). "
                                f"Check {port_hint}.",
                            )
                            if idle >= abort_after and not ftp_state["abort_sent"]:
                                ftp_state["abort_sent"] = True
                                self._log(
                                    job_id,
                                    f"  Aborting stalled FTP transfer after {int(idle)}s with no progress",
                                )
                                try:
                                    sw.conn.write_channel("\x03")
                                except Exception as e:
                                    self._log(job_id, f"  WARN: could not abort FTP copy: {e}")

                    threading.Thread(target=ftp_watchdog, daemon=True).start()
                    progress_registration = register_ftp_progress(image_filename, ftp_progress)
                    try:
                        sw.copy_image_from_ftp_to_flash(
                            image_filename,
                            firmware_url,
                            expected_size=size_bytes,
                            copy_vrf=copy_vrf,
                            source_interface=source_interface,
                            on_progress=copy_progress,
                            on_log=switch_log,
                            passive=ftp_passive,
                        )
                    finally:
                        ftp_state["stop"] = True
                        unregister_ftp_progress(progress_registration)
                elif method == "http":
                    if not app_base_url:
                        raise SwitchError("HTTP transfer needs the browser app URL")
                    firmware_url = swim_file_url(app_base_url, image_filename, on_log=switch_log)
                    self._log(job_id, f"Using dedicated HTTP image source: {firmware_url}")
                    self._log(job_id, "Switch will pull the image from the SWIM file server instead of using SCP")
                    sw.copy_image_from_http_to_flash(
                        image_filename,
                        firmware_url,
                        expected_size=size_bytes,
                        copy_vrf=copy_vrf,
                        source_interface=source_interface,
                        on_progress=copy_progress,
                        on_log=switch_log,
                    )
                elif method == "scp":
                    self._log(job_id, "Using SCP transfer from app to switch")
                    sw.copy_image_to_flash(
                        local_path,
                        on_progress=copy_progress,
                        on_log=switch_log,
                        expected_md5=local_md5,
                    )
                else:
                    raise SwitchError(f"Unsupported SWIM transfer method: {transfer_method}")
                if install_started_directly:
                    self._set_progress(job_id, 68, "Direct install command sent")
                    self._log(job_id, "Direct install command sent; waiting for reload")
                else:
                    self._set_progress(job_id, 60, "Image on flash")
                    self._log(job_id, "Image transfer phase complete")

                    self._set_stage(job_id, "verifying", "Verifying MD5 on switch")
                    self._set_progress(job_id, 62, "Verifying MD5 on switch")
                    self._log(job_id, f"Running: verify /md5 flash:{image_filename}")
                    switch_md5 = sw.verify_image_on_flash(image_filename, expected_md5=local_md5)
                    self._log(job_id, f"Switch MD5 = {switch_md5}")
                    self._set_progress(job_id, 65, "MD5 match")

                    self._set_stage(job_id, "installing", "Checking install state")
                    self._log(job_id, "Running: show install summary")
                    pre_summary = sw.conn.send_command("show install summary", read_timeout=30)
                    self._log(job_id, f"Pre-install summary:\n{pre_summary}")
                    if re.search(r"^IMG\s+I\s+", pre_summary, re.MULTILINE):
                        self._log(job_id, "Found inactive package(s), running install remove inactive")
                        remove_out = sw.install_remove_inactive()
                        self._log(job_id, f"Remove inactive output: {remove_out[-500:]}")
                    else:
                        self._log(job_id, "No inactive packages to remove")

                    self._set_progress(job_id, 66, "Starting install add/activate/commit")
                    self._log(job_id, f"Running: install add file flash:{image_filename} activate commit prompt-level none")
                    fire_out = sw.install_add_activate_commit_fire_and_forget(
                        image_filename,
                        on_log=switch_log,
                    )
                    self._log(job_id, fire_out)

            self._set_stage(job_id, "reloading", "Install command sent, monitoring reload")
            self._set_progress(job_id, 68, "Install command sent, monitoring reload")
            self._log(job_id, "Monitoring switch reload with ping and TCP/22 probes")
            reload_last_log = {"going_down": -9999, "coming_back": -9999}

            def reload_tick(phase, elapsed, note):
                mins = elapsed // 60
                secs = elapsed % 60
                if phase == "going_down":
                    pct = min(75, 68 + int(elapsed / 7500 * 7))
                    self._set_progress(job_id, pct, f"Waiting for reload to start ({mins}m{secs}s) - {note}")
                else:
                    pct = min(92, 75 + int(elapsed / 1800 * 17))
                    self._set_progress(job_id, pct, f"Waiting for switch to return ({mins}m{secs}s)")
                if elapsed - reload_last_log.get(phase, -9999) >= 60:
                    reload_last_log[phase] = elapsed
                    self._log(job_id, f"[{phase}] {mins}m{secs}s: {note}")

            wait_for_reload(
                host,
                on_tick=reload_tick,
                going_down_timeout=7500,
                coming_back_timeout=3600,
            )
            self._log(job_id, "Switch is back up")

            self._set_stage(job_id, "committing", "Verifying version and committing install")
            self._set_progress(job_id, 92, "Verifying version")
            with SwitchClient(host, username, password, secret) as sw:
                new_version = sw.get_version()
                self._log(job_id, f"New version = {new_version}")
                self._set_progress(job_id, 96, "Committing install")
                self._log(job_id, "Running: install commit")
                commit_out = sw.conn.send_command_timing(
                    "install commit",
                    read_timeout=120,
                    strip_prompt=False,
                    strip_command=False,
                )
                self._log(job_id, f"Commit output: {commit_out[-500:]}")

            with self._lock:
                job = self._jobs[job_id]
                job.status = "done"
                job.stage = "done"
                job.version = new_version
                job.error = None
                job.progress = 100
                job.progress_message = "Upgrade complete"
                job.finished_at = time.time()
            self._log(job_id, f"SWIM upgrade complete ({new_version})")
        except SwitchError as e:
            self._fail(job_id, str(e))
        except Exception as e:
            self._fail(job_id, f"Unexpected error: {e}")


swim_store = SwimStore()
