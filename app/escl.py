"""Minimal eSCL client for the HP DeskJet 5200 (flatbed, A4). Stdlib only.

Ported from LAN-Print-Scan-Plugin/scripts/escl-scan. Quirks found on this printer are in README.md.
"""
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

NS_SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"

# A4 in 1/300 in. The platen max is 2550 x 3508.
SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{ns_scan}" xmlns:pwg="{ns_pwg}">
<pwg:Version>2.0</pwg:Version><scan:Intent>Document</scan:Intent>
<pwg:ScanRegions><pwg:ScanRegion><pwg:Height>3507</pwg:Height><pwg:Width>2480</pwg:Width>
<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>
<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits></pwg:ScanRegion></pwg:ScanRegions>
<pwg:InputSource>Platen</pwg:InputSource><pwg:DocumentFormat>{fmt}</pwg:DocumentFormat>
<scan:ColorMode>{mode}</scan:ColorMode><scan:XResolution>{res}</scan:XResolution><scan:YResolution>{res}</scan:YResolution>
</scan:ScanSettings>"""


class ScannerError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]+)", text)
    return m.group(1) if m else None


class Escl:
    def __init__(self, base: str):
        self.base = base.rstrip("/")  # e.g. http://10.0.0.72/eSCL

    def status(self) -> dict:
        text = urllib.request.urlopen(self.base + "/ScannerStatus", timeout=10).read().decode()
        return {"state": _tag(text, "pwg:State") or "unknown",
                "adf_state": _tag(text, "scan:AdfState")}

    def scan(self, document_format: str, color_mode: str, resolution: int) -> bytes:
        """One flatbed page. Returns the raw document bytes."""
        state = self.status()["state"]
        if state != "Idle":
            raise ScannerError(f"scanner is {state}, not Idle", 409)
        body = SETTINGS.format(ns_scan=NS_SCAN, ns_pwg=NS_PWG, fmt=document_format,
                               mode=color_mode, res=resolution)
        req = urllib.request.Request(self.base + "/ScanJobs", data=body.encode(),
                                     headers={"Content-Type": "text/xml"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            detail = _tag(e.read().decode(errors="replace"), "Description") or e.reason
            raise ScannerError(f"ScanJobs rejected: HTTP {e.code} {detail}", 502) from e
        location = resp.headers.get("Location")
        if not location:
            raise ScannerError("ScanJobs returned no Location header")
        job = urljoin(self.base + "/", location).rstrip("/")
        # NextDocument answers 503 until the page is ready; on this printer it is ~10 s at 150 dpi.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                data = urllib.request.urlopen(job + "/NextDocument", timeout=180).read()
                if data:
                    return data
            except urllib.error.HTTPError as e:
                if e.code != 503:
                    raise ScannerError(f"NextDocument failed: HTTP {e.code}") from e
            time.sleep(2)
        raise ScannerError("scanner returned no document within 180 s", 504)
