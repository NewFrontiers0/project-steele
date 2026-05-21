# Meraki Catalyst Onboarder

Sibling to `meraki-onboarder` (the single-device wizard). Same Netmiko + Meraki SDK plumbing, but onboards a list of switches in parallel and shows live status in a job table.

## Quick start

```bash
unzip meraki-onboarder-bulk.zip
cd meraki-onboarder-bulk
./run.sh
```

Open <http://127.0.0.1:8001> on the machine running the app, or `http://<machine-ip>:8001` from another device on the LAN. Create a local project-steele profile, link your dashboard API key once, then sign in with username/password and choose the organization you want to work in. The bulk app runs on **port 8001** so you can run it alongside the single-device app (which runs on 8000).

`./run.sh` automatically stops existing processes listening on the app port and the SWIM transfer ports before starting. To disable that behavior, run `KILL_PORT_LISTENER=0 ./run.sh`. To use different ports, run `PORT=8002 SWIM_FILE_PORT=9001 SWIM_FTP_PORT=2122 ./run.sh`. TFTP uses standard UDP/69 because Cisco IOS-XE rejects TFTP URLs with explicit ports. On Linux, `./run.sh` automatically grants the local Python runtime permission to bind UDP/69 and may prompt for `sudo` once; Docker works without that extra step.

## Versioning

Project Steele uses semantic versioning. The current app version is stored in
`VERSION` and shown on the login screen and top bar. For normal releases, bump
the version with the code change:

- patch, such as `1.0.1`, for fixes
- minor, such as `1.1.0`, for backward-compatible features
- major, such as `2.0.0`, for breaking changes

The `/api/version` endpoint returns the app version plus the current Git build
SHA when the app is running from a Git checkout.

## Docker

A container build is included. Firmware images and local profile data live in named volumes, and the app runs as-is.

### Build

```bash
docker build -t catalyst-onboarder .
```

### Run — one-shot with docker run

```bash
docker run -d \
    --name catalyst-onboarder \
    -p 8001:8001 \
    -p 9000:9000 \
    -p 2121:2121 \
    -p 69:69/udp \
    -p 30000-30009:30000-30009 \
    -v catalyst-firmware:/app/firmware \
    -v project-steele-profiles:/app/data \
    --restart unless-stopped \
    catalyst-onboarder
```

Then open <http://localhost:8001>.

### Run — with docker compose

```bash
docker compose up -d
```

Leave `FIRMWARE_URLS` blank to choose the firmware source host and port in the web UI. Set it only when you want unattended startup downloads. Local profiles are stored in the `profile-data` volume by default.

### Firmware images

Open **Tools -> Firmware downloads**, enter the HTTP hostname/IP and port that lists your firmware files, then click **Find firmware**. Any listed or linked filename that starts with `cat9k` and ends with `.bin` is queued for download.

`FIRMWARE_URLS` is an optional space-separated unattended startup source. Set it to `none` to disable automatic firmware downloads. If it contains a repository URL or a legacy URL to a `cat9k*.bin` file, the app scans that repository and queues every matching `cat9k*.bin` image. On server startup, the FastAPI app checks each image against the firmware folder or volume; if the file already exists with non-zero size it's skipped, otherwise it's downloaded in the background and shown in the top-right firmware downloads panel. The Docker named volume persists across restarts so you pay the multi-GB download cost once per image version.

If you'd rather populate the volume another way, set `FIRMWARE_URLS=none` and use `docker cp` or pre-seed via a bind mount.

If an older `.env` still points at one specific `cat9k*.bin` file, the app treats that as a repository hint and scans the parent repository for all matching `cat9k*.bin` files.

Failed downloads can be retried from the firmware downloads panel. Completed or cached downloads can be restarted from the same panel, which removes the existing file and downloads it again.

### Network reachability

The container needs to reach both the internet (for `api.meraki.com`) and your switch management network. On Linux with `--network host` both work without config. On Docker Desktop (macOS/Windows), the default bridge network's NAT usually handles it; verify with `docker exec catalyst-onboarder ping <switch-management-ip>`.

### Logs

```bash
docker logs -f catalyst-onboarder
```

Firmware-download progress and errors are shown in the web UI.

## Ubuntu systemd service

For an always-on Ubuntu VM, run Project Steele with systemd instead of `./run.sh`.
The service runs uvicorn without `--reload`, and a timer checks GitHub `main`
every 5 minutes. When `main` changes, the updater pulls the new code, updates
Python dependencies, refreshes the systemd unit files, and restarts the app.

### 1. Add a deploy key for the private repo

On the Ubuntu VM:

```bash
sudo useradd --system --create-home --shell /bin/bash steele 2>/dev/null || true
sudo -u steele mkdir -p /home/steele/.ssh
sudo -u steele ssh-keygen -t ed25519 -f /home/steele/.ssh/project-steele -C project-steele-deploy
sudo -u steele cat /home/steele/.ssh/project-steele.pub
```

In GitHub, open **Settings -> Deploy keys** for `project-steele`, add that public
key, and leave write access disabled. Then create SSH config for the service user:

