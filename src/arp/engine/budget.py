"""Per-run budgets: tokens, tool calls, LLM calls, wall time and cost.

Usage is persisted with the run, so a resumed run continues from what it already spent.
Cost is computed from a configured price table; the default local model costs 0, so cost
limits are exercised in tests with an explicit price (no monetary cost was measured).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):  # noqa: N818 - control-flow signal
    def __init__(self, kind: str, used: float, limit: float):
        super().__init__(f"{kind} budget exceeded: {used:g} > {limit:g}")
        self.details = {"kind": kind, "used": used, "limit": limit}


@dataclass
class Prices:
    usd_per_1m_input: float = 0.0
    usd_per_1m_output: float = 0.0


@dataclass
class Budget:
    max_tokens: int = 20000
    max_tool_calls: int = 30
    max_llm_calls: int = 12
    max_wall_s: float = 600.0
    max_cost_usd: float = 1.0
    prices: Prices = field(default_factory=Prices)
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    active_s: float = 0.0  # executor-active time across resumes (excludes approval waits)
    _t0: float = field(default_factory=time.perf_counter, repr=False)

    @classmethod
    def from_run(cls, limits: dict, usage: dict, prices: Prices | None = None) -> Budget:
        b = cls(**{k: v for k, v in limits.items() if k in cls.__dataclass_fields__ and not k.startswith("_")})
        if prices:
            b.prices = prices
        for k in ("input_tokens", "output_tokens", "tool_calls", "llm_calls", "active_s"):
            setattr(b, k, usage.get(k, 0))
        return b

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens * self.prices.usd_per_1m_input + self.output_tokens * self.prices.usd_per_1m_output) / 1e6

    def wall(self) -> float:
        return self.active_s + (time.perf_counter() - self._t0)

    def check(self) -> None:
        if self.input_tokens + self.output_tokens > self.max_tokens:
            raise BudgetExceeded("tokens", self.input_tokens + self.output_tokens, self.max_tokens)
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded("tool_calls", self.tool_calls, self.max_tool_calls)
        if self.llm_calls > self.max_llm_calls:
            raise BudgetExceeded("llm_calls", self.llm_calls, self.max_llm_calls)
        if self.wall() > self.max_wall_s:
            raise BudgetExceeded("wall_s", self.wall(), self.max_wall_s)
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("cost_usd", self.cost_usd, self.max_cost_usd)

    def add_llm(self, input_tokens: int, output_tokens: int) -> None:
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def usage(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": self.tool_calls,
            "llm_calls": self.llm_calls,
            "active_s": round(self.wall(), 3),
            "cost_usd": round(self.cost_usd, 6),
        }
