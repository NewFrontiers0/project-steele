"""Netmiko wrapper — adds hostname, version comparison, install-mode upgrade."""
from __future__ import annotations

import os
import re
import time
import urllib.parse
from typing import Optional, Tuple

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException


class SwitchError(Exception): pass


CLOUD_ID_RE = re.compile(r"\b(Q[0-9A-Z]{3}-[0-9A-Z]{4}-[0-9A-Z]{4})\b")
VERSION_RE = re.compile(r"Version\s+([0-9]+\.[0-9]+\.[0-9a-zA-Z]+)")
HOSTNAME_RE = re.compile(r"^hostname\s+(\S+)", re.MULTILINE)
MIN_VERSION = (17, 15)


def parse_version_tuple(v: str) -> Tuple[int, int, int]:
    parts = re.findall(r"\d+", v or "")
    if len(parts) >= 2:
        return (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    return (0, 0, 0)


def is_version_supported(v: str) -> bool:
    return parse_version_tuple(v)[:2] >= MIN_VERSION


def generate_device_mode_config(prereqs, ntp_server="pool.ntp.org",
                                  dns_server="8.8.8.8"):
    """
    Given the output of SwitchClient.check_device_mode_prereqs(), return a
    list of (description, config_lines) tuples for each missing prereq.
    The UI renders these so the user sees exactly what's going to apply.

    Returns [] when everything already passes.
    """
    sections = []

    if not prereqs["aaa_new_model"] or not prereqs["aaa_auth_login"] or not prereqs["aaa_authz_exec"]:
        lines = []
        if not prereqs["aaa_new_model"]:
            lines.append("aaa new-model")
        if not prereqs["aaa_auth_login"]:
            lines.append("aaa authentication login default local")
        if not prereqs["aaa_authz_exec"]:
            lines.append("aaa authorization exec default local")
        sections.append(("AAA", lines))

    if not prereqs["ip_routing"]:
        sections.append(("IP routing", ["ip routing"]))

    # Clean up the unsupported ip default-gateway if present
    if prereqs["uses_ip_default_gateway"]:
        sections.append((
            "Remove unsupported ip default-gateway",
            ["no ip default-gateway"],
        ))

    if not prereqs["default_route"]:
        gw = prereqs.get("detected_gateway")
        if gw:
            sections.append((
                f"Default route via detected gateway {gw}",
                [f"ip route 0.0.0.0 0.0.0.0 {gw}"],
            ))
        else:
            # We can't safely guess the gateway — flag it so the UI can warn
            sections.append((
                "Default route — GATEWAY UNKNOWN, needs manual input",
                ["! ip route 0.0.0.0 0.0.0.0 <your-gateway-here>"],
            ))

    if not prereqs["dns_lookup"]:
        sections.append(("DNS", [
            "ip domain lookup",
            f"ip name-server {dns_server}",
        ]))

    if not prereqs["ntp_configured"]:
        sections.append((f"NTP ({ntp_server})", [f"ntp server {ntp_server}"]))

    return sections


class SwitchClient:
    def __init__(self, host, username, password, secret=None):
        self.device = {"device_type":"cisco_xe","host":host,"username":username,
                       "password":password,"secret":secret or password,
                       "fast_cli":False,"conn_timeout":15}
        self.conn = None

    def __enter__(self):
        try:
            self.conn = ConnectHandler(**self.device)
            self.conn.enable()
        except NetmikoAuthenticationException as e:
            raise SwitchError(f"Authentication failed: {e}") from e
        except NetmikoTimeoutException as e:
            raise SwitchError(f"Connection timed out: {e}") from e
        except Exception as e:
            raise SwitchError(f"SSH connect failed: {e}") from e
        return self

    def __exit__(self, *a):
        if self.conn is not None:
            try: self.conn.disconnect()
            except Exception: pass

    def get_version(self):
        out = self.conn.send_command("show version | include Version", read_timeout=20)
        m = VERSION_RE.search(out)
        if not m: raise SwitchError(f"Could not parse version from: {out!r}")
        return m.group(1)

    def get_hostname(self):
        try:
            out = self.conn.send_command("show running-config | include ^hostname", read_timeout=20)
        except Exception:
            return None
        m = HOSTNAME_RE.search(out)
        return m.group(1) if m else None

    def check_compatibility(self):
        out = self.conn.send_command("show meraki compatibility", read_timeout=30)
        lower = out.lower()
        if any(s in lower for s in ("not compatible","incompatible","not supported","unsupported")):
            return out, False
        if any(s in lower for s in ("compatible","supported","ready for meraki","ready to migrate")):
            return out, True
        return out, None

    def get_chassis_serial(self):
        try:
            out = self.conn.send_command("show version | include Processor board ID", read_timeout=20)
        except Exception:
            return None
        m = re.search(r"Processor board ID\s+([A-Z0-9]+)", out)
        return m.group(1) if m else None

    # ---------- device-mode prerequisite check ----------

    def check_device_mode_prereqs(self):
        """
        Check the IOS-XE configuration for the prerequisites required by
        Meraki cloud management with device configuration.

        Returns a dict with per-check pass/fail booleans and the detected
        default gateway (for use in the config generator). The checks come
        directly from the Meraki docs:
        documentation.meraki.com/MS/Cloud_Management_with_IOS_XE/
        Connect_Hybrid_Operating_Mode_Catalyst_Switch_to_Dashboard
        """
        results = {
            "ip_routing": False,
            "default_route": False,
            "aaa_new_model": False,
            "aaa_auth_login": False,
            "aaa_authz_exec": False,
            "dns_lookup": False,
            "ntp_configured": False,
            "detected_gateway": None,
            "uses_ip_default_gateway": False,  # explicitly flagged — NOT supported
        }

        # 1. ip routing
        try:
            out = self.conn.send_command(
                "show running-config | include ^ip routing|^no ip routing",
                read_timeout=20)
            # Any line matching 'ip routing' that isn't 'no ip routing'
            for line in out.splitlines():
                if line.strip() == "ip routing":
                    results["ip_routing"] = True
                    break
        except Exception:
            pass

        # 2. Default route (must be installed, must not be via ip default-gateway)
        try:
            out = self.conn.send_command("show ip route 0.0.0.0", read_timeout=20)
            # Two possible output forms:
            #   "Gateway of last resort is X.X.X.X to network 0.0.0.0"
            #   "Routing entry for 0.0.0.0/0 ... * X.X.X.X"  (no 'Gateway of last resort' line)
            m = re.search(r"Gateway of last resort is\s+(\d+\.\d+\.\d+\.\d+)", out)
            if not m:
                # Fall back to the Routing entry block — look for the '*' next-hop line
                m = re.search(r"^\s*\*\s+(\d+\.\d+\.\d+\.\d+)", out, re.MULTILINE)
            if m:
                results["default_route"] = True
                results["detected_gateway"] = m.group(1)
            elif "Routing entry for 0.0.0.0" in out:
                # Route exists but we couldn't parse a next-hop — still counts
                results["default_route"] = True
        except Exception:
            pass

        try:
            rc_gw = self.conn.send_command(
                "show running-config | include ^ip default-gateway",
                read_timeout=20)
            if re.search(r"^ip default-gateway", rc_gw, re.MULTILINE):
                results["uses_ip_default_gateway"] = True
        except Exception:
            pass

        # 3-5. AAA lines
        try:
            aaa = self.conn.send_command(
                "show running-config | include ^aaa", read_timeout=20)
            if re.search(r"^aaa new-model\s*$", aaa, re.MULTILINE):
                results["aaa_new_model"] = True
            if re.search(r"^aaa authentication login default local",
                         aaa, re.MULTILINE):
                results["aaa_auth_login"] = True
            if re.search(r"^aaa authorization exec default local",
                         aaa, re.MULTILINE):
                results["aaa_authz_exec"] = True
        except Exception:
            pass

        # 6. DNS — check `show hosts summary` which is the source of truth.
        # It reflects both statically configured name-servers AND ones learned
        # via DHCP. Only 'no ip domain lookup' would appear in running-config
        # since lookup is enabled by default.
        try:
            hosts = self.conn.send_command("show hosts summary", read_timeout=20)
            # Look for "Name servers are <IP>, <IP>" with at least one IP
            has_ns = bool(re.search(
                r"Name servers are\s+\d+\.\d+\.\d+\.\d+", hosts))

            # Also confirm lookup isn't explicitly disabled
            rc = self.conn.send_command(
                "show running-config | include ^no ip domain lookup",
                read_timeout=20)
            lookup_disabled = bool(re.search(
                r"^no ip domain lookup", rc, re.MULTILINE))

            results["dns_lookup"] = has_ns and not lookup_disabled
        except Exception:
            pass

        # 7. NTP — configured AND has picked a valid stratum/reference.
        # IOS-XE only reports "Clock is synchronized" after the loopfilter
        # transitions from FREQ (drift-measuring) to PLL (locked), which
        # takes 5-15 minutes after NTP first comes up. But the clock is
        # already accurate enough for mutual TLS much earlier — stratum
        # 1-15 with a reference peer is what Meraki actually needs.
        try:
            ntp_cfg = self.conn.send_command(
                "show running-config | include ^ntp server", read_timeout=20)
            has_ntp = bool(re.search(r"^ntp server", ntp_cfg, re.MULTILINE))
            if has_ntp:
                status = self.conn.send_command("show ntp status",
                                                 read_timeout=20)
                if "Clock is synchronized" in status:
                    results["ntp_configured"] = True
                else:
                    # Accept "actively polling a valid peer" as good enough.
                    # Require: stratum 1-15 (16 = unreachable) AND a
                    # reference peer picked AND an update within last 5 min.
                    stratum_m = re.search(r"stratum\s+(\d+)", status)
                    ref_m = re.search(r"reference is\s+(\S+)", status)
                    update_m = re.search(r"last update was\s+(\d+)\s+sec",
                                          status)
                    if (stratum_m and ref_m and update_m and
                        1 <= int(stratum_m.group(1)) <= 15 and
                        ref_m.group(1) not in ("unknown", "unsynced") and
                        int(update_m.group(1)) < 300):
                        results["ntp_configured"] = True
        except Exception:
            pass

        return results

    def apply_device_mode_config(self, config_lines):
        """
        Apply a list of config lines to the switch. Caller is responsible
        for building the list (see generate_device_mode_config below).

        Returns the combined command output for logging.
        """
        if not config_lines:
            return "no config to apply"
        try:
            return self.conn.send_config_set(config_lines, read_timeout=120)
        except Exception as e:
            raise SwitchError(f"Failed to apply device-mode config: {e}") from e

    def enable_meraki_service(self):
        try:
            return self.conn.send_config_set(["service meraki connect"], read_timeout=60)
        except Exception as e:
            raise SwitchError(f"Failed to enable meraki service: {e}") from e

    def get_cloud_id(self):
        for cmd in ("show meraki connect status","show meraki","show meraki connect"):
            try:
                out = self.conn.send_command(cmd, read_timeout=20)
            except Exception:
                continue
            m = CLOUD_ID_RE.search(out)
            if m: return m.group(1)
        return None

    def configure_device_mode_prereqs(self):
        try:
            return self.conn.send_config_set(["meraki management","enable"], read_timeout=60)
        except Exception as e:
            raise SwitchError(f"Failed to push device-mode prereqs: {e}") from e

    def run_cli_commands(self, commands, read_timeout=60):
        """
        Run one or more CLI commands over the active SSH session.

        send_command_timing is used so commands that change prompt context
        such as "configure terminal" can still be sent line-by-line.
        """
        results = []
        for command in commands:
            try:
                output = self.conn.send_command_timing(
                    command,
                    read_timeout=read_timeout,
                    strip_prompt=True,
                    strip_command=True,
                )
            except Exception as e:
                raise SwitchError(f"Command failed ({command}): {e}") from e
            results.append({"command": command, "output": output})
        return results

    def fsck_flash(self):
        """Run a flash filesystem check/repair and answer simple prompts."""
        try:
            out = self.conn.send_command_timing(
                "fsck flash:",
                read_timeout=300,
                strip_prompt=False,
                strip_command=False,
            )
            lower = out.lower()
            for _ in range(4):
                if (
                    "[confirm]" in lower
                    or "[y/n]" in lower
                    or "continue" in lower
                    or "proceed" in lower
                    or "fix" in lower
                    or "repair" in lower
                ):
                    answer = "y" if "[y/n]" in lower or "fix" in lower or "repair" in lower else ""
                    out += self.conn.send_command_timing(
                        answer,
                        read_timeout=300,
                        strip_prompt=False,
                        strip_command=False,
                    )
                    lower = out.lower()
                    continue
                break
            return out
        except Exception as e:
            raise SwitchError(f"fsck flash: failed: {e}") from e

    def save_running_config(self, on_log=None):
        """Save running-config before IOS-XE install commands."""
        def L(msg):
            if on_log:
                on_log(msg)

        cmd = "copy running-config startup-config"
        try:
            out = self.conn.send_command_timing(
                cmd,
                read_timeout=180,
                strip_prompt=False,
                strip_command=False,
            )
            lower = out.lower()
            for _ in range(6):
                if (
                    "destination filename" in lower
                    or "[startup-config]" in lower
                    or "[confirm]" in lower
                    or "confirm" in lower
                    or "overwrite" in lower
                ):
                    out += self.conn.send_command_timing(
                        "",
                        read_timeout=180,
                        strip_prompt=False,
                        strip_command=False,
                    )
                    lower = out.lower()
                    continue
                break
        except Exception as e:
            raise SwitchError(f"Could not save running-config: {e}") from e

        tail = out.strip()[-500:]
        L(f"Save config output: {tail or '(empty)'}")
        lower = out.lower()
        if "% invalid" in lower or "% error" in lower or "failed" in lower:
            raise SwitchError(f"Could not save running-config: {tail}")
        return out

    def copy_image_from_http_to_flash(self, image_filename, source_url,
                                      expected_size=None, copy_vrf=None,
                                      source_interface=None,
                                      on_progress=None, on_log=None):
        return self._copy_image_from_url_to_flash(
            image_filename,
            source_url,
            expected_size=expected_size,
            copy_vrf=copy_vrf,
            source_interface=source_interface,
            on_progress=on_progress,
            on_log=on_log,
            protocol_label="HTTP",
        )

    def copy_image_from_ftp_to_flash(self, image_filename, source_url,
                                     expected_size=None, copy_vrf=None,
                                     source_interface=None,
                                     on_progress=None, on_log=None,
                                     passive=False):
        return self._copy_image_from_url_to_flash(
            image_filename,
            source_url,
            expected_size=expected_size,
            copy_vrf=copy_vrf,
            source_interface=source_interface,
            on_progress=on_progress,
            on_log=on_log,
            protocol_label="FTP",
            ftp_passive=passive,
        )

    def copy_image_from_tftp_to_flash(self, image_filename, source_url,
                                      expected_size=None, copy_vrf=None,
                                      source_interface=None,
                                      on_progress=None, on_log=None):
        return self._copy_image_from_url_to_flash(
            image_filename,
            source_url,
            expected_size=expected_size,
            copy_vrf=copy_vrf,
            source_interface=source_interface,
            on_progress=on_progress,
            on_log=on_log,
            protocol_label="TFTP",
        )

    def _copy_image_from_url_to_flash(self, image_filename, source_url,
                                      expected_size=None, copy_vrf=None,
                                      source_interface=None,
                                      on_progress=None, on_log=None,
                                      protocol_label="HTTP",
                                      ftp_passive=False):
        """
        Ask the switch to pull an image from the app over URL-based copy.

        This avoids IOS-XE SCP server stalls seen on some trains while still
        using the already-downloaded firmware file from the app host.
        """
        def L(msg):
            if on_log:
                on_log(msg)

        protocol_label = (protocol_label or "HTTP").upper()
        dest_name = os.path.basename(image_filename)
        if not dest_name or dest_name in {".", ".."}:
            raise SwitchError("Invalid image filename")
        copy_vrf = (copy_vrf or "").strip()
        source_interface = (source_interface or "").strip()
        copy_source_url = source_url
        safe_source_url = source_url

        if protocol_label == "FTP":
            parsed_ftp = urllib.parse.urlparse(source_url)
            ftp_username = urllib.parse.unquote(parsed_ftp.username or "")
            ftp_password = urllib.parse.unquote(parsed_ftp.password or "")
            if ftp_username or ftp_password:
                L("Configuring FTP username/password on switch")
                ftp_config = []
                if ftp_username:
                    ftp_config.append(f"ip ftp username {ftp_username}")
                if ftp_password:
                    ftp_config.append(f"ip ftp password {ftp_password}")
                if ftp_passive:
                    ftp_config.append("ip ftp passive")
                else:
                    ftp_config.append("no ip ftp passive")
                try:
                    out = self.conn.send_config_set(ftp_config, read_timeout=30)
                    if ftp_password:
                        out = out.replace(ftp_password, "***")
                    L(f"  output: {out.strip()[-240:]}")
                except Exception as e:
                    raise SwitchError(f"Could not configure FTP credentials: {e}") from e
                L(f"  FTP data mode: {'passive' if ftp_passive else 'active'}")

                host = parsed_ftp.hostname or ""
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                if parsed_ftp.port:
                    host = f"{host}:{parsed_ftp.port}"
                copy_source_url = urllib.parse.urlunparse((
                    parsed_ftp.scheme,
                    host,
                    parsed_ftp.path,
                    "",
                    "",
                    "",
                ))
                safe_source_url = copy_source_url

        if source_interface:
            if protocol_label == "FTP":
                source_cmd = f"ip ftp source-interface {source_interface}"
            elif protocol_label == "TFTP":
                source_cmd = f"ip tftp source-interface {source_interface}"
            else:
                source_cmd = f"ip http client source-interface {source_interface}"
            L(f"Configuring {protocol_label} source-interface {source_interface}")
            try:
                out = self.conn.send_config_set(
                    [source_cmd],
                    read_timeout=30,
                )
                L(f"  output: {out.strip()[-240:]}")
            except Exception as e:
                raise SwitchError(
                    f"Could not configure {protocol_label} source-interface "
                    f"{source_interface}: {e}") from e

        L("Running: dir flash: | include bytes (free|total)")
        try:
            df_out = self.conn.send_command(
                "dir flash: | include bytes (free|total)", read_timeout=20)
            L(f"  output: {df_out.strip()[:200]}")
            free_match = re.search(r"([\d,]+)\s+bytes\s+free", df_out)
            if free_match and expected_size:
                free_bytes = int(free_match.group(1).replace(",", ""))
                free_mb = free_bytes // (1024 * 1024)
                need_mb = expected_size // (1024 * 1024)
                L(f"  parsed: {free_mb} MB free")
                if free_bytes < expected_size * 1.1:
                    raise SwitchError(
                        f"Insufficient flash: {free_mb} MB free, need {need_mb} MB")
        except SwitchError:
            raise
        except Exception as e:
            L(f"  WARN: free space check failed: {e}")

        L(f"Running: dir flash:{dest_name}")
        try:
            existing = self.conn.send_command(f"dir flash:{dest_name}", read_timeout=20)
            L(f"  output: {existing.strip()[:200]}")
        except Exception as e:
            existing = ""
            L(f"  dir failed: {e}")

        if dest_name in existing and "No such file" not in existing and "Error" not in existing:
            L(f"Existing {dest_name} found on flash, deleting before {protocol_label} copy")
            try:
                del_out = self.conn.send_command_timing(
                    f"delete /force flash:{dest_name}",
                    read_timeout=30,
                    strip_prompt=False,
                    strip_command=False,
                )
                L(f"  output: {del_out.strip()[:160]}")
            except Exception as e:
                raise SwitchError(f"Could not delete existing flash:{dest_name}: {e}") from e

        if on_progress:
            on_progress(12, f"Switch pulling image from app over {protocol_label}")
        cmd = f"copy {'/vrf ' + copy_vrf + ' ' if copy_vrf else ''}{copy_source_url} flash:"
        safe_cmd = f"copy {'/vrf ' + copy_vrf + ' ' if copy_vrf else ''}{safe_source_url} flash:"
        L(f"Running: {safe_cmd}")
        try:
            out = ""
            prompt = self.conn.find_prompt()
            prompt_pattern = re.escape(prompt)
            last = self.conn.send_command_timing(
                cmd,
                read_timeout=60,
                strip_prompt=False,
                strip_command=False,
            )
            out += last
            lower = last.lower()
            if "destination filename" in lower or "destination file name" in lower:
                L(f"  answering destination filename prompt with {dest_name}")
                self.conn.write_channel(dest_name + "\n")
            elif "[confirm]" in lower or "overwrite" in lower or "do you want" in lower:
                L("  confirming copy prompt")
                self.conn.write_channel("\n")

            if on_progress:
                on_progress(24, f"Waiting for {protocol_label} copy to finish on switch")
            L(f"  waiting for switch prompt after {protocol_label} copy")
            out += self.conn.read_until_pattern(
                pattern=prompt_pattern,
                read_timeout=3600,
            )
        except Exception as e:
            raise SwitchError(f"{protocol_label} firmware copy failed: {e}") from e

        L(f"  copy output tail: {out.strip()[-500:]}")
        if "%error" in out.lower() or "error opening" in out.lower() or "timed out" in out.lower():
            parsed_source = urllib.parse.urlparse(source_url)
            if protocol_label == "TFTP":
                port_hint = parsed_source.port or 69
                transport_hint = f"UDP/{port_hint}"
                extra_hint = (
                    " Cisco IOS-XE usually requires standard TFTP on UDP/69 "
                    "and rejects tftp:// URLs with explicit ports."
                )
            else:
                port_hint = parsed_source.port or (443 if parsed_source.scheme == "https" else 80)
                transport_hint = f"TCP/{port_hint}"
                extra_hint = ""
            hint = (
                "If the switch management interface is in a VRF, set SWIM Copy VRF "
                "to that VRF, commonly Mgmt-vrf, and set Transfer source interface to "
                "the management SVI/interface. Also confirm the switch can route "
                f"to the app host and that any ACL/firewall permits {transport_hint}."
                f"{extra_hint}"
            )
            raise SwitchError(f"{protocol_label} firmware copy failed: {out.strip()[-500:]}\n{hint}")

        if on_progress:
            on_progress(96, "Verifying size on flash")
        L(f"Running: dir flash:{dest_name}")
        try:
            dir_out = self.conn.send_command(f"dir flash:{dest_name}", read_timeout=30)
            L(f"  output: {dir_out.strip()[:240]}")
        except Exception as e:
            raise SwitchError(f"Could not check file size after HTTP copy: {e}") from e

        if expected_size:
            flash_size = self._parse_flash_file_size(dir_out, dest_name)
            if flash_size != expected_size:
                raise SwitchError(
                    f"Size mismatch after {protocol_label} copy: local={expected_size}, "
                    f"flash={flash_size}. Transfer was truncated or failed.")
            L(f"{protocol_label} copy complete, {flash_size:,} bytes on flash")
        if on_progress:
            on_progress(100, f"{protocol_label} copy complete")
        return f"{dest_name} copied to flash from {protocol_label}"

    @staticmethod
    def _parse_flash_file_size(dir_out, dest_name):
        size_match = re.search(
            r"(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+" + re.escape(dest_name),
            dir_out,
        )
        if not size_match:
            for line in dir_out.splitlines():
                if dest_name in line:
                    nums = re.findall(r"\b(\d{6,})\b", line)
                    if nums:
                        return int(max(nums, key=int))
        if not size_match:
            raise SwitchError(
                f"Could not parse size of flash:{dest_name} from dir output. "
                f"Raw: {dir_out.strip()[:300]}")
        return int(size_match.group(1))

    def verify_image_on_flash(self, image_filename, expected_md5=None):
        """
        Run 'verify /md5 flash:<image>' and confirm the switch reports a valid
        MD5. If expected_md5 is provided, also confirm it matches. Returns the
        computed MD5 string. Raises SwitchError on mismatch or parse failure.

        This is a deliberate belt-and-braces check before issuing 'install
        add file' — a half-downloaded image will hang or brick the install.
        """
        try:
            out = self.conn.send_command(
                f"verify /md5 flash:{image_filename}",
                read_timeout=300,  # MD5 of 800MB takes a minute or two
            )
        except Exception as e:
            raise SwitchError(f"verify command failed: {e}") from e

        # Output looks like: 'verify /md5 (flash:...) = a1b2c3...'
        m = re.search(r"=\s*([0-9a-fA-F]{32})", out)
        if not m:
            raise SwitchError(
                f"Could not parse MD5 from verify output. The image may be "
                f"missing or unreadable. Raw: {out[-200:]!r}")
        computed = m.group(1).lower()

        if expected_md5 and computed != expected_md5.lower():
            raise SwitchError(
                f"MD5 mismatch on flash:{image_filename} — "
                f"expected {expected_md5}, got {computed}")
        return computed

    def copy_image_to_flash(self, local_path, on_progress=None, on_log=None,
                              expected_md5=None):
        """
        SCP an image to flash: with real per-block progress and verbose logging.

        on_progress(percent, message) - for progress bar
        on_log(line) - for the terminal panel
        expected_md5 - if provided, used for the fast-path cache check

        This method is STRICT about errors: any SCP exception, size mismatch,
        or post-transfer verify failure raises SwitchError. We do not swallow
        transport errors.
        """
        import hashlib

        def L(msg):
            if on_log: on_log(msg)

        if not os.path.isfile(local_path):
            raise SwitchError(f"Image not found: {local_path}")

        size_bytes = os.path.getsize(local_path)
        size_mb = size_bytes // (1024 * 1024)
        dest_name = os.path.basename(local_path)
        L(f"Local file: {dest_name} ({size_bytes:,} bytes / {size_mb} MB)")

        # ---- Free space check ----
        L("Running: dir flash: | include bytes (free|total)")
        try:
            df_out = self.conn.send_command(
                "dir flash: | include bytes (free|total)", read_timeout=20)
            L(f"  output: {df_out.strip()[:200]}")
            free_match = re.search(r"([\d,]+)\s+bytes\s+free", df_out)
            if free_match:
                free_bytes = int(free_match.group(1).replace(",", ""))
                free_mb = free_bytes // (1024 * 1024)
                L(f"  parsed: {free_mb} MB free")
                if free_bytes < size_bytes * 1.1:
                    raise SwitchError(
                        f"Insufficient flash: {free_mb} MB free, need {size_mb} MB")
            else:
                L("  WARN: could not parse free space from output")
        except SwitchError:
            raise
        except Exception as e:
            L(f"  WARN: free space check failed: {e}")

        # ---- Fast path: file already on flash with matching MD5 ----
        L(f"Running: dir flash:{dest_name}")
        try:
            existing = self.conn.send_command(f"dir flash:{dest_name}", read_timeout=20)
            L(f"  output: {existing.strip()[:200]}")
        except Exception as e:
            existing = ""
            L(f"  dir failed: {e}")

        if dest_name in existing and "No such file" not in existing and "Error" not in existing:
            L(f"Existing {dest_name} found on flash, verifying MD5")
            if on_progress: on_progress(3, "Found existing file, verifying MD5")
            L(f"Running: verify /md5 flash:{dest_name}")
            try:
                verify_out = self.conn.send_command(
                    f"verify /md5 flash:{dest_name}", read_timeout=300)
                L(f"  output tail: ...{verify_out.strip()[-120:]}")
                m = re.search(r"=\s*([0-9a-fA-F]{32})", verify_out)
                if m:
                    existing_md5 = m.group(1).lower()
                    L(f"  existing MD5 = {existing_md5}")
                    if expected_md5 and existing_md5 == expected_md5.lower():
                        L("  ✓ MD5 matches local — skipping transfer")
                        if on_progress: on_progress(100, "Already on flash, MD5 verified")
                        return f"{dest_name} already on flash (cached)"
                    else:
                        L("  ✕ MD5 mismatch — will delete and re-transfer")
                else:
                    L("  could not parse MD5 — will delete and re-transfer")
            except Exception as e:
                L(f"  verify failed: {e} — will delete and re-transfer")

            L(f"Running: delete /force flash:{dest_name}")
            try:
                del_out = self.conn.send_command_timing(
                    f"delete /force flash:{dest_name}", read_timeout=30)
                L(f"  output: {del_out.strip()[:120]}")
            except Exception as e:
                raise SwitchError(f"Could not delete stale flash:{dest_name}: {e}")

        # ---- Fresh SCP transfer with real progress ----
        # Pre-check: is SCP server actually enabled? Without this, paramiko's
        # channel-open request gets rejected with the misleading error
        # 'ChannelException(4, Resource shortage)'.
        L("Running: show running-config | include scp")
        try:
            scp_cfg = self.conn.send_command(
                "show running-config | include scp", read_timeout=20)
            L(f"  output: {scp_cfg.strip()[:200] or '(empty)'}")
            if "ip scp server enable" not in scp_cfg.lower():
                L("  SCP server is not enabled; enabling it for this transfer")
                try:
                    cfg_out = self.conn.send_config_set(
                        ["ip scp server enable"],
                        read_timeout=60,
                    )
                    L(f"  output: {cfg_out.strip()[-240:]}")
                    scp_cfg = self.conn.send_command(
                        "show running-config | include scp", read_timeout=20)
                    if "ip scp server enable" not in scp_cfg.lower():
                        raise SwitchError(
                            "SCP server did not appear enabled after configuration")
                except SwitchError:
                    raise
                except Exception as e:
                    raise SwitchError(
                        "SCP server is not enabled and automatic enable failed. "
                        "Run 'configure terminal / ip scp server enable / end' "
                        f"on the switch and retry. Raw error: {e}"
                    ) from e
            L("  ✓ SCP server is enabled")
        except SwitchError:
            raise
        except Exception as e:
            L(f"  WARN: could not check SCP config: {e}")

        L("Trying SSH bulk mode for large SCP transfer")
        try:
            bulk_out = self.conn.send_config_set(
                ["ip ssh bulk-mode"],
                read_timeout=30,
            )
            if "invalid input" in bulk_out.lower() or "incomplete command" in bulk_out.lower():
                L("  SSH bulk mode is not supported on this switch")
            else:
                L(f"  output: {bulk_out.strip()[-240:]}")
        except Exception as e:
            L(f"  WARN: could not configure SSH bulk mode: {e}")

        L(f"Starting SCP transfer of {dest_name}")
        if on_progress: on_progress(8, f"Starting SCP ({size_mb} MB)")

        # IMPORTANT: open a SEPARATE paramiko SSHClient for SCP rather than
        # reusing Netmiko's transport. The Netmiko session already has an
        # interactive shell channel open, and many IOS-XE trains refuse to
        # open a second exec channel on the same transport — paramiko reports
        # this as ChannelException(4, 'Resource shortage'), which is the same
        # error you get when SCP server is disabled.
        L("Opening dedicated SSH transport for SCP")
        import paramiko
        from scp import SCPClient, SCPException

        scp_ssh = paramiko.SSHClient()
        scp_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            scp_ssh.connect(
                hostname=self.device["host"],
                username=self.device["username"],
                password=self.device["password"],
                timeout=15,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as e:
            raise SwitchError(f"Could not open dedicated SSH for SCP: {e}") from e

        last_pct = [-1]
        last_report = [time.time()]
        last_log = [time.time()]
        last_sent = [0]

        # scp library signature: progress(filename, size, sent)
        # where size = total file size, sent = bytes transferred so far.
        # Previously had these swapped which caused bogus percentages.
        def progress_cb(filename, size, sent):
            now = time.time()
            last_sent[0] = sent
            pct = int((sent / size) * 100) if size else 0
            if pct != last_pct[0] or (now - last_report[0]) >= 1.0:
                last_pct[0] = pct
                last_report[0] = now
                sent_mb = sent // (1024 * 1024)
                if on_progress:
                    on_progress(pct, f"SCP {sent_mb}/{size_mb} MB")
            if (now - last_log[0]) >= 10.0 or pct in (0, 25, 50, 75, 100):
                last_log[0] = now
                L(f"  SCP progress: {sent:,}/{size:,} bytes ({pct}%)")

        try:
            with SCPClient(scp_ssh.get_transport(),
                            progress=progress_cb, socket_timeout=60) as scp:
                scp.put(local_path, f"flash:{dest_name}")
        except SCPException as e:
            raise SwitchError(f"SCP protocol error: {e}") from e
        except Exception as e:
            hint = ""
            if 0 < last_sent[0] <= 128 * 1024:
                hint = (
                    f" Transfer stopped after only {last_sent[0]:,} bytes. "
                    "That pattern usually points to flash filesystem trouble "
                    "or the switch SCP server closing the write; run fsck flash: "
                    "and retry, or inspect flash for filesystem errors."
                )
            raise SwitchError(f"SCP transport error: {type(e).__name__}: {e}.{hint}") from e
        finally:
            try: scp_ssh.close()
            except Exception: pass

        L("SCP put() returned, verifying size on flash")

        # ---- Strict post-transfer size check ----
        if on_progress: on_progress(99, "Verifying size on flash")
        L(f"Running: dir flash:{dest_name}")
        try:
            dir_out = self.conn.send_command(f"dir flash:{dest_name}", read_timeout=30)
            L(f"  output: {dir_out.strip()[:200]}")
        except Exception as e:
            raise SwitchError(f"Could not check file size after SCP: {e}") from e

        # Parse the size from 'dir' output: line like
        #   '20  -rw-  812345678  Apr 10 2026 16:15:23  cat9k_iosxe.17.15.04d.SPA.bin'
        size_match = re.search(r"(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+" + re.escape(dest_name),
                                 dir_out)
        if not size_match:
            # Fallback: just find a big number on the line with the filename
            for line in dir_out.splitlines():
                if dest_name in line:
                    nums = re.findall(r"\b(\d{6,})\b", line)
                    if nums:
                        size_match = type("M", (), {"group": lambda s, i: max(nums, key=int)})()
                        break
        if not size_match:
            raise SwitchError(
                f"Could not parse size of flash:{dest_name} from dir output. "
                f"Transfer may have failed silently. Raw: {dir_out.strip()[:300]}")

        flash_size = int(size_match.group(1))
        L(f"  flash size = {flash_size:,} bytes, local size = {size_bytes:,} bytes")
        if flash_size != size_bytes:
            raise SwitchError(
                f"Size mismatch after SCP: local={size_bytes}, flash={flash_size}. "
                f"Transfer was truncated or failed.")

        L(f"✓ SCP complete, {flash_size:,} bytes on flash")
        if on_progress: on_progress(100, "SCP complete")
        return f"{dest_name} transferred to flash: ({size_mb} MB)"

    def install_remove_inactive(self):
        """
        Run 'install remove inactive' to discard any staged-but-not-activated
        packages. Required before 'install add' can accept a new package if a
        previous add was left hanging by a failed/cancelled upgrade.

        The command prompts 'Do you want to remove the above files? [y/n]'
        which we auto-answer 'y'.
        """
        cmd = "install remove inactive"
        try:
            out = self.conn.send_command_timing(
                cmd, read_timeout=300, strip_prompt=False, strip_command=False)
            if "[y/n]" in out.lower() or "proceed" in out.lower() or "do you want" in out.lower():
                out += self.conn.send_command_timing(
                    "y", read_timeout=300, strip_prompt=False, strip_command=False)
            return out
        except Exception as e:
            raise SwitchError(f"install remove inactive failed: {e}") from e

    def install_add_activate_commit_fire_and_forget(self, image_filename,
                                                    source_interface=None,
                                                    on_log=None):
        """
        Send 'install add file X activate commit prompt-level none' over the
        paramiko channel WITHOUT waiting for the command to return.

        The install takes 2-5 minutes before the reload kicks in, which is
        longer than any sensible read timeout. Waiting for the prompt also
        risks the context manager tearing down SSH while the install is
        mid-flight, which aborts the whole operation.

        We write the command to the shell channel, then return immediately.
        The caller is responsible for detecting the reload (by probing SSH
        reachability) and reconnecting afterwards.
        """
        source_arg = image_filename
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source_arg):
            source_arg = f"flash:{image_filename}"

        def L(msg):
            if on_log:
                on_log(msg)

        source_interface = (source_interface or "").strip()
        if source_interface and source_arg.lower().startswith(("http:", "https:")):
            L(f"Configuring HTTP source-interface {source_interface}")
            try:
                out = self.conn.send_config_set(
                    [f"ip http client source-interface {source_interface}"],
                    read_timeout=30,
                )
                L(f"  output: {out.strip()[-240:]}")
            except Exception as e:
                raise SwitchError(
                    f"Could not configure HTTP source-interface {source_interface}: {e}") from e

        L("Saving running-config before install command")
        self.save_running_config(on_log=L)

        cmd = (f"install add file {source_arg} "
               f"activate commit prompt-level none\n")
        try:
            channel = self.conn.remote_conn
            channel.send(cmd)
            # Give the switch a beat to acknowledge the command. Don't wait
            # for the prompt — we're intentionally not reading output here.
            time.sleep(3)
            initial = ""
            try:
                while channel.recv_ready():
                    initial += channel.recv(65535).decode("utf-8", errors="replace")
                    time.sleep(0.2)
            except Exception:
                pass
            lower = initial.lower()
            if "% invalid" in lower or "error" in lower and "install" in lower:
                raise SwitchError(f"Install command failed immediately: {initial[-500:]}")
            if initial.strip():
                return f"install command sent: {cmd.strip()}\nInitial output:\n{initial[-1000:]}"
            return f"install command sent: {cmd.strip()}"
        except Exception as e:
            raise SwitchError(f"Could not send install command: {e}") from e

    def install_add_activate_commit_watch(self, image_filename,
                                          source_interface=None,
                                          on_log=None,
                                          on_progress=None,
                                          watch_timeout=7200):
        """
        Send install add/activate/commit and keep the SSH shell attached.

        Remote installs can spend many minutes in 'install_add: Adding IMG'
        before the reload begins. Keeping the channel open lets the UI show
        IOS-XE output instead of going dark while the switch is still working.
        """
        source_arg = image_filename
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source_arg):
            source_arg = f"flash:{image_filename}"

        cmd = f"install add file {source_arg} activate commit prompt-level none"
        return self._watch_install_command(
            cmd,
            source_arg=source_arg,
            source_interface=source_interface,
            on_log=on_log,
            on_progress=on_progress,
            watch_timeout=watch_timeout,
            progress_start=18,
            progress_end=67,
            progress_message="Install add/activate/commit in progress",
            done_hint="install_add",
        )

    def install_add_remote_watch(self, image_source,
                                 source_interface=None,
                                 on_log=None,
                                 on_progress=None,
                                 watch_timeout=7200):
        """Stage a local or remote IOS-XE image without activating it."""
        source_arg = image_source
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source_arg):
            source_arg = f"flash:{image_source}"
        cmd = f"install add file {source_arg}"
        return self._watch_install_command(
            cmd,
            source_arg=source_arg,
            source_interface=source_interface,
            on_log=on_log,
            on_progress=on_progress,
            watch_timeout=watch_timeout,
            progress_start=18,
            progress_end=58,
            progress_message="Install add in progress",
            done_hint="install_add",
            expect_reload=False,
        )

    def install_activate_commit_watch(self, on_log=None, on_progress=None,
                                      watch_timeout=7200):
        """Activate the staged package set and commit the install."""
        cmd = "install activate commit prompt-level none"
        return self._watch_install_command(
            cmd,
            on_log=on_log,
            on_progress=on_progress,
            watch_timeout=watch_timeout,
            progress_start=60,
            progress_end=68,
            progress_message="Install activate/commit in progress",
            done_hint="install_activate",
            expect_reload=True,
        )

    def install_diagnostics(self):
        """Collect install-related output after a failure."""
        commands = (
            "show install summary",
            "show install log",
            "show logging | include INSTALL|install|FAILED|Failed|failed|ERROR|Error|error",
        )
        sections = []
        for cmd in commands:
            try:
                out = self.conn.send_command(cmd, read_timeout=60)
            except Exception as e:
                out = f"{type(e).__name__}: {e}"
            sections.append(f"$ {cmd}\n{out.strip() or '(empty)'}")
        return "\n\n".join(sections)

    def _watch_install_command(self, cmd, source_arg=None,
                               source_interface=None,
                               on_log=None,
                               on_progress=None,
                               watch_timeout=7200,
                               progress_start=18,
                               progress_end=67,
                               progress_message="Install in progress",
                               done_hint="install",
                               expect_reload=True):
        def L(msg):
            if on_log:
                on_log(msg)

        source_arg = source_arg or ""
        source_interface = (source_interface or "").strip()
        if source_interface and source_arg.lower().startswith(("http:", "https:")):
            L(f"Configuring HTTP source-interface {source_interface}")
            try:
                out = self.conn.send_config_set(
                    [f"ip http client source-interface {source_interface}"],
                    read_timeout=30,
                )
                L(f"  output: {out.strip()[-240:]}")
            except Exception as e:
                raise SwitchError(
                    f"Could not configure HTTP source-interface {source_interface}: {e}") from e

        L("Saving running-config before install command")
        self.save_running_config(on_log=L)

        try:
            prompt = self.conn.find_prompt().strip()
        except Exception:
            prompt = "#"
        channel = self.conn.remote_conn
        try:
            channel.send(cmd + "\n")
        except Exception as e:
            raise SwitchError(f"Could not send install command: {e}") from e

        L(f"install command sent: {cmd}")
        if on_progress:
            on_progress(progress_start, "Install command accepted")

        start = time.time()
        last_status = start
        output = ""
        current_line = ""
        answered_prompt = False

        while time.time() - start < watch_timeout:
            try:
                if channel.recv_ready():
                    chunk = channel.recv(65535).decode("utf-8", errors="replace")
                    if not chunk:
                        time.sleep(1)
                        continue
                    output += chunk
                    text = chunk.replace("\r", "")
                    current_line += text
                    lines = current_line.split("\n")
                    current_line = lines.pop() if lines else ""
                    for line in lines:
                        clean = line.strip()
                        if clean:
                            L(f"install: {clean}")

                    lower_tail = output[-5000:].lower()
                    if (
                        "package verification fail" in lower_tail
                        and source_arg.lower().startswith(("http:", "https:"))
                    ):
                        raise SwitchError(
                            "Install package verification failed after remote HTTP download. "
                            "The image may be incompatible/corrupt, or the HTTP stream profile "
                            "may still be too aggressive for this switch. Retry once with the "
                            "updated balanced profile; if it repeats, set SWIM_HTTP_PROFILE=safe "
                            f"and retry. Tail:\n{output[-1200:]}"
                        )
                    if self._install_output_has_failure(lower_tail):
                        raise SwitchError(
                            f"Install command reported an error: {output[-1200:]}")

                    if (
                        not answered_prompt
                        and ("[y/n]" in lower_tail or "proceed" in lower_tail
                             or "continue" in lower_tail)
                    ):
                        L("install: answering confirmation prompt")
                        channel.send("y\n")
                        answered_prompt = True

                    if "reloading" in lower_tail or "reload" in lower_tail:
                        if on_progress:
                            on_progress(68, "Switch reload is starting")

                    if prompt and output[-1000:].rstrip().endswith(prompt) and done_hint in lower_tail:
                        return f"install command returned to prompt. Tail:\n{output[-1200:]}"

                if getattr(channel, "closed", False):
                    return "install SSH channel closed; switch may be reloading"

                now = time.time()
                if now - last_status >= 60:
                    elapsed = int(now - start)
                    mins = elapsed // 60
                    secs = elapsed % 60
                    L(f"install still running ({mins}m{secs}s elapsed)")
                    if on_progress:
                        pct = min(
                            progress_end,
                            progress_start + int(elapsed / 900 * (progress_end - progress_start)),
                        )
                        on_progress(pct, f"{progress_message} ({mins}m{secs}s)")
                    last_status = now
                time.sleep(1)
            except SwitchError:
                raise
            except Exception as e:
                return f"install session ended while command was running; switch may be reloading: {e}"

        raise SwitchError(
            f"Install command did not finish or trigger reload within {watch_timeout}s. "
            f"Tail:\n{output[-1200:]}")

    @staticmethod
    def _install_output_has_failure(lower_tail):
        failure_patterns = (
            r"%\s*invalid",
            r"error opening",
            r"not enough space",
            r"\bfailed\b",
            r"\berror:",
            r"\babort(?:ed)?\b",
        )
        return any(re.search(pattern, lower_tail) for pattern in failure_patterns)

    def install_add_activate_commit(self, image_filename):
        """
        Run the combined 'install add file X activate commit prompt-level none'.
        This is the scripted-upgrade form — bypasses ISSU compatibility check
        (which fails for large version jumps like 17.12 -> 17.15) and uses
        the traditional reload-based upgrade path instead.

        'prompt-level none' is CRITICAL: without it the switch waits for a
        y/n confirmation that our fire-and-forget code can't answer.

        The command triggers a reload several minutes in, so we'll lose the
        session mid-command. That's expected.
        """
        cmd = (f"install add file flash:{image_filename} "
               f"activate commit prompt-level none")
        try:
            # Long read timeout for the add phase (2-5 min), then the
            # activate triggers reload and the session dies.
            out = self.conn.send_command_timing(
                cmd, read_timeout=600, strip_prompt=False, strip_command=False)
            return out
        except Exception as e:
            # Session death during reload is expected, not an error
            return f"install add_activate_commit issued, session died as expected: {e}"

    def install_add(self, image_filename):
        """
        Run 'install add file flash:<image>' and wait for completion.
        This phase copies the image into the install workspace but does NOT
        activate or reload. Takes 1-3 minutes.
        """
        cmd = f"install add file flash:{image_filename}"
        try:
            out = self.conn.send_command_timing(
                cmd, read_timeout=600, strip_prompt=False, strip_command=False)
            # Some trains prompt to proceed; others don't.
            if "proceed" in out.lower() or "[y/n]" in out.lower():
                out += self.conn.send_command_timing(
                    "y", read_timeout=600, strip_prompt=False, strip_command=False)
            return out
        except Exception as e:
            raise SwitchError(f"install add failed: {e}") from e

    def install_activate(self, image_filename):
        """
        Run 'install activate file flash:<image> prompt-level none'.
        The 'prompt-level none' is CRITICAL — without it the switch will
        sit waiting for a y/n confirmation that a scripted session can't
        answer, and the reload never happens.

        The activate phase triggers a reload, so this call will lose its
        session mid-command. We treat that as success (as with the single
        fire-and-forget command elsewhere).
        """
        cmd = f"install activate file flash:{image_filename} prompt-level none"
        try:
            out = self.conn.send_command_timing(
                cmd, read_timeout=120, strip_prompt=False, strip_command=False)
            return out
        except Exception as e:
            # Session death here is expected because the switch reloads
            return f"install activate issued, session died as expected: {e}"

    def upgrade_install_mode(self, image_filename):
        """
        Issue 'install add file ... activate commit' and return without waiting
        for the switch to finish — it will reload mid-command, so we can't get
        a clean prompt back. Caller is responsible for the reload-wait loop.

        We use a short read_timeout and swallow timeouts because the lack of
        a returning prompt is the expected outcome here, not an error.
        """
        cmd = f"install add file flash:{image_filename} activate commit"
        try:
            # Send the command, then immediately try to confirm any y/n prompt
            # that appears within the first 30s. After that, return — the
            # switch is committed to the install and will reload on its own.
            out = self.conn.send_command_timing(
                cmd, read_timeout=120, strip_prompt=False, strip_command=False)
            if "proceed" in out.lower() or "[y/n]" in out.lower() or "[y/n/q]" in out.lower():
                try:
                    out += self.conn.send_command_timing(
                        "y", read_timeout=120, strip_prompt=False, strip_command=False)
                except Exception:
                    pass  # session likely died because the install kicked off
            return out
        except Exception as e:
            # A read timeout here is actually fine — install is running, the
            # session just won't return. Only re-raise if the connection itself
            # is broken (which is also fine because we're about to reload).
            return f"install command issued, no prompt returned: {e}"


