"""Reads real GPU telemetry from NVIDIA DCGM, the way data centers export it.

Two ways in, both read-only:

* straight from one or more dcgm-exporter endpoints (``http://host:9400/metrics``), or every
  exporter pod in a Kubernetes cluster at once (``auto``), and
* from a Prometheus server that already scrapes them (``/api/v1/query``).

Either way the result is the same: per server, per GPU, the DCGM fields Nightingale reasons
about (temperature, power and its limits, clocks and the reasons they are held back, load,
memory, ECC errors, row remapping, NVLink errors, the last Xid) plus the pod using the GPU when
dcgm-exporter runs with Kubernetes pod mapping.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .local_gpu import decode_reasons

# DCGM renamed some fields in newer releases; dcgm-exporter's own lists still use the old names.
# Every name maps to one key of a GPU reading.
FIELDS = {
    "DCGM_FI_DEV_GPU_TEMP": "temp", "DCGM_FI_DEV_GPU_TEMP_CELSIUS": "temp",
    "DCGM_FI_DEV_MEMORY_TEMP": "mem_temp",
    "DCGM_FI_DEV_POWER_USAGE": "power", "DCGM_FI_DEV_BOARD_POWER_WATTS": "power",
    "DCGM_FI_DEV_POWER_MGMT_LIMIT": "power_limit", "DCGM_FI_DEV_BOARD_POWER_LIMIT_REQUESTED_WATTS": "power_limit",
    "DCGM_FI_DEV_ENFORCED_POWER_LIMIT": "power_enforced", "DCGM_FI_DEV_BOARD_POWER_LIMIT_ENFORCED_WATTS": "power_enforced",
    "DCGM_FI_DEV_POWER_MGMT_LIMIT_MIN": "power_min", "DCGM_FI_DEV_BOARD_POWER_LIMIT_MIN_WATTS": "power_min",
    "DCGM_FI_DEV_POWER_MGMT_LIMIT_MAX": "power_max", "DCGM_FI_DEV_BOARD_POWER_LIMIT_MAX_WATTS": "power_max",
    "DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF": "power_default", "DCGM_FI_DEV_BOARD_POWER_LIMIT_DEFAULT_WATTS": "power_default",
    "DCGM_FI_DEV_SM_CLOCK": "clock", "DCGM_FI_DEV_MAX_SM_CLOCK": "max_clock",
    "DCGM_FI_DEV_GPU_UTIL": "util",
    "DCGM_FI_DEV_FB_USED": "mem_used", "DCGM_FI_DEV_FB_FREE": "mem_free", "DCGM_FI_DEV_FB_RESERVED": "mem_reserved",
    "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": "sbe", "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": "dbe",
    "DCGM_FI_DEV_ECC_SBE_AGG_TOTAL": "sbe_agg", "DCGM_FI_DEV_ECC_DBE_AGG_TOTAL": "dbe_agg",
    "DCGM_FI_DEV_ROW_REMAP_PENDING": "remap_pending",
    "DCGM_FI_DEV_ROW_REMAP_FAILURE": "remap_failed", "DCGM_FI_DEV_ROW_REMAP_FAILED": "remap_failed",
    "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS": "rows_uncorrectable",
    "DCGM_FI_DEV_ROW_REMAP_UNCORRECTABLE_TOTAL": "rows_uncorrectable",
    "DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS": "rows_correctable",
    "DCGM_FI_DEV_RETIRED_PENDING": "retired_pending", "DCGM_FI_DEV_PAGE_RETIRED_PENDING": "retired_pending",
    "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL": "nvlink_crc", "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL": "nvlink_crc",
    "DCGM_FI_DEV_NVLINK_CRC_ERROR_TOTAL": "nvlink_crc",
    "DCGM_FI_DEV_PCIE_REPLAY_COUNTER": "pcie_replay", "DCGM_FI_DEV_PCIE_REPLAY_TOTAL": "pcie_replay",
    "DCGM_FI_DEV_XID_ERRORS": "xid", "DCGM_FI_DEV_XID_ERROR": "xid",
    "DCGM_EXP_XID_ERRORS_TOTAL": "xid_total",
    "DCGM_FI_DEV_CLOCKS_EVENT_REASONS": "reasons_mask", "DCGM_FI_DEV_CLOCK_THROTTLE_REASONS": "reasons_mask",
    "DCGM_FI_DEV_THERMAL_VIOLATION": "thermal_violation", "DCGM_FI_DEV_POWER_VIOLATION": "power_violation",
}
# Which label names the server, in order of preference. dcgm-exporter's own "Hostname" is often the
# exporter pod's name inside Kubernetes, so labels added by Prometheus come first.
NODE_LABELS = ("kubernetes_node", "node", "nodename", "node_name", "Hostname", "hostname", "instance")

# What the common NVIDIA driver Xid codes mean, and what kind of problem they point at.
XID = {
    13: ("Graphics engine exception", "software"), 31: ("GPU memory page fault (illegal address)", "software"),
    43: ("GPU stopped processing", "software"), 45: ("Preemptive cleanup after an earlier error", "software"),
    48: ("Double-bit ECC error", "memory"), 61: ("Internal micro-controller breakpoint", "firmware"),
    62: ("Internal micro-controller halt", "firmware"), 63: ("ECC page retirement or row remap recorded", "memory"),
    64: ("ECC page retirement or row remap failed", "memory"), 68: ("Video processor exception", "software"),
    69: ("Graphics engine class error", "software"), 74: ("NVLink error", "interconnect"),
    79: ("GPU has fallen off the bus", "gpu_lost"), 92: ("High single-bit ECC error rate", "memory"),
    94: ("Contained ECC error", "memory"), 95: ("Uncontained ECC error", "memory"),
    109: ("Context switch timeout", "software"), 119: ("GSP RPC timeout", "firmware"),
    120: ("GSP error", "firmware"), 154: ("GPU recovery action changed", "gpu_lost"),
}

_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\d+)?\s*$")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def xid_text(code: int) -> str:
    meaning = XID.get(int(code))
    return f"Xid {code} ({meaning[0]})" if meaning else f"Xid {code}"


def parse_text(text: str) -> list[tuple[str, dict, float]]:
    """Prometheus text exposition format into (metric, labels, value)."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, labels, value = m.groups()
        try:
            v = float(value)
        except ValueError:
            continue
        lab = {k: bytes(val, "utf-8").decode("unicode_escape") if "\\" in val else val
               for k, val in _LABEL.findall(labels or "")}
        out.append((name, lab, v))
    return out


