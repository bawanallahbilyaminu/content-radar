"""Claude client access, token/cost accounting, and the per-run cost guard."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import anthropic

from content_radar.config import Settings

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


class ModelClient(Protocol):
    """The slice of anthropic.Anthropic we use. Tests supply a fake."""

    messages: Any


def make_client(settings: Settings) -> anthropic.Anthropic:
    if not settings.secrets.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot run LLM steps")
    return anthropic.Anthropic(api_key=settings.secrets.anthropic_api_key, max_retries=2)


# --- cost accounting -------------------------------------------------------------


def price_usd(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = settings.budget.pricing.get(model)
    if pricing is None:
        log.warning("no pricing configured for %s; counting it as $0", model)
        return 0.0
    return (input_tokens * pricing.input + output_tokens * pricing.output) / 1_000_000


@dataclass
class Call:
    step: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0


@dataclass
class UsageTracker:
    settings: Settings
    calls: list[Call] = field(default_factory=list)

    def record(self, step: str, model: str, usage: Any) -> Call:
        call = Call(
            step=step,
            model=model,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        )
        call.cost_usd = price_usd(self.settings, model, call.input_tokens, call.output_tokens)
        self.calls.append(call)
        log.info(
            "llm %s %s: in=%d out=%d cache_read=%d cost=$%.4f",
            step,
            model,
            call.input_tokens,
            call.output_tokens,
            call.cache_read,
            call.cost_usd,
        )
        return call

    @property
    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    def calls_for(self, step: str) -> int:
        return sum(1 for c in self.calls if c.step == step)

    def summary(self) -> str:
        if not self.calls:
            return "llm usage: no calls"
        by_step: dict[str, list[Call]] = {}
        for c in self.calls:
            by_step.setdefault(c.step, []).append(c)
        parts = []
        for step, calls in by_step.items():
            parts.append(
                f"{step}: {len(calls)} call(s), in={sum(c.input_tokens for c in calls)}, "
                f"out={sum(c.output_tokens for c in calls)}, "
                f"${sum(c.cost_usd for c in calls):.4f}"
            )
        parts.append(
            f"total ${self.total_cost:.4f} "
            f"(budget ${self.settings.budget.max_cost_usd_per_run:.2f})"
        )
        return "llm usage: " + "; ".join(parts)


def count_input_tokens(client: ModelClient, model: str, system: str, user: str) -> int:
    """Exact count via the free count_tokens endpoint; falls back to a chars/3.5 estimate."""
    try:
        resp = client.messages.count_tokens(
            model=model, system=system, messages=[{"role": "user", "content": user}]
        )
        return int(resp.input_tokens)
    except Exception as exc:  # noqa: BLE001 - estimation must never kill the run
        log.warning("count_tokens failed (%s); using character estimate", exc)
        return int((len(system) + len(user)) / 3.5)


class BudgetExceeded(Exception):
    pass


def check_budget(
    settings: Settings, tracker: UsageTracker, step: str, model: str, est_in: int, est_out: int
) -> None:
    """Raise BudgetExceeded when spent + this step's estimate would pass the per-run cap."""
    projected = price_usd(settings, model, est_in, est_out)
    total = tracker.total_cost + projected
    cap = settings.budget.max_cost_usd_per_run
    log.info(
        "cost guard %s: spent $%.4f + est $%.4f (in=%d out=%d) = $%.4f vs cap $%.2f",
        step,
        tracker.total_cost,
        projected,
        est_in,
        est_out,
        total,
        cap,
    )
    if total > cap:
        raise BudgetExceeded(
            f"skipping {step}: projected run cost ${total:.4f} exceeds budget "
            f"${cap:.2f} (spent ${tracker.total_cost:.4f}, this step est. ${projected:.4f} "
            f"for ~{est_in} input + ~{est_out} output tokens on {model})"
        )