def ping_host(host, timeout=2):
    """Returns True if host responds to a single ping within timeout."""
    import subprocess
    try:
        # -c 1 for one ping, -W for timeout. macOS/Linux compatible.
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout * 1000), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except Exception:
        # Try macOS -W (seconds instead of milliseconds)
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-t", str(timeout), host],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout + 2,
            )
            return result.returncode == 0
        except Exception:
            return False


def probe_ssh(host, timeout=5):
    """Returns True if TCP 22 accepts a connection. Quick, no auth."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, 22))
        s.close()
        return True
    except Exception:
        return False


def wait_for_reload(host, on_tick=None, going_down_timeout=600,
                     coming_back_timeout=3600):
    """
    Two-phase reload detector:
      Phase A: wait for the switch to STOP responding (reload has started).
               If the switch never becomes unreachable within going_down_timeout,
               raises SwitchError — the install probably didn't trigger a reload.
      Phase B: wait for the switch to START responding again (reload complete).

    on_tick(phase, elapsed, note) fires every probe so callers can update UI.
    """
    # Phase A: wait for the switch to become unreachable
    start = time.time()
    deadline_a = start + going_down_timeout
    consecutive_failures = 0
    while time.time() < deadline_a:
        up = ping_host(host) or probe_ssh(host, timeout=3)
        if up:
            consecutive_failures = 0
            if on_tick:
                on_tick("going_down", int(time.time() - start),
                         "switch still responding")
            time.sleep(5)
        else:
            consecutive_failures += 1
            if on_tick:
                on_tick("going_down", int(time.time() - start),
                         f"no response ({consecutive_failures}/3)")
            # Require 3 consecutive failures to avoid being fooled by a
            # brief network blip.
            if consecutive_failures >= 3:
                break
            time.sleep(3)
    else:
        raise SwitchError(
            f"Switch never went unreachable within {going_down_timeout}s — "
            f"the install command may not have triggered a reload. "
            f"Check 'show install summary' on the switch.")

    # Phase B: wait for it to come back
    phase_b_start = time.time()
    deadline_b = phase_b_start + coming_back_timeout
    while time.time() < deadline_b:
        if probe_ssh(host, timeout=5):
            # SSH port is open — give sshd a few more seconds to fully init
            time.sleep(10)
            return
        if on_tick:
            on_tick("coming_back", int(time.time() - phase_b_start),
                     "probing SSH")
        time.sleep(15)
    raise SwitchError(
        f"Switch did not come back within {coming_back_timeout}s after reload")


def wait_for_reachable(host, username, password, secret, timeout_seconds=4500,
                        on_tick=None):
    """
    Poll SSH connectivity after a reload. Default 75 minutes — install-mode
    upgrades on a chassis can take a full hour before the switch is back.
    on_tick(elapsed_seconds, deadline_seconds) is called between attempts so
    callers can update progress UI.
    """
    start = time.time()
    deadline = start + timeout_seconds
    last_err = "unknown"
    while time.time() < deadline:
        try:
            with SwitchClient(host, username, password, secret):
                return
        except SwitchError as e:
            last_err = str(e)
            if on_tick:
                try: on_tick(int(time.time() - start), timeout_seconds)
                except Exception: pass
            time.sleep(20)
    raise SwitchError(f"Switch did not come back within {timeout_seconds}s: {last_err}")