def node_of(labels: dict) -> str:
    for key in NODE_LABELS:
        v = labels.get(key)
        if v:
            return v.split(":")[0] if key == "instance" else v
    return ""


def group(samples, resolve=None) -> dict:
    """Samples into {server: {"gpus": {index: reading}, "model": ..., "driver": ...}}.

    resolve maps whatever names a server in the labels (a node, an exporter pod) to the server's name.
    """
    nodes: dict = {}
    for name, labels, value in samples:
        key = FIELDS.get(name)
        if key is None:
            continue
        raw_node = labels.pop("__node__", "") or node_of(labels)
        node = (resolve(raw_node) if resolve else raw_node) or raw_node or "unknown"
        try:
            gi = int(labels.get("gpu", labels.get("GPU_I_ID", 0)) or 0)
        except ValueError:
            continue
        n = nodes.setdefault(node, {"gpus": {}, "model": "", "driver": ""})
        g = n["gpus"].setdefault(gi, {"index": gi, "uuid": "", "pci": "", "model": "", "pod": "", "namespace": ""})
        g["uuid"] = g["uuid"] or labels.get("UUID", "")
        g["pci"] = g["pci"] or labels.get("pci_bus_id", "").lower().replace("00000000:", "0000:")
        g["model"] = g["model"] or labels.get("modelName", "")
        if labels.get("pod"):
            g["pod"], g["namespace"] = labels["pod"], labels.get("namespace", "")
        n["model"] = n["model"] or labels.get("modelName", "")
        n["driver"] = n["driver"] or labels.get("DCGM_FI_DRIVER_VERSION", "") or labels.get("driver_version", "")
        if key == "reasons_mask":
            g["reasons"] = [r for r in decode_reasons(str(int(value))) if r != "GPU_IDLE"]
        g[key] = value
    for n in nodes.values():
        for g in n["gpus"].values():
            if "mem_used" in g and "mem_free" in g:
                g["mem_total"] = g["mem_used"] + g["mem_free"] + g.get("mem_reserved", 0)
    return nodes


