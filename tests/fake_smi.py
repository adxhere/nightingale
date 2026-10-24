"""A stand-in for nvidia-smi, for tests and for trying the laptop source on a machine without an NVIDIA GPU.

    NVIDIA_SMI="python|tests/fake_smi.py" FAKE_SMI_STATE=state.json python -m uvicorn app.main:app

The state file is JSON with any of the keys in DEFAULT; edit it while the app runs to change the readings.
Set "lost": true to make the GPU disappear.
"""
import json
import os
import sys

DEFAULT = {
    "name": "NVIDIA GeForce RTX 4060 Laptop GPU", "driver": "566.36", "pci": "00000000:01:00.0", "pstate": "P8",
    "temp": 52, "power": 14.2, "power_limit": 115.0, "power_max": 140.0, "clock": 210, "max_clock": 3105,
    "util": 3, "mem_used": 412, "mem_total": 8188, "fan": "[N/A]", "reasons": "0x0000000000000001",
    "procs": [], "lost": False,
}
HELP = """List of valid properties to query for the switch "--query-gpu=":

"index"
Zero based index of the GPU.

"name" or "gpu_name"
The official product name of the GPU.

"pci.bus_id" or "gpu_bus_id"
PCI bus id.

"driver_version"
The version of the installed NVIDIA display driver.

"pstate"
The current performance state for the GPU.

"temperature.gpu"
Core GPU temperature. in degrees C.

"power.draw"
The last measured power draw for the entire board, in watts.

"power.limit"
The software power limit in watts.

"power.max_limit"
The maximum value in watts that power limit can be set to.

"clocks.sm" or "clocks.current.sm"
Current frequency of SM clock.

"clocks.max.sm" or "clocks.max.sm"
Maximum frequency of SM clock.

"utilization.gpu"
Percent of time the GPU was busy.

"memory.used"
Total memory allocated by active contexts.

"memory.total"
Total installed GPU memory.

"fan.speed"
The fan speed value is the percent of the product's maximum noise tolerance fan speed.

"clocks_event_reasons.active" or "clocks_throttle_reasons.active"
Bitmask of active clock event reasons.
"""


def state() -> dict:
    s = dict(DEFAULT)
    path = os.getenv("FAKE_SMI_STATE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            s.update(json.load(fh))
    return s


def main(argv: list[str]) -> int:
    s = state()
    if s.get("lost"):
        print("No devices were found")
        return 6
    if "--help-query-gpu" in argv:
        print(HELP)
        return 0
    query = next((a for a in argv if a.startswith("--query-gpu=")), None)
    if query:
        values = {"index": 0, "name": s["name"], "pci.bus_id": s["pci"], "driver_version": s["driver"],
                  "pstate": s["pstate"], "temperature.gpu": s["temp"], "power.draw": s["power"],
                  "power.limit": s["power_limit"], "power.max_limit": s["power_max"], "clocks.sm": s["clock"],
                  "clocks.max.sm": s["max_clock"], "utilization.gpu": s["util"], "memory.used": s["mem_used"],
                  "memory.total": s["mem_total"], "fan.speed": s["fan"],
                  "clocks_event_reasons.active": s["reasons"], "clocks_throttle_reasons.active": s["reasons"]}
        fields = query.split("=", 1)[1].split(",")
        bad = [f for f in fields if f not in values]
        if bad:
            print(f'Field "{bad[0]}" is not a valid field to query.')
            return 2
        print(", ".join(str(values[f]) for f in fields))
        return 0
    if any(a.startswith("--query-compute-apps=") for a in argv):
        for p in s["procs"]:
            print(f"{p['pid']}, {p['name']}, {p.get('mem', '[N/A]')}")
        return 0
    print("+-----------------------------------------------------------------------------------------+")
    print("| Processes:                                                                              |")
    print("+-----------------------------------------------------------------------------------------+")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
