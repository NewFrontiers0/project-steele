"""Latency checks for public global targets and the local default gateway."""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LatencyTarget:
    id: str
    label: str
    country: str
    region: str
    ip: str
    source: str = "RIPE Atlas anchor"
    local: bool = False


# Public RIPE Atlas anchor IPv4 targets. They are intentionally centralized so
# operators can replace an anchor if a target is retired or stops answering ICMP.
GLOBAL_TARGETS = [
    LatencyTarget("london", "London", "GB", "Europe", "45.77.229.242"),
    LatencyTarget("amsterdam", "Amsterdam", "NL", "Europe", "193.0.0.165"),
    LatencyTarget("frankfurt", "Frankfurt", "DE", "Europe", "2.56.11.26"),
    LatencyTarget("ashburn", "Ashburn", "US", "North America", "37.10.42.14"),
    LatencyTarget("chicago", "Chicago", "US", "North America", "156.154.39.254"),
    LatencyTarget("singapore", "Singapore", "SG", "Asia", "103.140.3.227"),
    LatencyTarget("tokyo", "Tokyo", "JP", "Asia", "152.195.112.52"),
    LatencyTarget("sydney", "Sydney", "AU", "Oceania", "157.20.113.125"),
    LatencyTarget("cape-town", "Cape Town", "ZA", "Africa", "102.222.103.100"),
]


def run_latency_test(count: int = 3, timeout_seconds: int = 2) -> dict:
    targets = []
    gateway = default_gateway()
    if gateway:
        targets.append(
            LatencyTarget(
                "local",
                "Local gateway",
                "LAN",
                "Local",
                gateway["gateway"],
                f"default next-hop via {gateway.get('interface') or 'unknown interface'}",
                local=True,
            )
        )
    targets.extend(GLOBAL_TARGETS)

    max_workers = min(_latency_parallelism(), len(targets))
    started = time.time()
    results = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_measure_target, target, count, timeout_seconds): index
            for index, target in enumerate(targets)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    completed = int(time.time())
    ok_results = [r for r in results if r and r["ok"] and r["avg_ms"] is not None]
    return {
        "ok": True,
        "tested_at": completed,
        "duration_seconds": round(time.time() - started, 2),
        "target_count": len(results),
        "reachable_count": len(ok_results),
        "results": results,
    }


def default_gateway() -> Optional[dict]:
    system = platform.system().lower()
    if system == "linux":
        gateway = _linux_gateway()
        if gateway:
            return gateway
    if system == "darwin":
        gateway = _darwin_gateway()
        if gateway:
            return gateway
    return _netstat_gateway()


def latency_targets() -> dict:
    gateway = default_gateway()
    targets = []
    if gateway:
        targets.append({
            "id": "local",
            "label": "Local gateway",
            "country": "LAN",
            "region": "Local",
            "ip": gateway["gateway"],
            "source": f"default next-hop via {gateway.get('interface') or 'unknown interface'}",
            "local": True,
        })
    targets.extend(_target_dict(t) for t in GLOBAL_TARGETS)
    return {"targets": targets}


def _measure_target(target: LatencyTarget, count: int, timeout_seconds: int) -> dict:
    started = time.time()
    result = _ping(target.ip, count, timeout_seconds)
    return {
        **_target_dict(target),
        **result,
        "duration_seconds": round(time.time() - started, 2),
    }


def _target_dict(target: LatencyTarget) -> dict:
    return {
        "id": target.id,
        "label": target.label,
        "country": target.country,
        "region": target.region,
        "ip": target.ip,
        "source": target.source,
        "local": target.local,
    }


def _ping(ip: str, count: int, timeout_seconds: int) -> dict:
    args = ["ping", "-n", "-c", str(count)]
    if platform.system().lower() == "darwin":
        args.extend(["-W", str(timeout_seconds * 1000)])
    else:
        args.extend(["-W", str(timeout_seconds)])
    args.append(ip)

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=max(5, (timeout_seconds + 1) * count + 2),
            check=False,
        )
    except FileNotFoundError:
        return _failed("ping command is not available")
    except subprocess.TimeoutExpired:
        return _failed("ping timed out")

    output = f"{proc.stdout}\n{proc.stderr}".strip()
    transmitted, received = _packet_counts(output, count)
    avg_ms = _average_ms(output)
    loss_pct = 100.0
    if transmitted:
        loss_pct = round(max(0, transmitted - received) * 100 / transmitted, 1)
    ok = received > 0 and avg_ms is not None
    return {
        "ok": ok,
        "status": "reachable" if ok else "unreachable",
        "avg_ms": avg_ms,
        "min_ms": _stat_ms(output, 1),
        "max_ms": _stat_ms(output, 3),
        "packet_loss_pct": loss_pct,
        "packets_sent": transmitted,
        "packets_received": received,
        "error": None if ok else _last_error(output),
    }


def _packet_counts(output: str, fallback_count: int) -> tuple[int, int]:
    match = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received", output)
    if match:
        return int(match.group(1)), int(match.group(2))
    return fallback_count, len(re.findall(r"time[=<]([\d.]+)\s*ms", output))


def _average_ms(output: str) -> Optional[float]:
    stats = re.search(r"(?:rtt|round-trip).*=\s*([\d.]+)/([\d.]+)/([\d.]+)", output)
    if stats:
        return round(float(stats.group(2)), 1)
    samples = [float(v) for v in re.findall(r"time[=<]([\d.]+)\s*ms", output)]
    if not samples:
        return None
    return round(sum(samples) / len(samples), 1)


def _stat_ms(output: str, group: int) -> Optional[float]:
    stats = re.search(r"(?:rtt|round-trip).*=\s*([\d.]+)/([\d.]+)/([\d.]+)", output)
    if not stats:
        return None
    return round(float(stats.group(group)), 1)


def _last_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "No response"
    for line in reversed(lines):
        if "packets transmitted" not in line and "packet loss" not in line:
            return line[:180]
    return "No response"


def _failed(error: str) -> dict:
    return {
        "ok": False,
        "status": "failed",
        "avg_ms": None,
        "min_ms": None,
        "max_ms": None,
        "packet_loss_pct": 100.0,
        "packets_sent": 0,
        "packets_received": 0,
        "error": error,
    }


def _linux_gateway() -> Optional[dict]:
    for args in (["ip", "route", "get", "1.1.1.1"], ["ip", "route", "show", "default"]):
        output = _command_output(args)
        if not output:
            continue
        via = re.search(r"\bvia\s+([0-9.]+)", output)
        dev = re.search(r"\bdev\s+(\S+)", output)
        if via:
            return {"gateway": via.group(1), "interface": dev.group(1) if dev else None}
    return None


def _darwin_gateway() -> Optional[dict]:
    output = _command_output(["route", "-n", "get", "default"])
    if not output:
        return None
    gateway = re.search(r"gateway:\s+([0-9.]+)", output)
    interface = re.search(r"interface:\s+(\S+)", output)
    if gateway:
        return {"gateway": gateway.group(1), "interface": interface.group(1) if interface else None}
    return None


def _netstat_gateway() -> Optional[dict]:
    output = _command_output(["netstat", "-rn"])
    if not output:
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"default", "0.0.0.0"} and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[1]):
            return {"gateway": parts[1], "interface": parts[-1] if len(parts) > 3 else None}
    return None


def _command_output(args: list[str]) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return f"{proc.stdout}\n{proc.stderr}".strip()


def _latency_parallelism() -> int:
    try:
        return max(1, min(24, int(os.environ.get("LATENCY_MAX_PARALLEL", "12"))))
    except ValueError:
        return 12
