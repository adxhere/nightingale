"""This computer's GPU, served in NVIDIA DCGM's format, so the real data center path can be tried for free.

    python -m app.dcgm_bridge                 serves http://127.0.0.1:9400/metrics
    python -m app.dcgm_bridge --port 9401

Real GPU servers publish their health through dcgm-exporter in exactly this format (same metric names,
same labels). Point Nightingale at this URL (Data sources > Real GPU servers, Kubernetes off) and this
computer shows up as one real server in hall R, read the way a data center is read. Laptops have no
ECC or NVLink and no DCGM, so those counters are absent here and the readings come from nvidia-smi;
on a real server they come from DCGM itself.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .local_gpu import REASONS, LocalGPU

METRICS = [  # (DCGM field, reading key, type, help)
    ("DCGM_FI_DEV_GPU_TEMP", "temp", "gauge", "GPU temperature (in C)."),
    ("DCGM_FI_DEV_POWER_USAGE", "power", "gauge", "Power draw (in W)."),
    ("DCGM_FI_DEV_POWER_MGMT_LIMIT", "power_limit", "gauge", "Configured power management limit (in W)."),
    ("DCGM_FI_DEV_POWER_MGMT_LIMIT_MAX", "power_max", "gauge", "Maximum power management limit (in W)."),
    ("DCGM_FI_DEV_SM_CLOCK", "clock", "gauge", "SM clock frequency (in MHz)."),
    ("DCGM_FI_DEV_MAX_SM_CLOCK", "max_clock", "gauge", "Maximum SM clock frequency (in MHz)."),
    ("DCGM_FI_DEV_GPU_UTIL", "util", "gauge", "GPU utilization (in %)."),
    ("DCGM_FI_DEV_FB_USED", "mem_used", "gauge", "Framebuffer memory used (in MiB)."),
    ("DCGM_FI_DEV_FB_FREE", "mem_free", "gauge", "Framebuffer memory free (in MiB)."),
    ("DCGM_FI_DEV_CLOCKS_EVENT_REASONS", "mask", "gauge", "Current clock event reasons (bitmask)."),
    ("DCGM_FI_DEV_XID_ERRORS", "xid", "gauge", "Value of the last XID error encountered."),
]
BITS = {name: bit for bit, name in REASONS}


def render(gpus: list[dict], host: str) -> str:
    out = []
    for field, key, kind, help_text in METRICS:
        out.append(f"# HELP {field} {help_text}\n# TYPE {field} {kind}")
        for g in gpus:
            r = dict(g)
            r["mem_free"] = (g["mem_total"] - g["mem_used"]) if g.get("mem_total") and g.get("mem_used") is not None else None
            r["mask"] = sum(BITS.get(x, 0) for x in g.get("reasons", []))
            r["xid"] = 0
            value = r.get(key)
            if value is None:
                continue
            pci = (g.get("pci") or "").upper().replace("0000:", "00000000:")
            labels = (f'gpu="{g["index"]}",UUID="GPU-{host}-{g["index"]}",pci_bus_id="{pci}",device="nvidia{g["index"]}",'
                      f'modelName="{g.get("name", "NVIDIA GPU")}",Hostname="{host}",'
                      f'DCGM_FI_DRIVER_VERSION="{g.get("driver", "")}"')
            out.append(f"{field}{{{labels}}} {float(value):g}")
    return "\n".join(out) + "\n"


def serve(port: int = 9400, host: str = "127.0.0.1", source: LocalGPU | None = None) -> ThreadingHTTPServer:
    src = source or LocalGPU(events=False)
    ok, msg = src.probe()
    if not ok:
        raise SystemExit(f"Can't read this computer's GPU: {msg}")
    src.start()
    name = socket.gethostname().lower() or "this-computer"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_response(404)
                self.end_headers()
                return
            snap = src.snapshot()
            body = render(snap["gpus"] if snap["state"] == "ok" else [], name).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.gpu_name = msg
    threading.Thread(target=server.serve_forever, name="dcgm-bridge", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    server = serve(args.port, args.host)
    url = f"http://{args.host}:{args.port}/metrics"
    print(f"{server.gpu_name}\nServing this computer's GPU in DCGM format at {url}\n"
          f"In Nightingale: Data sources > Real GPU servers > paste {url}, Kubernetes off, then Connect.\n"
          "Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
