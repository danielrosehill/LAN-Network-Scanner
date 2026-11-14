"""LAN Network Scanner: HTTP API over the HP DeskJet 5200 flatbed (eSCL)."""
import io
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

from .escl import Escl, ScannerError

SCANNER_URL = os.environ.get("SCANNER_URL", "http://10.0.0.72/eSCL")
SCAN_DIR = Path(os.environ.get("SCAN_DIR", "/data/scans"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(pdf|png|jpg)$")
EXT = {"pdf": "pdf", "png": "png", "jpeg": "jpg"}
MODE = {"color": "RGB24", "gray": "Grayscale8", "bw": "Grayscale8"}  # bw: thresholded here

escl = Escl(SCANNER_URL)
lock = threading.Lock()


class ScanRequest(BaseModel):
    format: Literal["pdf", "png", "jpeg"] = "pdf"
    resolution: Literal[150, 300, 600] = 300
    color: Literal["color", "gray", "bw"] = "color"
    name: str | None = None


def prune() -> int:
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - RETENTION_DAYS * 86400
    n = 0
    for f in SCAN_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    return n


def scan_file(name: str) -> Path:
    if not FILE_RE.match(name):
        raise HTTPException(400, "bad file name")
    path = SCAN_DIR / name
    if not path.is_file():
        raise HTTPException(404, "no such scan")
    return path


@asynccontextmanager
async def lifespan(_app):
    prune()
    yield


app = FastAPI(title="LAN Network Scanner", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        scanner = {"reachable": True, **escl.status()}
    except Exception as e:  # service is still healthy if the printer is asleep or off
        scanner = {"reachable": False, "error": str(e)}
    scanner["url"] = SCANNER_URL
    scanner["platen"] = True
    return {"ok": True, "scanner": scanner}


@app.post("/scan")
def scan(req: ScanRequest):
    slug = ""
    if req.name:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", req.name).strip("-")[:60]
    # Native PDF/JPEG for color and gray; png and bw need the image, so ask for JPEG then.
    native = req.format == "pdf" and req.color != "bw"
    fmt = "application/pdf" if native else "image/jpeg"
    with lock:
        prune()
        started = time.monotonic()
        try:
            data = escl.scan(fmt, MODE[req.color], req.resolution)
        except ScannerError as e:
            raise HTTPException(e.status, str(e))
        except OSError as e:
            raise HTTPException(502, f"scanner unreachable: {e}")
        seconds = round(time.monotonic() - started, 1)
        if not native and (req.format != "jpeg" or req.color == "bw"):
            img = Image.open(io.BytesIO(data))
            if req.color == "bw":
                img = img.convert("L").point(lambda v: 255 if v >= 128 else 0, mode="1")
            out = io.BytesIO()
            save_as = {"pdf": "PDF", "png": "PNG", "jpeg": "JPEG"}[req.format]
            img.save(out, save_as, resolution=float(req.resolution), dpi=(req.resolution,) * 2)
            data = out.getvalue()
        name = f"{datetime.now():%Y-%m-%d_%H%M%S}{'_' + slug if slug else ''}.{EXT[req.format]}"
        (SCAN_DIR / name).write_bytes(data)
    return {"ok": True, "file": name, "url": f"/scans/{name}", "bytes": len(data), "seconds": seconds}


@app.get("/scans")
def list_scans():
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted((f for f in SCAN_DIR.iterdir() if f.is_file() and FILE_RE.match(f.name)),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return {"scans": [{"file": f.name, "url": f"/scans/{f.name}", "bytes": f.stat().st_size,
                       "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
                      for f in files]}


@app.get("/scans/{name}")
def get_scan(name: str):
    return FileResponse(scan_file(name), filename=name)


@app.delete("/scans/{name}")
def delete_scan(name: str):
    scan_file(name).unlink()
    return {"ok": True, "deleted": name}
