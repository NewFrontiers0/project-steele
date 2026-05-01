"""Meraki SDK wrapper. The API key is supplied per-instance from the request
header — no env-var fallback. Each request creates a fresh MerakiClient with
the user's session key, validated at login time."""
from __future__ import annotations

from typing import Iterator, List, Optional

import meraki

try:
    from meraki.exceptions import APIError as MerakiApiError
except Exception:
    MerakiApiError = getattr(meraki, "APIError", None)

if MerakiApiError is None:
    class MerakiApiError(Exception):
        pass


class MerakiError(Exception):
    pass


class MerakiClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise MerakiError("API key is required")
        try:
            self.dashboard = meraki.DashboardAPI(
                api_key=api_key,
                suppress_logging=True,
            )
        except Exception as e:
            raise MerakiError(f"Could not initialize dashboard client: {e}") from e

    def validate(self) -> bool:
        """Hit /organizations as a cheap auth check. Raises if the key is bad."""
        self.list_organizations()
        return True

    def list_organizations(self):
        orgs = []
        try:
            for org in self.dashboard.organizations.getOrganizations():
                orgs.append({"id": org["id"], "name": org["name"]})
        except MerakiApiError as e:
            raise MerakiError(f"Invalid API key: {e}") from e
        except Exception as e:
            raise MerakiError(f"Could not reach dashboard API: {e}") from e
        return sorted(orgs, key=lambda org: org["name"].lower())

    def list_switch_networks(self, organization_id: str):
        out = []
        try:
            networks = self.dashboard.organizations.getOrganizationNetworks(
                organization_id, total_pages="all")
        except MerakiApiError as e:
            raise MerakiError(f"Failed to list networks for organization {organization_id}: {e}") from e
        for n in networks:
            if "switch" not in n.get("productTypes", []):
                continue
            out.append({"id": n["id"], "name": n["name"],
                        "organization_id": organization_id,
                        "product_types": n.get("productTypes", [])})
        return out

    def claim_into_network(self, network_id, cloud_id, mode,
                            username=None, password=None, secret=None):
        api_mode = "managed" if mode == "cloud" else "monitored"
        details = [{"name": "device mode", "value": api_mode}]
        if mode == "device":
            if not username or not password:
                raise MerakiError("Device mode requires switch credentials")
            details.append({"name": "username", "value": username})
            details.append({"name": "password", "value": password})
            if secret:
                details.append({"name": "enable password", "value": secret})
        try:
            self.dashboard.networks.claimNetworkDevices(
                network_id, serials=[cloud_id], addAtomically=True,
                detailsByDevice=[{"serial": cloud_id, "details": details}])
        except MerakiApiError as e:
            raise MerakiError(f"Claim failed: {e}") from e

    def update_device_name(self, serial, name):
        try:
            self.dashboard.devices.updateDevice(serial, name=name)
        except MerakiApiError as e:
            raise MerakiError(f"Set name failed: {e}") from e
