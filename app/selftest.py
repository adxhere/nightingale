"""GPU self-tests, run by Nightingale in a separate process so a crash or a hung driver can't take it down.

    python -m app.selftest --test quick            about 20 seconds: does the GPU compute correctly, how fast
    python -m app.selftest --test memory           1 to 2 minutes: writes patterns to most of the free GPU memory
                                                   and reads them back, looking for bits that don't stick
    python -m app.selftest --test stress --minutes 3
                                                   full load for N minutes: does speed hold up, do results stay
                                                   correct, does anything crash

Every line printed is one JSON event: start, progress, result or error. Needs PyTorch with CUDA:
    python -m pip install torch --index-url https://download.pytorch.org/whl/cu126
If a CPU-only PyTorch is already installed, pip says "already satisfied" and keeps it, so remove it first:
    python -m pip uninstall -y torch
NIGHTINGALE_SELFTEST_CPU=1 runs tiny versions on the CPU (for Nightingale's own tests).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

INSTALL = "python -m pip install torch --index-url https://download.pytorch.org/whl/cu126"
# pip keeps an installed CPU-only build ("already satisfied"), so swapping builds needs an uninstall first.
REINSTALL = "python -m pip uninstall -y torch, then " + INSTALL


def emit(event: str, **kv) -> None:
    print(json.dumps({"event": event, "t": round(time.time(), 2), **kv}), flush=True)


def load_torch():
    try:
        import torch  # noqa: PLC0415 - optional, heavy dependency
    except ImportError:
        emit("error", code="no_torch", message=f"PyTorch isn't installed. Install it with: {INSTALL}")
        sys.exit(2)
    cpu_ok = os.getenv("NIGHTINGALE_SELFTEST_CPU") == "1"
    if not torch.cuda.is_available() and not cpu_ok:
        build = "a CPU-only build" if not getattr(torch.version, "cuda", None) else f"CUDA {torch.version.cuda}"
        fix = (f"Swap it for the CUDA build: {REINSTALL}" if not getattr(torch.version, "cuda", None) else
               f"Reinstall it: {REINSTALL}. If that doesn't help, update the NVIDIA driver")
        emit("error", code="no_cuda", message=f"PyTorch {torch.__version__} ({build}) can't see your NVIDIA GPU. {fix}.")
        sys.exit(3)
    return torch, ("cuda" if torch.cuda.is_available() else "cpu")


def device_info(torch, dev: str) -> dict:
    if dev == "cpu":
        return {"name": "CPU (test mode)", "memory_gb": None, "torch": torch.__version__, "cuda": None}
    p = torch.cuda.get_device_properties(0)
    return {"name": p.name, "memory_gb": round(p.total_memory / 2**30, 1), "torch": torch.__version__,
            "cuda": torch.version.cuda, "capability": f"{p.major}.{p.minor}", "sms": p.multi_processor_count}


def sync(torch, dev):
    if dev == "cuda":
        torch.cuda.synchronize()


def matmul_tflops(torch, dev, n: int, dtype, seconds: float) -> float:
    a = torch.randn(n, n, device=dev, dtype=dtype)
    b = torch.randn(n, n, device=dev, dtype=dtype)
    a @ b
    sync(torch, dev)
    start, count = time.time(), 0
    while time.time() - start < seconds:
        for _ in range(4):
            a @ b
        sync(torch, dev)
        count += 4
    return 2 * n ** 3 * count / (time.time() - start) / 1e12


# ------------------------------------------------------------------- quick check
def quick(torch, dev) -> dict:
    small = dev == "cpu"
    torch.backends.cuda.matmul.allow_tf32 = False
    emit("progress", pct=10, message="Checking that the GPU computes correctly")
    g = torch.Generator(device="cpu").manual_seed(7)
    a = torch.randn(512, 512, generator=g, dtype=torch.float64)
    b = torch.randn(512, 512, generator=g, dtype=torch.float64)
    ref = a @ b
    got32 = (a.float().to(dev) @ b.float().to(dev)).double().cpu()
    err32 = float(((got32 - ref).abs().max() / ref.abs().max()).item())
    got16 = (a.half().to(dev) @ b.half().to(dev)).double().cpu() if dev == "cuda" else got32
    err16 = float(((got16 - ref).abs().max() / ref.abs().max()).item())
    correct = err32 < 1e-4 and err16 < 2e-2
    emit("progress", pct=35, message="Measuring speed (FP32)")
    n = 256 if small else 4096
    fp32 = matmul_tflops(torch, dev, n, torch.float32, 1 if small else 5)
    emit("progress", pct=65, message="Measuring speed (FP16 tensor cores)")
    fp16 = matmul_tflops(torch, dev, n, torch.float16 if dev == "cuda" else torch.float32, 1 if small else 5)
    emit("progress", pct=85, message="Measuring memory bandwidth")
    x = torch.empty(int((16 if small else 512) * 2**20 / 4), device=dev, dtype=torch.float32)
    y = torch.empty_like(x)
    y.copy_(x)
    sync(torch, dev)
    start, copies = time.time(), 0
    while time.time() - start < (0.5 if small else 3):
        y.copy_(x)
        copies += 1
        if copies % 8 == 0:
            sync(torch, dev)
    sync(torch, dev)
    gbs = 2 * x.numel() * 4 * copies / (time.time() - start) / 1e9
    ok = correct
    summary = (f"{'Computes correctly' if correct else 'WRONG RESULTS'}: {fp16:.1f} TFLOPS FP16, {fp32:.1f} TFLOPS FP32, "
               f"memory bandwidth {gbs:.0f} GB/s.")
    return {"ok": ok, "summary": summary,
            "details": {"fp32_tflops": round(fp32, 2), "fp16_tflops": round(fp16, 2), "bandwidth_gbs": round(gbs, 1),
                        "fp32_error": err32, "fp16_error": err16}}


# -------------------------------------------------------------------- memory test
def memory(torch, dev, passes: int = 2) -> dict:
    chunk_words = 2**20 if dev == "cpu" else 64 * 2**20   # 4 MB on the CPU, 256 MB of int32 on the GPU
    idx = torch.arange(chunk_words, device=dev, dtype=torch.int32)   # for the address pattern
    if dev == "cuda":
        free, total = torch.cuda.mem_get_info()
        # Leave room for the read-back copy, the display and other programs.
        budget = int(free * 0.80) - chunk_words * 4
    else:
        free = total = budget = 64 * 2**20
    chunks = []
    try:
        while (len(chunks) + 1) * chunk_words * 4 <= budget:
            chunks.append(torch.empty(chunk_words, dtype=torch.int32, device=dev))
    except RuntimeError:  # ran out early: test what we got
        if dev == "cuda":
            torch.cuda.empty_cache()
    tested = len(chunks) * chunk_words * 4
    emit("progress", pct=3, message=f"Testing {tested / 2**30:.2f} GB of GPU memory, {passes} passes")
    patterns = [("zeros", 0), ("ones", -1), ("checker", 0x55555555), ("checker inverted", -0x55555556), ("random", None)]
    steps = passes * len(patterns)
    bad, done = 0, 0
    for p in range(passes):
        for name, value in patterns:
            for i, c in enumerate(chunks):
                if value is None:
                    gen = torch.Generator(device=dev).manual_seed(1000 * p + i)
                    c.random_(-2**31, 2**31 - 1, generator=gen)
                else:
                    c.fill_(value)
            sync(torch, dev)
            for i, c in enumerate(chunks):
                if value is None:
                    gen = torch.Generator(device=dev).manual_seed(1000 * p + i)
                    want = torch.empty_like(c).random_(-2**31, 2**31 - 1, generator=gen)
                    bad += int((c != want).sum().item())
                    del want
                else:
                    bad += int((c != value).sum().item())
            if name == "random":  # address pattern too: each word holds its own index
                for c in chunks:
                    c.copy_(idx)
                sync(torch, dev)
                for c in chunks:
                    bad += int((c != idx).sum().item())
            done += 1
            emit("progress", pct=round(100 * done / steps), message=f"Pass {p + 1} of {passes}: {name} pattern",
                 errors=bad)
    del chunks, idx
    if dev == "cuda":
        torch.cuda.empty_cache()
    ok = bad == 0
    summary = (f"No errors in {tested / 2**30:.2f} GB of GPU memory ({passes} passes, 6 patterns)." if ok else
               f"{bad:,} memory errors in {tested / 2**30:.2f} GB: some GPU memory doesn't hold its value. "
               "That's a hardware fault; get the GPU checked under warranty.")
    return {"ok": ok, "summary": summary, "details": {"tested_gb": round(tested / 2**30, 2), "errors": bad,
                                                       "free_gb": round(free / 2**30, 2), "total_gb": round(total / 2**30, 2)}}


# -------------------------------------------------------------------- stress test
def stress(torch, dev, minutes: float) -> dict:
    small = dev == "cpu"
    n = 256 if small else 4096
    dtype = torch.float16 if dev == "cuda" else torch.float32
    g = torch.Generator(device="cpu").manual_seed(11)
    a = torch.randn(n, n, generator=g).to(dev, dtype)
    b = torch.randn(n, n, generator=g).to(dev, dtype)
    probe = (a[:256, :256].float() @ b[:256, :256].float())  # a fixed check, recomputed every window
    probe_ref = probe.cpu()
    seconds = max(5.0, minutes * 60)
    window = 2.0 if small else 5.0
    start = time.time()
    windows, wrong, nan = [], 0, 0
    while time.time() - start < seconds:
        w0, count = time.time(), 0
        while time.time() - w0 < window:
            c = a @ b
            count += 1
            if count % 8 == 0:
                sync(torch, dev)
        sync(torch, dev)
        tflops = 2 * n ** 3 * count / (time.time() - w0) / 1e12
        if not torch.isfinite(c).all():
            nan += 1
        again = (a[:256, :256].float() @ b[:256, :256].float()).cpu()
        if not torch.allclose(again, probe_ref, rtol=1e-3, atol=1e-2):
            wrong += 1
        windows.append(round(tflops, 3))
        elapsed = time.time() - start
        emit("progress", pct=min(99, round(100 * elapsed / seconds)), message=f"Full load: {tflops:.1f} TFLOPS",
             tflops=round(tflops, 2), elapsed=round(elapsed), errors=wrong + nan)
    first = windows[: max(1, len(windows) // 5)]
    last = windows[-max(1, len(windows) // 5):]
    f_avg, l_avg = sum(first) / len(first), sum(last) / len(last)
    drop = max(0.0, 100 * (1 - l_avg / f_avg)) if f_avg else 0.0
    ok = wrong == 0 and nan == 0
    span = f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"
    summary = (f"Ran {span} at full load. " +
               ("Every result stayed correct. " if ok else f"{wrong + nan} windows gave wrong results: the GPU is unstable under load. ") +
               (f"Speed fell {drop:.0f}% from the start ({f_avg:.1f} to {l_avg:.1f} TFLOPS), which usually means it slowed down for heat."
                if drop >= 10 else f"Speed held steady at about {l_avg:.1f} TFLOPS."))
    return {"ok": ok, "summary": summary,
            "details": {"minutes": round(seconds / 60, 1), "windows": windows, "start_tflops": round(f_avg, 2),
                        "end_tflops": round(l_avg, 2), "drop_pct": round(drop, 1), "wrong_windows": wrong,
                        "nan_windows": nan}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", choices=["quick", "memory", "stress"], default="quick")
    ap.add_argument("--minutes", type=float, default=3)
    args = ap.parse_args(argv)
    torch, dev = load_torch()
    emit("start", test=args.test, device=device_info(torch, dev))
    try:
        if args.test == "quick":
            out = quick(torch, dev)
        elif args.test == "memory":
            out = memory(torch, dev)
        else:
            out = stress(torch, dev, args.minutes)
    except RuntimeError as exc:  # CUDA errors land here: they are findings, not bugs
        msg = str(exc).splitlines()[0][:300]
        emit("result", ok=False, summary=f"The GPU failed during the test: {msg}. That points at a driver or hardware "
                                         "problem.", details={"error": msg})
        return 1
    emit("result", **out)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
