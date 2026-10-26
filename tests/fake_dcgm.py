"""A stand-in dcgm-exporter: serves /metrics in DCGM's format from a dict the test changes."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def gpu(temp=55, power=180, limit=350, util=90, clock=2100, sbe=0, dbe=0, xid=0, remap=0, mem_used=20000,
        mem_free=25000, reasons=0, pod="", namespace="", **extra) -> dict:
    return {"DCGM_FI_DEV_GPU_TEMP": temp, "DCGM_FI_DEV_POWER_USAGE": power, "DCGM_FI_DEV_POWER_MGMT_LIMIT": limit,
            "DCGM_FI_DEV_POWER_MGMT_LIMIT_MIN": 100, "DCGM_FI_DEV_POWER_MGMT_LIMIT_MAX": 350,
            "DCGM_FI_DEV_GPU_UTIL": util, "DCGM_FI_DEV_SM_CLOCK": clock, "DCGM_FI_DEV_MAX_SM_CLOCK": 2520,
            "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": sbe, "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": dbe, "DCGM_FI_DEV_XID_ERRORS": xid,
            "DCGM_FI_DEV_ROW_REMAP_PENDING": remap, "DCGM_FI_DEV_FB_USED": mem_used, "DCGM_FI_DEV_FB_FREE": mem_free,
            "DCGM_FI_DEV_CLOCKS_EVENT_REASONS": reasons, "_pod": pod, "_namespace": namespace, **extra}


def render(nodes: dict) -> str:
    """nodes: {hostname: [gpu dict, ...]} -> exposition text like dcgm-exporter's."""
    lines = []
    for host, gpus in nodes.items():
        for i, g in enumerate(gpus):
            labels = (f'gpu="{i}",UUID="GPU-{host}-{i}",pci_bus_id="00000000:{0x18 + i:02X}:00.0",device="nvidia{i}",'
                      f'modelName="NVIDIA L40S",Hostname="{host}",DCGM_FI_DRIVER_VERSION="550.127.08",'
                      f'container="",namespace="{g.get("_namespace", "")}",pod="{g.get("_pod", "")}"')
            for name, value in g.items():
                if not name.startswith("_"):
                    lines.append(f"{name}{{{labels}}} {value}")
    return "# HELP DCGM_FI_DEV_GPU_TEMP GPU temperature (in C).\n# TYPE DCGM_FI_DEV_GPU_TEMP gauge\n" + "\n".join(lines) + "\n"


class FakeExporter:
    def __init__(self, nodes: dict) -> None:
        self.nodes = nodes
        self.up = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if not owner.up:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = render(owner.nodes).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/metrics"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
