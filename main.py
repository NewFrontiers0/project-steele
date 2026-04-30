"""Meraki Catalyst onboarder — gated stages with firmware upgrade."""
from __future__ import annotations

import hmac
import os
import time
from typing import List, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from jobs import FIRMWARE_DIR, store, list_firmware_files
from meraki_client import MerakiClient, MerakiError
from discovery import scan_store
from firmware_downloads import firmware_downloads
from switch import SwitchClient, SwitchError
from swim import swim_store
from swim_file_server import http_streaming_profile


def _open_firmware_file(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename or safe_name not in list_firmware_files():
        raise HTTPException(status_code=404, detail="Firmware file not found")
    path = os.path.join(FIRMWARE_DIR, safe_name)
    headers = {
        "Cache-Control": "no-store",
        "Connection": "close",
        "Content-Length": str(os.path.getsize(path)),
        "Content-Type": "application/octet-stream",
    }
    return safe_name, path, headers


def _firmware_chunks(path: str):
    profile = http_streaming_profile()
    chunk_bytes = int(profile["chunk_bytes"])
    chunk_delay = float(profile["chunk_delay_ms"]) / 1000
    initial_delay = float(profile["initial_delay_ms"]) / 1000
    if initial_delay > 0:
        time.sleep(initial_delay)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_bytes)
            if not chunk:
                break
            yield chunk
            if chunk_delay > 0:
                time.sleep(chunk_delay)


class ScanRequest(BaseModel):
    subnet: str = Field(..., examples=["192.0.2.0/24"])
    username: str
    password: str
    secret: Optional[str] = None


class DeviceRow(BaseModel):
    host: str
    network_id: str = ""


class CreateRunRequest(BaseModel):
    username: str
    password: str
    secret: Optional[str] = None
    mode: Literal["cloud", "device"]
    default_network_id: str
    devices: List[DeviceRow] = Field(..., min_length=1)


class UpgradeRequest(BaseModel):
    image_filename: str


class FirmwareRetryRequest(BaseModel):
    filename: Optional[str] = None
    force: bool = False


class FirmwareSourceRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    scheme: Literal["http", "https"] = "http"


class CliCommandRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)
    command: str = Field(..., min_length=1, max_length=4000)
    secret: Optional[str] = Field(default=None, max_length=255)
    read_timeout: int = Field(default=60, ge=5, le=300)


class SwimUpgradeRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)
    image_filename: str = Field(..., min_length=1, max_length=255)
    secret: Optional[str] = Field(default=None, max_length=255)
    app_base_url: Optional[str] = Field(default=None, max_length=2048)
    copy_vrf: Optional[str] = Field(default=None, max_length=255)
    source_interface: Optional[str] = Field(default=None, max_length=255)
    transfer_method: Literal["install-http", "tftp", "ftp", "ftp-active", "ftp-passive", "http", "scp"] = "install-http"
    repair_flash: bool = True
    allow_downgrade: bool = False


class RunResponse(BaseModel):
    run_id: str


class NetworkOption(BaseModel):
    id: str
    name: str
    organization_id: str
    product_types: List[str]


class OrganizationOption(BaseModel):
    id: str
    name: str


app = FastAPI(title="Meraki Catalyst Onboarder")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _check_config():
    if os.environ.get("MERAKI_API_KEY"):
        print("INFO: MERAKI_API_KEY is set, but browser session keys are used for API requests")
    firmware_downloads.start_from_env()