```bash
sudo -u steele tee /home/steele/.ssh/config >/dev/null <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile /home/steele/.ssh/project-steele
  IdentitiesOnly yes
EOF
sudo chmod 600 /home/steele/.ssh/config
sudo -u steele ssh -T git@github.com
```

The final SSH test should identify the GitHub account or repo access. It may say
shell access is not provided; that is normal.

### 2. Clone and install the service

```bash
sudo apt-get update
sudo apt-get install -y git
sudo mkdir -p /opt/project-steele
sudo chown steele:steele /opt/project-steele
sudo -u steele git clone git@github.com:NewFrontiers0/project-steele.git /opt/project-steele
cd /opt/project-steele
sudo ./deploy/install-systemd.sh
```

The app will listen on `http://<vm-ip>:8001`. The installer also enables the
update timer.

### 3. Operate it

```bash
sudo systemctl status project-steele.service
sudo journalctl -u project-steele.service -f
sudo systemctl restart project-steele.service
sudo systemctl list-timers project-steele-update.timer
sudo systemctl start project-steele-update.service
```

Local runtime settings live in `/opt/project-steele/.env`. Do not commit that file.

The update job refuses to deploy if tracked files on the VM have local edits. That
keeps accidental server-side changes from being overwritten silently. Commit changes
through Git branches and merge them to `main`; the VM will pick them up on the next
timer run.

### Switch CLI

Use **Tools -> CLI** to open a dedicated SSH command page. Enter one or more switch IPs or hostnames, username, password, and one or more CLI commands. The backend uses the same Netmiko IOS-XE SSH wrapper as the onboarding workflow, runs across switches in parallel, and returns per-switch command output in the page. Set `CLI_MAX_PARALLEL` to tune concurrent SSH sessions; the default is 10 and the app caps it at 20.

### SWIM

Use **Tools -> SWIM** for single-switch firmware management. Pick one of the downloaded `cat9k*.bin` firmware images, choose a transfer method, enter the switch IP plus SSH credentials, and start the upgrade. Firmware images are sorted newest-first. SWIM warns before downgrades/reinstalls, but you can run one intentionally by enabling **Allow downgrade or reinstall**. The backend follows IOS-XE install-mode workflow: MD5 check, switch-initiated copy from the app to flash, flash verification, inactive-package cleanup, `install add ... activate commit`, reload monitoring, reconnect, and final install commit.

SWIM now defaults to **Direct install HTTP** because HTTP/FTP/SCP copies to `flash:` can fail after the first data window on some IOS-XE switches. Direct install sends `install add file http://...` first, then `install activate commit prompt-level none` after the add phase succeeds, letting the IOS-XE install subsystem pull and process the remote image instead of staging the whole `.bin` with a separate copy command first. Before IOS-XE install commands, SWIM saves the running config because install mode refuses to proceed when the system configuration is modified. TFTP, SCP, FTP active/passive, and HTTP copy remain selectable fallbacks. TFTP uses UDP/69, FTP uses TCP/2121 for control, passive FTP uses TCP/30000-30009 by default, and HTTP uses TCP/9000 by default with byte-range support for IOS-XE download clients.

HTTP firmware serving uses a named streaming profile. The default `balanced` profile now uses a short 256 KB slow-start phase, then accelerates to 8 KB writes with an intermediate MSS, a bounded send buffer, and TCP_NOTSENT_LOWAT where Linux supports it. This avoids both the original immediate VM burst that IOS-XE rejected and the aggressive profile that could make package verification fail after download. If a switch regresses, set `SWIM_HTTP_PROFILE=safe` in `/opt/project-steele/.env` to roll back to the original 512-byte / 5 ms profile, then restart the service. Use `SWIM_HTTP_PROFILE=fast` only on clean lab networks. Use `SWIM_HTTP_PROFILE=custom` if you want `SWIM_HTTP_CHUNK_BYTES`, `SWIM_HTTP_CHUNK_DELAY_MS`, `SWIM_HTTP_ACCELERATE_AFTER_BYTES`, `SWIM_HTTP_ACCELERATED_CHUNK_BYTES`, `SWIM_HTTP_ACCELERATED_CHUNK_DELAY_MS`, `SWIM_HTTP_INITIAL_DELAY_MS`, `SWIM_HTTP_TCP_MAXSEG`, `SWIM_HTTP_SNDBUF_BYTES`, and `SWIM_HTTP_TCP_NOTSENT_LOWAT` to be read directly.

During TFTP, SCP, FTP, and direct HTTP transfers, the app logs byte progress and updates the SWIM progress bar while the image is transferring. Direct install keeps the SSH command session attached and streams IOS-XE install output until the reload starts or the command fails. If FTP starts but no bytes move for 5 minutes, SWIM aborts the stalled copy so the job can fail cleanly instead of hanging indefinitely.

SWIM runs `fsck flash:` before the image transfer by default. This can be disabled from the SWIM page, but leaving it enabled is useful when transfers fail after the first small chunk because that often points to flash filesystem trouble.

For switches that use a management VRF, set **Copy VRF** to the management VRF name, commonly `Mgmt-vrf`, and set **Transfer source interface** to the management SVI or interface. The switch must have a route from that VRF/interface to the app host on the chosen transfer ports.

