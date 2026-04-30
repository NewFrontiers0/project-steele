"""Bulk runner with gated stages including firmware upgrade."""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Callable

from switch import (SwitchClient, SwitchError, is_version_supported,
                     wait_for_reachable, generate_device_mode_config)
from meraki_client import MerakiClient, MerakiError


MAX_PARALLEL = 5
FIRMWARE_DIR = os.environ.get("FIRMWARE_DIR", os.path.join(os.path.dirname(__file__), "firmware"))


@dataclass
class DeviceJob:
    host: str
    network_id: str
    status: str = "pending"
    # status flow:
    #  pending -> running -> precheck_done | needs_upgrade | failed
    #  needs_upgrade -> running -> precheck_done | failed   (after upgrade stage)
    #  precheck_done -> running -> enable_done | failed
    #  enable_done -> running -> claim_done | failed
    stage: str = "queued"
    version: Optional[str] = None
    hostname: Optional[str] = None
    compatible: Optional[bool] = None
    cloud_id: Optional[str] = None
    error: Optional[str] = None
    progress: int = 0
    progress_message: str = ""
    log: List[str] = field(default_factory=list)
    # Device-mode prerequisite check results (empty in cloud mode)
    prereqs: Optional[dict] = None
    prereq_config: Optional[list] = None   # list of (section, lines) tuples
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class Run:
    id: str
    created_at: float
    mode: str
    username: str
    password: str
    secret: Optional[str]
    api_key: str = field(repr=False)
    org_id: str
    jobs: List[DeviceJob] = field(default_factory=list)
    current_stage: str = "ready"
    stage_running: bool = False


def list_firmware_files() -> List[str]:
    if not os.path.isdir(FIRMWARE_DIR):
        return []
    files = [
        f for f in os.listdir(FIRMWARE_DIR)
        if f.lower().startswith("cat9k") and f.lower().endswith(".bin")
    ]
    return sorted(files, key=_firmware_sort_key, reverse=True)


FIRMWARE_VERSION_RE = re.compile(
    r"cat9k.*?(\d+)\.(\d+)\.(\d+)([a-zA-Z]*)",
    re.IGNORECASE,
)


def firmware_version_tuple(filename: str):
    match = FIRMWARE_VERSION_RE.search(filename or "")
    if not match:
        return None
    suffix = match.group(4).lower()
    suffix_score = sum((ord(ch) - 96) for ch in suffix if "a" <= ch <= "z")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        suffix_score,
    )


def firmware_version_label(filename: str) -> Optional[str]:
    version = firmware_version_tuple(filename)
    if version is None:
        return None
    label = f"{version[0]}.{version[1]}.{version[2]:02d}"
    suffix_match = FIRMWARE_VERSION_RE.search(filename or "")
    if suffix_match and suffix_match.group(4):
        label += suffix_match.group(4)
    return label


def _firmware_sort_key(filename: str):
    version = firmware_version_tuple(filename)
    return (version or (0, 0, 0, 0), filename)