def _get_api_key(
    x_meraki_api_key: Optional[str] = Header(default=None, alias="X-Meraki-API-Key"),
) -> str:
    api_key = (x_meraki_api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Meraki API key is required")
    return api_key


def _get_org_id(
    x_meraki_org_id: Optional[str] = Header(default=None, alias="X-Meraki-Org-Id"),
) -> str:
    org_id = (x_meraki_org_id or "").strip()
    if not org_id:
        raise HTTPException(status_code=401, detail="Meraki organization is required")
    return org_id


def _meraki_error_status(error: MerakiError) -> int:
    message = str(error).lower()
    if "invalid api key" in message or "unauthorized" in message or "401" in message:
        return 401
    return 502


@app.post("/api/login")
def login(api_key: str = Depends(_get_api_key)):
    try:
        organizations = MerakiClient(api_key).list_organizations()
    except MerakiError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if not organizations:
        raise HTTPException(status_code=403, detail="No Meraki organizations are available for this API key")
    return {"ok": True, "organizations": [OrganizationOption(**org) for org in organizations]}


@app.post("/api/run", response_model=RunResponse)
def create_run(
    req: CreateRunRequest,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    rows = [{"host": d.host.strip(),
             "network_id": (d.network_id or req.default_network_id).strip()}
            for d in req.devices if d.host.strip()]
    if not rows:
        raise HTTPException(status_code=400, detail="No devices in request")
    run = store.create_run(rows, req.mode, req.username, req.password, req.secret, api_key, org_id)
    return RunResponse(run_id=run.id)


def _get_run(run_id, api_key: Optional[str] = None, org_id: Optional[str] = None):
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if api_key is not None and not hmac.compare_digest(run.api_key, api_key):
        raise HTTPException(status_code=403, detail="API key does not match this run")
    if org_id is not None and not hmac.compare_digest(run.org_id, org_id):
        raise HTTPException(status_code=403, detail="Organization does not match this run")
    return run


@app.post("/api/run/{run_id}/precheck")
def trigger_precheck(
    run_id: str,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    store.run_precheck_stage(_get_run(run_id, api_key, org_id))
    return {"started": True}


@app.post("/api/run/{run_id}/upgrade")
def trigger_upgrade(
    run_id: str,
    req: UpgradeRequest,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    if req.image_filename not in list_firmware_files():
        raise HTTPException(status_code=400, detail="Image not in firmware folder")
    store.run_upgrade_stage(_get_run(run_id, api_key, org_id), req.image_filename)
    return {"started": True}


@app.post("/api/run/{run_id}/apply-prereqs")
def trigger_apply_prereqs(
    run_id: str,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    store.run_apply_prereqs_stage(_get_run(run_id, api_key, org_id))
    return {"started": True}


@app.post("/api/run/{run_id}/enable")
def trigger_enable(
    run_id: str,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    store.run_enable_stage(_get_run(run_id, api_key, org_id))
    return {"started": True}


@app.post("/api/run/{run_id}/claim")
def trigger_claim(
    run_id: str,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    store.run_claim_stage(_get_run(run_id, api_key, org_id))
    return {"started": True}


@app.get("/api/run/{run_id}")
def get_run(
    run_id: str,
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    return store.serialize(_get_run(run_id, api_key, org_id))


@app.get("/api/firmware")
def get_firmware():
    return {"images": list_firmware_files(), "downloads": firmware_downloads.serialize()}


@app.get("/api/firmware/downloads")
def get_firmware_downloads():
    return firmware_downloads.serialize()


@app.post("/api/firmware/downloads/retry")
def retry_firmware_downloads(req: FirmwareRetryRequest):
    try:
        return firmware_downloads.retry(req.filename, force=req.force)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/firmware/downloads/rescan")
def rescan_firmware_downloads():
    return firmware_downloads.rescan()


@app.post("/api/firmware/downloads/source")
def set_firmware_download_source(req: FirmwareSourceRequest):
    try:
        return firmware_downloads.set_source(req.host, req.port, req.scheme)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/cli/run")
def run_cli_command(
    req: CliCommandRequest,
    _api_key: str = Depends(_get_api_key),
    _org_id: str = Depends(_get_org_id),
):
    commands = [line.strip() for line in req.command.splitlines() if line.strip()]
    if not commands:
        raise HTTPException(status_code=400, detail="Enter at least one CLI command")
    if len(commands) > 25:
        raise HTTPException(status_code=400, detail="Run 25 commands or fewer at a time")
    try:
        with SwitchClient(req.host.strip(), req.username.strip(), req.password, req.secret) as sw:
            results = sw.run_cli_commands(commands, read_timeout=req.read_timeout)
    except SwitchError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "host": req.host.strip(), "results": results}


@app.post("/api/swim/upgrade")
def start_swim_upgrade(
    req: SwimUpgradeRequest,
    _api_key: str = Depends(_get_api_key),
    _org_id: str = Depends(_get_org_id),
):
    try:
        job = swim_store.start_upgrade(
            req.host,
            req.username,
            req.password,
            req.secret,
            req.image_filename,
            req.app_base_url,
            req.copy_vrf,
            req.source_interface,
            req.transfer_method,
            req.repair_flash,
            req.allow_downgrade,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return swim_store.serialize(job)


@app.get("/api/swim/upgrade/{job_id}")
def get_swim_upgrade(
    job_id: str,
    _api_key: str = Depends(_get_api_key),
    _org_id: str = Depends(_get_org_id),
):
    job = swim_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="SWIM job not found")
    return swim_store.serialize(job)


@app.api_route("/firmware-files/{filename}", methods=["GET", "HEAD"])
def serve_firmware_file(filename: str, request: Request):
    _safe_name, path, headers = _open_firmware_file(filename)
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)
    return StreamingResponse(
        _firmware_chunks(path),
        media_type="application/octet-stream",
        headers=headers,
    )


@app.api_route("/f/{filename}", methods=["GET", "HEAD"])
def stream_firmware_file(filename: str, request: Request):
    _safe_name, path, headers = _open_firmware_file(filename)
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)
    return StreamingResponse(
        _firmware_chunks(path),
        media_type="application/octet-stream",
        headers=headers,
    )


@app.get("/api/networks", response_model=List[NetworkOption])
def list_networks(
    api_key: str = Depends(_get_api_key),
    org_id: str = Depends(_get_org_id),
):
    try:
        return [NetworkOption(**n) for n in MerakiClient(api_key).list_switch_networks(org_id)]
    except MerakiError as e:
        raise HTTPException(status_code=_meraki_error_status(e), detail=str(e))


@app.post("/api/scan")
def start_scan(req: ScanRequest):
    try:
        scan = scan_store.start_scan(req.subnet, req.username, req.password, req.secret)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"scan_id": scan.id}


@app.get("/api/scan/{scan_id}")
def get_scan(scan_id: str):
    scan = scan_store.get(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan_store.serialize(scan)


_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