### Latency

Use **Tools -> Latency** to run a parallel latency test from the project-steele host. The first tile is the local default next-hop gateway detected from the app host routing table. The global tiles use public RIPE Atlas anchor IPv4 targets and show average RTT, packet loss, and target IP. Set `LATENCY_MAX_PARALLEL` to tune concurrent probes; the default is 12.

## How it works

1. Create or sign in to a local project-steele profile. Profiles store a salted password hash plus the linked dashboard API key in `PROJECT_STEELE_USERS_FILE`.
2. Select the organization for this onboarding run.
3. Enter a shared username + password and pick a default target network.
4. Add device rows (just the management IP for each). Optionally override the target network per row — leave blank to use the default.
5. Hit **Create run & precheck**. The backend returns a `run_id` immediately and the page switches to a live status table.
6. The frontend polls `/api/run/{id}` every 1.5s. Each row shows stage, status, parsed version, compatibility check, parsed cloud ID, and any error.

## Concurrency & failure model

- **Up to 5 sessions in parallel** — capped by a thread pool + semaphore in `jobs.py` (`MAX_PARALLEL = 5`). Adjust if your TACACS, link, or dashboard rate limits allow more.
- **Keep-going on failure** — if switch #17 fails its precheck, the rest of the batch continues. Failed jobs are marked red in the table with the error message.
- **Stages per device** — `precheck → enable → claim`. A failure at any stage marks the job failed and skips the rest of its stages, but doesn't affect siblings.

## Endpoints

| Method | Path                | Purpose                                            |
|--------|---------------------|----------------------------------------------------|
| POST   | `/api/auth/register` | Create a local profile and link a dashboard API key |
| POST   | `/api/auth/login`   | Sign in with username/password and list accessible organizations |
| POST   | `/api/auth/logout`  | Invalidate the current browser session token |
| POST   | `/api/login`        | Legacy API-key validation endpoint |
| GET    | `/api/version`      | App semantic version and Git build SHA            |
| POST   | `/api/run`          | Start a batch — returns `run_id`                  |
| GET    | `/api/run/{id}`     | Live job status — frontend polls this             |
| GET    | `/api/networks`     | Network dropdown for the selected organization, filtered to switch networks |
| GET    | `/api/firmware/downloads` | Firmware URL download status for the top-right panel |
| POST   | `/api/firmware/downloads/source` | Set the firmware repository host and port from the web UI |
| POST   | `/api/firmware/downloads/retry` | Retry failed firmware downloads, or restart one file with `force: true` |
| POST   | `/api/firmware/downloads/rescan` | Rescan the firmware repository for cat9k*.bin files |
| POST   | `/api/cli/run`     | Run CLI command(s) on one or more switches over SSH |
| POST   | `/api/swim/upgrade` | Start a single-switch SWIM firmware upgrade        |
| GET    | `/api/swim/upgrade/{id}` | Poll SWIM upgrade status, progress, and log output |
| GET    | `/api/latency/targets` | List the local gateway/global latency targets |
| POST   | `/api/latency/run` | Run a parallel latency probe across all targets |
| FTP    | `ftp://<app-host>:2121/{filename}` | SWIM firmware FTP transfer endpoint |
| TFTP   | `tftp://<app-host>/{filename}` | SWIM firmware TFTP transfer endpoint on UDP/69 |
| GET    | `http://<app-host>:9000/{filename}` | Optional dedicated plain HTTP firmware endpoint used by SWIM |
| GET    | `/f/{filename}` | Plain streaming firmware endpoint retained for browser/app testing |
| GET    | `/firmware-files/{filename}` | Browser-friendly firmware endpoint |

## Layout

```
meraki-onboarder-bulk/
  main.py            FastAPI app — endpoints
  jobs.py            Job store, thread pool, stage runner
  firmware_downloads.py  Background firmware URL download tracker
  swim.py            Single-switch SWIM firmware upgrade jobs
  swim_ftp_server.py  Read-only FTP server for SWIM image pulls
  swim_file_server.py  Dedicated plain HTTP server for SWIM image pulls
  profiles.py        Local username/password profiles and session tokens
  latency.py         Global latency probe targets and ping runner
  switch.py          Netmiko wrapper (shared with single-device app)
  meraki_client.py   Meraki SDK wrapper (shared)
  static/index.html  Bulk frontend with live job table
  requirements.txt
  .env.example
  run.sh
```

## Production notes

- **In-memory job store.** Runs disappear on restart. For real ops, swap `JobStore` for Redis or Postgres — the dataclasses are already JSON-serializable via `asdict`.
- **Single-process only.** The module-level `store` singleton means you can't run uvicorn with `--workers > 1` until you move state out of process.
- **Local profile auth only.** Passwords are salted and hashed, browser sessions use random tokens, and dashboard API keys are stored only in the local profile JSON file. Put the app behind your normal auth proxy before exposing it outside a trusted network.
- **Firmware upgrades are long-running.** Keep the app process alive while a bulk upgrade or SWIM job is running; in-memory status disappears if the server restarts.
