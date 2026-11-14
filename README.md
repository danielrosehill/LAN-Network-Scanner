# LAN-Network-Scanner

Small HTTP API on **residencehome** that scans one page from the **HP DeskJet 5200** flatbed
(`10.0.0.72`) over eSCL and keeps the files for 30 days. It replaces the scan half of the
retired gateway MCP `home-printer` and the ad-hoc `~/bin/escl-scan` on the server.
Printing is not handled here (CUPS is separate).

| | |
|---|---|
| Stack name / container | `network-scanner` / `network-scanner` |
| Host path | `/home/daniel/deployments/network-scanner/` (checkout of this repo, SSH remote) |
| LAN URL | `http://10.0.0.2:9191` |
| Tailnet URL | `http://100.105.192.28:9191` |
| Port | host `9191` -> container `8000` |
| Network | `network-scanner_default`, subnet `172.16.68.0/24` |
| Data | `./data/scans/` on the host (`/data/scans` in the container), gitignored; files older than `RETENTION_DAYS` (30) are pruned at startup and before every scan |
| Auth | none (LAN/tailnet only, like the other bridges) |
| Image | built locally from `Dockerfile` (`network-scanner:local`, official `ghcr.io/astral-sh/uv` image, FastAPI + Pillow) |

## Endpoints

```bash
S=http://10.0.0.2:9191

# Service + scanner state (always 200 while the service is up; scanner.reachable=false if the printer is off)
curl -s $S/health
# {"ok":true,"scanner":{"reachable":true,"state":"Idle","adf_state":"ScannerAdfEmpty","url":"http://10.0.0.72/eSCL","platen":true}}

# Scan one flatbed page. All fields optional:
#   format pdf|png|jpeg (pdf), resolution 150|300|600 (300), color color|gray|bw (color), name (slug suffix)
curl -s -X POST $S/scan -H 'Content-Type: application/json' \
     -d '{"format":"pdf","resolution":300,"color":"gray","name":"electric-bill"}'
# {"ok":true,"file":"2026-11-14_153012_electric-bill.pdf","url":"/scans/2026-11-14_153012_electric-bill.pdf","bytes":123456,"seconds":14.2}

curl -s $S/scans                                   # list, newest first
curl -sO $S/scans/2026-11-14_153012_electric-bill.pdf   # download
curl -s -X DELETE $S/scans/2026-11-14_153012_electric-bill.pdf
```

Errors: `409` scanner not Idle (busy, or a job is stuck), `502` scanner unreachable or job
rejected (message carries the printer's `ErrorInfo` description), `504` no page within 180 s.
Scans are serialised with a lock (one scanner), so a second request waits for the first.

Files are named `<YYYY-MM-DD_HHMMSS>[_name].<ext>`, with `ext` one of `pdf`, `png`, `jpg`.

## Deploy / update

```bash
# first time, on residencehome
cd /home/daniel/deployments && git clone git@github.com:danielrosehill/LAN-Network-Scanner.git network-scanner
cd network-scanner && mkdir -p data && docker compose up -d --build

# update
cd /home/daniel/deployments/network-scanner && git pull && docker compose up -d --build
```

Watchtower covers every container, but it **does not rebuild local builds**: this image only
changes when you redeploy with the command above. The base image is also only refreshed then.

Config is in `docker-compose.yml` env: `SCANNER_URL` (default `http://10.0.0.72/eSCL`),
`SCAN_DIR`, `RETENTION_DAYS`. No secrets, no `.env` needed.

## HP DeskJet 5200 eSCL: what was found (2026-11-14)

Verified against the printer from residencehome; firmware reports eSCL `pwg:Version` 2.63.

- **Endpoints answer on three ports:** `http://10.0.0.72/eSCL/`, `http://10.0.0.72:8080/eSCL/` and
  `https://10.0.0.72/eSCL/` (self-signed). This service uses plain HTTP on 80. The older `escl-scan`
  used `:8080`, which also works.
- **Flow:** `GET /eSCL/ScannerStatus` (`pwg:State` must be `Idle`) -> `POST /eSCL/ScanJobs` with a
  `scan:ScanSettings` XML body (`Content-Type: text/xml`) -> `201 Created` with an **absolute**
  `Location: http://10.0.0.72/eSCL/ScanJobs/<uuid>` -> poll `GET <job>/NextDocument`, which returns
  `503` until the page is ready, then `200` with the document. A 150 dpi gray page took ~11 s.
- **Capabilities** (`/eSCL/ScannerCapabilities`): platen max 2550 x 3508 (1/300 in), an ADF
  section is advertised (AdfState `ScannerAdfEmpty`) but the unit is flatbed-only in practice;
  formats `application/pdf`, `image/jpeg`, `application/octet-stream` (no PNG); resolutions
  75-600 (1200 on the platen); intents Document, Photo, Preview, TextAndGraphic.
- **`BlackAndWhite1` is advertised but rejected**: every combination tried (pdf/jpeg, 150/300 dpi,
  Document/TextAndGraphic intent) returns `409 Conflict`, `ErrorInfo` type `conflictWithExisting`,
  "Input Settings Mismatch Error!". So `color: "bw"` scans `Grayscale8` JPEG and thresholds at 128
  in Pillow, then writes PDF/PNG/JPEG from that.
- **PNG** is not a scanner format: the service scans JPEG and converts with Pillow. Colour and gray
  PDF and JPEG are passed through as the printer produced them.
- The scan region is A4 (2480 x 3507 at 1/300 in), `InputSource` `Platen`.
- `ScannerStatus` keeps a list of recent jobs (`scan:JobInfo`) with `JobState` `Completed`; no
  cleanup call is needed after fetching the page.

Code: `app/escl.py` (stdlib eSCL client, ported from
`LAN-Print-Scan-Plugin/scripts/escl-scan`), `app/main.py` (API).
