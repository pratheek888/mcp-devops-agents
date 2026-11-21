"""Guardrails: read-only-by-default tool policy, token/tool budgets with cost tracking, and an audit log."""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Approver = Callable[[str, str, dict[str, Any]], bool | Awaitable[bool]]


class BudgetExceeded(RuntimeError):
    """Raised when a session goes over its LLM-call, tool-call or token budget."""


@dataclass
class ToolPolicy:
    """Decides whether an agent may call a tool.

    Tools the MCP server marks `readOnlyHint=True` are always allowed. Anything else is a write:
    blocked unless `allow_writes` is set, and even then each call goes through `approver`
    (a human in the CLI) unless `auto_approve` is set.
    """

    allow_writes: bool = False
    auto_approve: bool = False
    approver: Approver | None = None
    denied_tools: frozenset[str] = frozenset()

    async def check(self, agent: str, tool: str, read_only: bool, args: dict[str, Any]) -> str | None:
        """Return None if allowed, otherwise the reason for denial (sent back to the model)."""
        if tool in self.denied_tools:
            return f"tool '{tool}' is disabled by policy"
        if read_only:
            return None
        if not self.allow_writes:
            return (
                f"BLOCKED by policy: '{tool}' changes the system and this session is read-only. "
                "Recommend the action to the human instead (include the exact command)."
            )
        if self.auto_approve:
            return None
        if self.approver is None:
            return f"BLOCKED: '{tool}' needs human approval and no approver is configured"
        decision = self.approver(agent, tool, args)
        if inspect.isawaitable(decision):
            decision = await decision
        return None if decision else f"DENIED by operator: '{tool}' was not approved"


@dataclass
class Budget:
    max_llm_calls: int = 30
    max_tool_calls: int = 60
    max_tokens: int = 400_000
    max_tool_output_chars: int = 6_000
    # Estimated USD per million tokens; set these to your model's actual price.
    price_input_per_mtok: float = 3.0
    price_output_per_mtok: float = 15.0


@dataclass
class AgentUsage:
    llm_calls: int = 0
    tool_calls: int = 0
    blocked_tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class UsageTracker:
    budget: Budget = field(default_factory=Budget)
    per_agent: dict[str, AgentUsage] = field(default_factory=dict)

    def _agent(self, agent: str) -> AgentUsage:
        return self.per_agent.setdefault(agent, AgentUsage())

    @property
    def totals(self) -> AgentUsage:
        t = AgentUsage()
        for u in self.per_agent.values():
            t.llm_calls += u.llm_calls
            t.tool_calls += u.tool_calls
            t.blocked_tool_calls += u.blocked_tool_calls
            t.input_tokens += u.input_tokens
            t.output_tokens += u.output_tokens
        return t

    def before_llm_call(self, agent: str) -> None:
        t = self.totals
        if t.llm_calls >= self.budget.max_llm_calls:
            raise BudgetExceeded(f"LLM call budget reached ({self.budget.max_llm_calls})")
        if t.input_tokens + t.output_tokens >= self.budget.max_tokens:
            raise BudgetExceeded(f"token budget reached ({self.budget.max_tokens:,})")

    def record_llm(self, agent: str, input_tokens: int, output_tokens: int) -> None:
        u = self._agent(agent)
        u.llm_calls += 1
        u.input_tokens += input_tokens
        u.output_tokens += output_tokens

    def before_tool_call(self, agent: str) -> None:
        if self.totals.tool_calls >= self.budget.max_tool_calls:
            raise BudgetExceeded(f"tool call budget reached ({self.budget.max_tool_calls})")

    def record_tool(self, agent: str, blocked: bool = False) -> None:
        u = self._agent(agent)
        u.tool_calls += 1
        if blocked:
            u.blocked_tool_calls += 1

    def cost(self, u: AgentUsage) -> float:
        b = self.budget
        return u.input_tokens / 1e6 * b.price_input_per_mtok + u.output_tokens / 1e6 * b.price_output_per_mtok

    def report(self) -> list[dict[str, Any]]:
        rows = [
            {"agent": name, **vars(u), "est_cost_usd": round(self.cost(u), 4)}
            for name, u in self.per_agent.items()
        ]
        t = self.totals
        rows.append({"agent": "TOTAL", **vars(t), "est_cost_usd": round(self.cost(t), 4)})
        return rows


_SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|credential)", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "***" if _SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass
class AuditLog:
    """Append-only JSONL record of every tool call: who, what, allowed or not, how long."""

    path: Path | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        agent: str,
        tool: str,
        args: dict[str, Any],
        allowed: bool,
        is_error: bool,
        duration_ms: int,
        note: str | None = None,
    ) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent": agent,
            "tool": tool,
            "args": redact(args),
            "allowed": allowed,
            "is_error": is_error,
            "duration_ms": duration_ms,
        }
        if note:
            entry["note"] = note
        self.entries.append(entry)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
