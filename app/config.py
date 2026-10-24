"""Runtime settings, read from environment variables (and a local .env file if present)."""
from __future__ import annotations

import os

try:  # optional: lets people keep their key in a .env file during development
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        # Nebius Token Factory (OpenAI-compatible API)
        self.api_key = os.getenv("NEBIUS_API_KEY", "").strip()
        self.base_url = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")

        # Two-tier model routing: a small fast model scans everything,
        # the large reasoning model only sees incidents.
        self.triage_model = os.getenv("TRIAGE_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
        self.diagnosis_model = os.getenv("DIAGNOSIS_MODEL", "nvidia/Nemotron-3-Ultra-550b-a55b")
        # Model IDs differ slightly between catalogs; at startup Nightingale checks
        # the live model list and picks the closest Nemotron match if needed.
        self.auto_discover_models = _bool("AUTO_DISCOVER_MODELS", True)
        # Triage doesn't need long chain-of-thought; ask the model to skip it when supported.
        self.triage_disable_thinking = _bool("TRIAGE_DISABLE_THINKING", True)

        # Demo mode runs an offline stand-in for the models (no key, no credits needed).
        self.force_mock = _bool("MOCK_MODE", False)

        # Simulation: how many GPUs the simulated data center has (8 per server, 64 per rack)
        self.num_gpus = int(os.getenv("NUM_GPUS", "1024"))
        self.tick_seconds = _float("TICK_SECONDS", 2.0)
        self.triage_every_ticks = int(os.getenv("TRIAGE_EVERY_TICKS", "5"))
        self.seed = int(os.getenv("SEED")) if os.getenv("SEED") else None

        # This computer's own NVIDIA GPU, read through nvidia-smi. Connected at startup when one is
        # found (LOCAL_GPU=0 turns that off); also from the Data sources screen. NVIDIA_SMI overrides the path.
        self.local_gpu = _bool("LOCAL_GPU", True)
        self.local_poll_seconds = _float("LOCAL_POLL_SECONDS", 2.0)
        # A simulated laptop (readings and Windows) for the public demo and for machines without one.
        self.demo_pc = _bool("DEMO_PC", False)

        # Safety: low-risk steps (power cap, ticket, notify) may run without a click if enabled.
        self.auto_approve_low_risk = _bool("AUTO_APPROVE_LOW_RISK", False)

        # USD per 1M tokens (input, output). Estimates for the cost panel; check the
        # Token Factory pricing page and override if they change.
        self.triage_price = (_float("TRIAGE_PRICE_IN", 0.06), _float("TRIAGE_PRICE_OUT", 0.24))
        self.diagnosis_price = (_float("DIAGNOSIS_PRICE_IN", 1.00), _float("DIAGNOSIS_PRICE_OUT", 3.00))

    @property
    def mock(self) -> bool:
        return self.force_mock or not self.api_key


def load_settings() -> Settings:
    return Settings()