class JobStore:
    def __init__(self):
        self._runs: Dict[str, Run] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL)
        self._semaphore = threading.Semaphore(MAX_PARALLEL)

    def create_run(self, hosts_with_networks, mode, username, password, secret, api_key, org_id):
        run = Run(id=str(uuid.uuid4())[:8], created_at=time.time(), mode=mode,
                  username=username, password=password, secret=secret, api_key=api_key,
                  org_id=org_id,
                  jobs=[DeviceJob(host=h["host"], network_id=h["network_id"])
                        for h in hosts_with_networks])
        with self._lock:
            self._runs[run.id] = run
        return run

    def get(self, run_id):
        with self._lock:
            return self._runs.get(run_id)

    def serialize(self, run):
        with self._lock:
            return {"id":run.id,"created_at":run.created_at,"mode":run.mode,
                    "organization_id":run.org_id,
                    "current_stage":run.current_stage,"stage_running":run.stage_running,
                    "jobs":[asdict(j) for j in run.jobs]}

    # ---------- stage triggers ----------

    def run_precheck_stage(self, run):
        self._run_stage(run, lambda j: j.status == "pending",
                        self._do_precheck, "precheck_done", "precheck")

    def run_upgrade_stage(self, run, image_filename):
        self._run_stage(run, lambda j: j.status == "needs_upgrade",
                        lambda r,j,d: self._do_upgrade(r,j,d,image_filename),
                        "precheck_done", "upgrade")

    def run_apply_prereqs_stage(self, run):
        """
        Apply the generated device-mode config lines to each device that
        landed in needs_prereq_config after precheck. Re-runs the prereq
        check after the apply to confirm everything now passes.
        """
        self._run_stage(run, lambda j: j.status == "needs_prereq_config",
                        self._do_apply_prereqs, "precheck_done", "apply_prereqs")

    def run_enable_stage(self, run):
        self._run_stage(run, lambda j: j.status == "precheck_done",
                        self._do_enable, "enable_done", "enable")

    def run_claim_stage(self, run):
        self._run_stage(run, lambda j: j.status == "enable_done",
                        self._do_claim, "claim_done", "claim")

    def _run_stage(self, run, eligible, worker, done_state, stage_name):
        with self._lock:
            if run.stage_running: return
            run.stage_running = True
            run.current_stage = stage_name
            targets = [j for j in run.jobs if eligible(j)]
            for j in targets:
                j.status = "running"
                j.stage = stage_name
                if j.started_at is None:
                    j.started_at = time.time()

        def _runner():
            futures = [self._executor.submit(worker, run, j, done_state) for j in targets]
            for f in futures:
                try: f.result()
                except Exception as e: print(f"Stage worker crash: {e}")
            with self._lock:
                run.stage_running = False
                run.current_stage = done_state

        threading.Thread(target=_runner, daemon=True).start()

    # ---------- workers ----------

    def _do_precheck(self, run, job, done_state):
        with self._semaphore:
            try:
                with SwitchClient(job.host, run.username, run.password, run.secret) as sw:
                    version = sw.get_version()
                    hostname = sw.get_hostname()
                    raw, compatible = sw.check_compatibility()
                    # Device-mode prereq check only matters when we won't
                    # be factory-resetting the switch. Skip for cloud mode.
                    prereqs = None
                    prereq_sections = None
                    if run.mode == "device":
                        prereqs = sw.check_device_mode_prereqs()
                        prereq_sections = generate_device_mode_config(prereqs)
                with self._lock:
                    job.version = version
                    job.hostname = hostname
                    job.compatible = compatible
                    job.prereqs = prereqs
                    job.prereq_config = prereq_sections
                    if compatible is not True:
                        job.status = "failed"
                        job.error = f"Not compatible (compatible={compatible})"
                        job.finished_at = time.time()
                    elif not is_version_supported(version):
                        job.status = "needs_upgrade"
                        job.error = f"Running {version}, requires 17.15+"
                    elif prereq_sections:
                        job.status = "needs_prereq_config"
                        job.error = (f"{len(prereq_sections)} prerequisite "
                                     f"section(s) missing for device mode")
                    else:
                        job.status = done_state
            except SwitchError as e: self._fail(job, str(e))
            except Exception as e: self._fail(job, f"Unexpected: {e}")

    def _do_upgrade(self, run, job, done_state, image_filename):
        with self._semaphore:
            try:
                local_path = os.path.join(FIRMWARE_DIR, image_filename)
                if not os.path.isfile(local_path):
                    raise SwitchError(f"Image not in {FIRMWARE_DIR}: {image_filename}")

                def set_progress(pct, msg):
                    # Clamp to valid range — bugs in band math shouldn't
                    # send a 120% value to the UI or reset the bar backwards.
                    pct = max(0, min(100, int(pct)))
                    with self._lock:
                        # Never go backwards during the upgrade except when
                        # explicitly finishing a phase. Prevents flicker when
                        # a callback fires in the wrong order.
                        if pct > job.progress or pct in (5, 60, 62, 65, 66, 67, 70, 92, 100):
                            job.progress = pct
                        job.progress_message = msg

                def log(line):
                    self._log(job, line)

                size_bytes = os.path.getsize(local_path)
                log(f"▶ Starting upgrade to {image_filename} ({size_bytes // (1024*1024)} MB)")

                # Compute local MD5
                set_progress(2, "Computing local MD5")
                log("Computing local MD5...")
                import hashlib
                h = hashlib.md5()
                with open(local_path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                local_md5 = h.hexdigest()
                log(f"  local MD5 = {local_md5}")

                # Give the switch a moment to release any lingering SSH channels
                # from the precheck phase before we open a new session.
                log("Pausing 5s to let switch release SSH channels from precheck")
                time.sleep(5)

                # ---- SINGLE SESSION for SCP + verify + install ----
                # Opening multiple sequential sessions hits 'Resource shortage'
                # on switches with low VTY limits. One long-lived session
                # avoids the channel-slot issue entirely.
                set_progress(5, "Opening SSH session")
                log(f"Opening SSH session to {job.host}")
                with SwitchClient(job.host, run.username, run.password, run.secret) as sw:
                    log("  ✓ SSH connected, entered enable mode")

                    # SCP phase (5-60%)
                    def scp_cb(pct, msg):
                        set_progress(5 + int(pct * 0.55), msg)

                    def switch_log(line):
                        log(f"  {line}")

                    sw.copy_image_to_flash(
                        local_path, on_progress=scp_cb, on_log=switch_log,
                        expected_md5=local_md5,
                    )
                    set_progress(60, "Image on flash")
                    log("✓ SCP phase complete")

                    # Verify phase (60-65%) — same session
                    set_progress(62, "Verifying MD5 on switch")
                    log(f"Running: verify /md5 flash:{image_filename}")
                    computed = sw.verify_image_on_flash(image_filename, expected_md5=local_md5)
                    log(f"  switch MD5 = {computed}")
                    log("✓ MD5 match — image is intact")
                    set_progress(65, "MD5 match — image is intact")

                    # Clean up any leftover inactive packages from a previous
                    # failed run, otherwise the add phase will fail with
                    # "Super package already added".
                    set_progress(65, "Checking for stale install state")
                    log("Running: show install summary")
                    pre_summary = sw.conn.send_command("show install summary", read_timeout=30)
                    log(f"  pre-install summary:\n{pre_summary}")
                    if re.search(r"^IMG\s+I\s+", pre_summary, re.MULTILINE):
                        log("⚠ Found inactive package(s) from previous run")
                        log("Running: install remove inactive")
                        rm_out = sw.install_remove_inactive()
                        log(f"  remove output: {rm_out[-400:]}")
                        log("✓ Install workspace cleaned")
                    else:
                        log("✓ No stale packages to clean")

                    set_progress(66, "install add_activate_commit")
                    log(f"Running: install add file flash:{image_filename} activate commit prompt-level none")
                    log("  (fire-and-forget — we won't wait for the prompt to return)")
                    fire_out = sw.install_add_activate_commit_fire_and_forget(image_filename)
                    log(f"  {fire_out}")
                set_progress(68, "Install command sent, monitoring switch")

                # Phase 3: wait for reload using active probing (67-92%)
                # Don't assume a fixed timer — watch the switch go down, then come back.
                log("Probing switch to detect reload (ping + TCP 22)...")

                def reload_tick(phase, elapsed, note):
                    mins = elapsed // 60
                    secs = elapsed % 60
                    if phase == "going_down":
                        # 68-75% while waiting for the switch to stop responding.
                        # Scale against the full 7500s timeout so the bar doesn't
                        # pin at 75% for an hour.
                        pct = min(75, 68 + int(elapsed / 7500 * 7))
                        set_progress(pct, f"Waiting for reload to start ({mins}m{secs}s) — {note}")
                    else:  # coming_back
                        pct = min(92, 75 + int(elapsed / 1800 * 17))
                        set_progress(pct, f"Waiting for switch to come back ({mins}m{secs}s)")
                    if elapsed > 0 and elapsed % 30 < 16:  # log every ~30s
                        log(f"  [{phase}] {mins}m{secs}s: {note}")

                from switch import wait_for_reload
                wait_for_reload(job.host, on_tick=reload_tick,
                                 going_down_timeout=7500,   # 2h 5m — covers 7200s auto-abort
                                 coming_back_timeout=3600)  # 60 min for reload + boot
                log("✓ Switch is back up")

                # Phase 4: verify version (92-100%)
                set_progress(92, "Verifying new version")
                log("Running: show version | include Version")
                with SwitchClient(job.host, run.username, run.password, run.secret) as sw:
                    new_version = sw.get_version()
                    log(f"  new version = {new_version}")
                    if not is_version_supported(new_version):
                        raise SwitchError(f"Upgrade ran but version is still {new_version}")

                    # Commit the install so it survives a reload. Without
                    # this, the switch will roll back to the old image on
                    # the next power cycle via the auto-abort timer.
                    set_progress(96, "Committing install")
                    log("Running: install commit")
                    commit_out = sw.conn.send_command_timing(
                        "install commit", read_timeout=120,
                        strip_prompt=False, strip_command=False)
                    log(f"  commit output: {commit_out[-300:]}")

                log(f"✓ Upgrade complete ({new_version})")
                with self._lock:
                    job.version = new_version
                    job.error = None
                    job.progress = 100
                    job.progress_message = "Upgrade complete"
                    job.status = done_state
            except SwitchError as e: self._fail(job, f"Upgrade failed: {e}")
            except Exception as e: self._fail(job, f"Upgrade unexpected: {e}")

    def _do_apply_prereqs(self, run, job, done_state):
        """
        Apply the previously-generated config lines to bring a device-mode
        switch into compliance, then re-run the prereq check to confirm.
        """
        with self._semaphore:
            try:
                def log(line): self._log(job, line)
                sections = job.prereq_config or []
                if not sections:
                    with self._lock:
                        job.status = done_state
                    return

                # Flatten to a single config list, skipping lines that
                # start with '!' (which we use as placeholder for unknown
                # values the user needs to fill in by hand).
                all_lines = []
                for section_name, lines in sections:
                    log(f"▶ Applying section: {section_name}")
                    for line in lines:
                        if line.strip().startswith("!"):
                            raise SwitchError(
                                f"Cannot auto-apply: section '{section_name}' "
                                f"contains placeholder line requiring manual "
                                f"input ({line.strip()})")
                        log(f"  {line}")
                        all_lines.append(line)

                with SwitchClient(job.host, run.username, run.password,
                                   run.secret) as sw:
                    out = sw.apply_device_mode_config(all_lines)
                    log(f"apply output: {out[-400:]}")

                    # Re-check to confirm everything now passes
                    log("Re-running prereq check...")
                    new_prereqs = sw.check_device_mode_prereqs()
                    new_sections = generate_device_mode_config(new_prereqs)

                with self._lock:
                    job.prereqs = new_prereqs
                    job.prereq_config = new_sections
                    if new_sections:
                        # Still missing something after apply — fail loudly
                        remaining = ", ".join(s[0] for s in new_sections)
                        raise SwitchError(
                            f"Prereqs still failing after apply: {remaining}")
                    job.error = None
                    job.status = done_state
                    job.finished_at = time.time()
                log("✓ All device-mode prereqs satisfied")
            except SwitchError as e: self._fail(job, f"Apply failed: {e}")
            except Exception as e: self._fail(job, f"Apply unexpected: {e}")

    def _do_enable(self, run, job, done_state):
        """
        Run 'service meraki connect' on the switch and parse the cloud ID.
        This is the same command for both cloud and device modes — the
        cloud-vs-device choice is made at claim time via the Meraki API.
        """
        with self._semaphore:
            try:
                with SwitchClient(job.host, run.username, run.password, run.secret) as sw:
                    sw.enable_meraki_service()
                    cloud_id = sw.get_cloud_id()
                    if not cloud_id:
                        raise SwitchError(
                            "Could not parse cloud ID after enabling service")
                    with self._lock:
                        job.cloud_id = cloud_id
                        job.status = done_state
            except SwitchError as e: self._fail(job, str(e))
            except Exception as e: self._fail(job, f"Unexpected: {e}")

    def _do_claim(self, run, job, done_state):
        with self._semaphore:
            try:
                client = MerakiClient(run.api_key)
                client.claim_into_network(
                    job.network_id, job.cloud_id, run.mode,
                    username=run.username, password=run.password,
                    secret=run.secret,
                )
                # Apply hostname from the switch as the dashboard device name
                if job.hostname:
                    try:
                        client.update_device_name(job.cloud_id, job.hostname)
                    except MerakiError as e:
                        print(f"WARN: rename {job.cloud_id} -> {job.hostname}: {e}")
                with self._lock:
                    job.status = done_state
                    job.finished_at = time.time()
            except MerakiError as e: self._fail(job, str(e))
            except Exception as e: self._fail(job, f"Unexpected: {e}")

    def _log(self, job, line):
        """Append a timestamped line to the job's log buffer. Thread-safe."""
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            job.log.append(f"[{ts}] {line}")
            # Cap log size to prevent unbounded growth on long runs
            if len(job.log) > 500:
                job.log = job.log[-500:]

    def _fail(self, job, msg):
        with self._lock:
            job.status = "failed"
            job.error = msg
            job.finished_at = time.time()
            job.log.append(f"[{time.strftime('%H:%M:%S')}] ✕ FAILED: {msg}")


store = JobStore()
