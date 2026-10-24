"""Talks to NVIDIA Nemotron models on Nebius Token Factory (OpenAI-compatible API).

Two models, three jobs:
  triage    -> Nemotron Nano: fast and cheap, reads every telemetry window
  diagnosis -> Nemotron Ultra: investigates flagged incidents with tools and plans the fix
  ask       -> Nemotron Ultra too: answers questions in "Ask your data center" (counted separately)
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import mock_llm, prompts
from .config import Settings

ROLES = ("triage", "diagnosis", "ask")
MODEL_FOR = {"triage": "triage", "diagnosis": "diagnosis", "ask": "diagnosis"}


class UsageTracker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.stats = {r: {"calls": 0, "in": 0, "out": 0, "seconds": 0.0} for r in ROLES}

    def record(self, role: str, tokens_in: int, tokens_out: int, seconds: float) -> None:
        with self.lock:
            s = self.stats[role]
            s["calls"] += 1
            s["in"] += int(tokens_in)
            s["out"] += int(tokens_out)
            s["seconds"] += seconds

    def summary(self) -> dict:
        with self.lock:
            t, d, a = self.stats["triage"], self.stats["diagnosis"], self.stats["ask"]
            (tin, tout), (din, dout) = self.settings.triage_price, self.settings.diagnosis_price
            triage_cost = (t["in"] * tin + t["out"] * tout) / 1e6
            diag_cost = (d["in"] * din + d["out"] * dout) / 1e6
            ask_cost = (a["in"] * din + a["out"] * dout) / 1e6
            # What it would cost if the big model read every window too.
            ultra_only = ((t["in"] + d["in"] + a["in"]) * din + (t["out"] + d["out"] + a["out"]) * dout) / 1e6
            total = triage_cost + diag_cost + ask_cost
            return {
                "triage": {**t, "cost": round(triage_cost, 5),
                           "avg_seconds": round(t["seconds"] / t["calls"], 2) if t["calls"] else None},
                "diagnosis": {**d, "cost": round(diag_cost, 5),
                              "avg_seconds": round(d["seconds"] / d["calls"], 2) if d["calls"] else None},
                "ask": {**a, "cost": round(ask_cost, 5),
                        "avg_seconds": round(a["seconds"] / a["calls"], 2) if a["calls"] else None},
                "total_cost": round(total, 5),
                "ultra_only_cost": round(ultra_only, 5),
                "saved_pct": round(100 * (1 - total / ultra_only), 1) if ultra_only else 0.0,
            }


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (handles think tags and code fences)."""
    if not text:
        raise ValueError("empty reply")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    text = text.replace("```json", "```")
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object found in reply")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.usage = UsageTracker(settings)
        self.mock = settings.mock
        self.models = {"triage": settings.triage_model, "diagnosis": settings.diagnosis_model}
        self.model_note = ""
        self._thinking_toggle_ok = True
        self.fast = False  # demo mode: skip the pretend network delay (benchmarks and tests)
        self.client = None
        if not self.mock:
            from openai import OpenAI

            self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=180)

    # ------------------------------------------------------------ discovery
    def discover_models(self) -> None:
        """Match configured model IDs against the live Token Factory catalog."""
        try:
            ids = [m.id for m in self.client.models.list().data]
        except Exception as exc:  # network or auth problem: keep configured IDs
            self.model_note = f"Couldn't list models ({exc.__class__.__name__}); using configured IDs."
            return
        notes = []
        for role, keywords in (("triage", ("nano", "super")), ("diagnosis", ("ultra", "super"))):
            wanted = self.models[role]
            if wanted in ids:
                continue
            same = [i for i in ids if i.lower() == wanted.lower()]
            if same:
                self.models[role] = same[0]
                continue
            picked = None
            for kw in keywords:
                cands = [i for i in ids if "nemotron" in i.lower() and kw in i.lower()]
                cands.sort(key=lambda i: ("nemotron-3" not in i.lower(), len(i)))
                if cands:
                    picked = cands[0]
                    break
            if picked:
                self.models[role] = picked
                notes.append(f"{role}: '{wanted}' not found, using '{picked}'")
            else:
                notes.append(f"{role}: no Nemotron match for '{wanted}'")
        self.model_note = "; ".join(notes)

    def list_nemotron_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list().data if "nemotron" in m.id.lower())

    # ----------------------------------------------------------------- calls
    def _chat(self, role: str, system: str, user: str, max_tokens: int) -> str:
        return self._chat_messages(role, [{"role": "system", "content": system},
                                          {"role": "user", "content": user}], max_tokens)

    def _chat_messages(self, role: str, messages: list[dict], max_tokens: int) -> str:
        kwargs = dict(model=self.models[MODEL_FOR[role]], temperature=0.2, max_tokens=max_tokens, messages=messages)
        if role == "triage" and self.settings.triage_disable_thinking and self._thinking_toggle_ok:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        start = time.time()
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "extra_body" not in kwargs or getattr(exc, "status_code", None) != 400:
                raise
            self._thinking_toggle_ok = False  # this deployment doesn't accept the toggle
            kwargs.pop("extra_body")
            resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        text = msg.content or getattr(msg, "reasoning_content", None) or ""
        usage = resp.usage
        self.usage.record(role,
                          usage.prompt_tokens if usage else _estimate_tokens("".join(m["content"] for m in messages)),
                          usage.completion_tokens if usage else _estimate_tokens(text),
                          time.time() - start)
        return text

    def _chat_json(self, role: str, system: str, user: str, max_tokens: int) -> dict:
        text = self._chat(role, system, user, max_tokens)
        try:
            return extract_json(text)
        except ValueError:
            retry = user + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
            return extract_json(self._chat(role, system, retry, max_tokens))

    def investigate_step(self, messages: list[dict], mock_state: dict | None = None) -> dict:
        """One turn of the investigation loop: a tool call or the final diagnosis."""
        if self.mock:
            step = mock_llm.investigate_next(mock_state or {})
            prompt = "".join(m["content"] for m in messages)
            return self._mock("diagnosis", prompt, step, 1.1 if "final" in step else 0.7)
        text = self._chat_messages("diagnosis", messages, 6000)
        try:
            return extract_json(text)
        except ValueError:
            retry = messages + [{"role": "assistant", "content": text[-2000:]},
                                {"role": "user", "content": "That was not valid JSON. Reply with ONLY one JSON object."}]
            return extract_json(self._chat_messages("diagnosis", retry, 6000))

    def ask_step(self, messages: list[dict], mock_state: dict | None = None) -> dict:
        """One turn of answering a question: a tool call or the answer."""
        if self.mock:
            step = mock_llm.ask_next(mock_state or {})
            prompt = "".join(m["content"] for m in messages)
            return self._mock("ask", prompt, step, 0.9 if "answer" in step else 0.5)
        text = self._chat_messages("ask", messages, 4000)
        try:
            return extract_json(text)
        except ValueError:
            retry = messages + [{"role": "assistant", "content": text[-2000:]},
                                {"role": "user", "content": "That was not valid JSON. Reply with ONLY one JSON object."}]
            return extract_json(self._chat_messages("ask", retry, 4000))

    def _mock(self, role: str, prompt: str, result: dict, delay: float) -> dict:
        if not self.fast:
            time.sleep(delay)  # feel like a real network call in demos
        self.usage.record(role, _estimate_tokens(prompt), _estimate_tokens(json.dumps(result)), delay)
        return result

    def triage(self, digest: str) -> dict:
        if self.mock:
            return self._mock("triage", prompts.TRIAGE_SYSTEM + digest, mock_llm.triage(digest), 0.4)
        return self._chat_json("triage", prompts.TRIAGE_SYSTEM, digest, 2048)

    def diagnose(self, context: str, external: bool = False) -> dict:
        user = prompts.EXTERNAL_NOTE + context if external else context
        if self.mock:
            return self._mock("diagnosis", prompts.DIAGNOSIS_SYSTEM + user,
                              mock_llm.diagnose(context, external), 1.6)
        return self._chat_json("diagnosis", prompts.DIAGNOSIS_SYSTEM, user, 8192)

    def extract(self, chunk: str) -> dict:
        """Nano filters a long log chunk down to the lines that matter."""
        if self.mock:
            return self._mock("triage", prompts.EXTRACT_SYSTEM + chunk, mock_llm.extract(chunk), 0.3)
        return self._chat_json("triage", prompts.EXTRACT_SYSTEM, chunk, 4096)
