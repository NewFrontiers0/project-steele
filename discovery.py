"""
Network discovery — scan a subnet for Catalyst switches.

Probes TCP 22 in parallel across every IP in the given CIDR range, then
SSHs into responders with the provided credentials and runs 'show version'
to identify Catalyst 9000 series switches.

Results include hostname, model, version, and serial — enough for the UI
to display a pick-list for the user.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from switch import SwitchClient, SwitchError


MAX_SCAN_PARALLEL = 20  # TCP probes are cheap; SSH logins less so


@dataclass
class DiscoveredDevice:
    ip: str
    ssh_open: bool = False
    is_catalyst: bool = False
    hostname: Optional[str] = None
    model: Optional[str] = None
    version: Optional[str] = None
    serial: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ScanJob:
    id: str
    subnet: str
    created_at: float
    status: str = "running"   # running | done | failed
    total_ips: int = 0
    probed: int = 0
    devices: List[DiscoveredDevice] = field(default_factory=list)
    error: Optional[str] = None


MODEL_RE = re.compile(r"(C9[23]\d{2}[A-Z0-9-]*|WS-C9\d{3}[A-Z0-9-]*)", re.IGNORECASE)
VERSION_RE = re.compile(r"Version\s+([0-9]+\.[0-9]+\.[0-9a-zA-Z]+)")
SERIAL_RE = re.compile(r"Processor board ID\s+([A-Z0-9]+)")
HOSTNAME_RE = re.compile(r"^(\S+)\s+uptime is", re.MULTILINE)


def _probe_ssh(ip: str, timeout: float = 2.0) -> bool:
    """Quick TCP 22 check — no auth, just SYN/ACK."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, 22))
        s.close()
        return True
    except Exception:
        return False


def _identify_switch(ip: str, username: str, password: str,
                      secret: Optional[str]) -> DiscoveredDevice:
    """SSH in and run show version to identify the device."""
    dev = DiscoveredDevice(ip=ip, ssh_open=True)
    try:
        with SwitchClient(ip, username, password, secret) as sw:
            out = sw.conn.send_command("show version", read_timeout=20)

            # Hostname — first line typically: "hostname uptime is ..."
            m = HOSTNAME_RE.search(out)
            if m:
                dev.hostname = m.group(1)

            # Model
            m = MODEL_RE.search(out)
            if m:
                dev.model = m.group(1)
                dev.is_catalyst = True

            # Version
            m = VERSION_RE.search(out)
            if m:
                dev.version = m.group(1)

            # Serial
            m = SERIAL_RE.search(out)
            if m:
                dev.serial = m.group(1)

            # If we didn't find a Catalyst model string, check for broader
            # indicators in the show version output
            if not dev.is_catalyst:
                lower = out.lower()
                if "catalyst" in lower or "cat9k" in lower or "c9300" in lower or "c9200" in lower:
                    dev.is_catalyst = True

    except SwitchError as e:
        dev.error = str(e)
    except Exception as e:
        dev.error = f"Unexpected: {e}"
    return dev


class ScanStore:
    """Thread-safe store for scan jobs."""

    def __init__(self):
        self._scans: Dict[str, ScanJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=MAX_SCAN_PARALLEL)

    def start_scan(self, subnet: str, username: str, password: str,
                    secret: Optional[str]) -> ScanJob:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid subnet: {e}")

        hosts = [str(ip) for ip in network.hosts()]
        scan = ScanJob(
            id=str(uuid.uuid4())[:8],
            subnet=subnet,
            created_at=time.time(),
            total_ips=len(hosts),
        )
        with self._lock:
            self._scans[scan.id] = scan

        def _run():
            try:
                # Phase 1: parallel TCP 22 probe
                ssh_open = []
                futures = {
                    self._executor.submit(_probe_ssh, ip): ip
                    for ip in hosts
                }
                for future in as_completed(futures):
                    ip = futures[future]
                    with self._lock:
                        scan.probed += 1
                    try:
                        if future.result():
                            ssh_open.append(ip)
                    except Exception:
                        pass

                # Phase 2: SSH into responders and identify
                id_futures = {
                    self._executor.submit(
                        _identify_switch, ip, username, password, secret
                    ): ip
                    for ip in ssh_open
                }
                for future in as_completed(id_futures):
                    try:
                        dev = future.result()
                        with self._lock:
                            scan.devices.append(dev)
                    except Exception:
                        pass

                with self._lock:
                    scan.status = "done"
            except Exception as e:
                with self._lock:
                    scan.status = "failed"
                    scan.error = str(e)

        threading.Thread(target=_run, daemon=True).start()
        return scan

    def get(self, scan_id: str) -> Optional[ScanJob]:
        with self._lock:
            return self._scans.get(scan_id)

    def serialize(self, scan: ScanJob) -> dict:
        with self._lock:
            return {
                "id": scan.id,
                "subnet": scan.subnet,
                "status": scan.status,
                "total_ips": scan.total_ips,
                "probed": scan.probed,
                "error": scan.error,
                "devices": [asdict(d) for d in scan.devices],
            }


scan_store = ScanStore()