def _get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"Accept": "text/plain, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the operator's own endpoint
        return resp.read().decode("utf-8", "replace")


class ExporterReader:
    """Scrapes dcgm-exporter endpoints. urls: a list of (server or "", url), or "auto" with a Kubernetes client."""

    kind = "dcgm-exporter"

    def __init__(self, urls, kube=None, timeout: float = 5.0) -> None:
        self.auto = urls == "auto"
        self.urls = [] if self.auto else [(n, u) for n, u in urls]
        self.kube = kube
        self.timeout = timeout

    def describe(self) -> str:
        if self.auto:
            return "every dcgm-exporter pod in the Kubernetes cluster"
        return ", ".join(u for _, u in self.urls)

    def targets(self) -> list[tuple[str, str]]:
        if not self.auto:
            return self.urls
        if self.kube is None:
            raise RuntimeError("'auto' needs a Kubernetes connection to find the dcgm-exporter pods")
        found = self.kube.exporter_endpoints()
        if not found:
            raise RuntimeError("no dcgm-exporter pods found (is the NVIDIA GPU Operator installed?)")
        return found

    def read(self) -> tuple[list, list[str]]:
        samples, errors = [], []
        for node, url in self.targets():
            try:
                text = _get(url, self.timeout)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(f"{node or url}: {getattr(exc, 'reason', exc)}")
                continue
            for name, labels, value in parse_text(text):
                if node:
                    labels["__node__"] = node
                samples.append((name, labels, value))
        return samples, errors


class PrometheusReader:
    """Asks a Prometheus server for the latest value of every DCGM series."""

    kind = "prometheus"
    QUERY = '{__name__=~"DCGM_FI_.*|DCGM_EXP_.*"}'

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def describe(self) -> str:
        return self.url

    def read(self) -> tuple[list, list[str]]:
        q = urllib.parse.urlencode({"query": self.QUERY})
        try:
            data = json.loads(_get(f"{self.url}/api/v1/query?{q}", self.timeout))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return [], [f"{self.url}: {getattr(exc, 'reason', exc)}"]
        if data.get("status") != "success":
            return [], [f"{self.url}: {data.get('error') or 'query failed'}"]
        samples = []
        for row in data.get("data", {}).get("result", []):
            labels = dict(row.get("metric") or {})
            name = labels.pop("__name__", "")
            try:
                value = float((row.get("value") or [0, "nan"])[1])
            except (TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            samples.append((name, labels, value))
        return samples, []


def make_reader(url: str, kube=None):
    """'auto', a Prometheus base URL, or one or more exporter /metrics URLs (comma separated, optionally name=url)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("no DCGM source given")
    if url == "auto":
        return ExporterReader("auto", kube)
    parts = [p.strip() for p in url.split(",") if p.strip()]
    if len(parts) == 1 and "/metrics" not in parts[0] and "=" not in parts[0]:
        return PrometheusReader(parts[0])
    urls = []
    for p in parts:
        node, _, u = p.partition("=") if "=" in p and not p.startswith("http") else ("", "", p)
        if not u.startswith(("http://", "https://")):
            raise ValueError(f"'{u}' is not an http(s) URL")
        urls.append((node, u))
    return ExporterReader(urls)


def wait_until(fn, seconds: float, step: float = 0.2) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())
