"""Specialist agents: a tool-use loop over the MCP servers assigned to them."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client

from .guardrails import AuditLog, BudgetExceeded, ToolPolicy, UsageTracker
from .llm import LLM
from .registry import SpecialistSpec

EventHook = Callable[[str, str, dict[str, Any]], None]


def _noop(kind: str, agent: str, data: dict[str, Any]) -> None:
    return None


@dataclass
class ToolBinding:
    client: Client
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool

    def anthropic_tool(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class AgentRuntime:
    """Shared services for all agents in one session."""

    llm: LLM
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    usage: UsageTracker = field(default_factory=UsageTracker)
    audit: AuditLog = field(default_factory=AuditLog)
    on_event: EventHook = _noop


def _result_text(result: Any) -> str:
    parts = []
    for c in result.content or []:
        text = getattr(c, "text", None)
        parts.append(text if text is not None else json.dumps(c.model_dump(exclude_none=True)))
    return "\n".join(parts) if parts else "(empty result)"


class SpecialistAgent:
    def __init__(self, spec: SpecialistSpec, runtime: AgentRuntime):
        self.spec = spec
        self.rt = runtime
        self.tools: dict[str, ToolBinding] = {}
        self._stack: AsyncExitStack | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    async def __aenter__(self) -> SpecialistAgent:
        self._stack = AsyncExitStack()
        try:
            for server in self.spec.servers:
                client = await self._stack.enter_async_context(Client(server.connect_target()))
                listed = await client.list_tools()
                for t in listed.tools:
                    if t.name in self.tools:
                        raise ValueError(f"duplicate tool {t.name!r} in agent {self.name!r}")
                    ann = t.annotations
                    self.tools[t.name] = ToolBinding(
                        client=client,
                        server=server.name,
                        name=t.name,
                        description=t.description or "",
                        input_schema=t.input_schema,
                        # MCP spec: readOnlyHint defaults to false, so unannotated tools count as writes.
                        read_only=bool(ann and ann.read_only_hint),
                    )
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack:
            await self._stack.aclose()

    async def _call_tool(self, use: dict[str, Any]) -> dict[str, Any]:
        name, args = use["name"], use.get("input") or {}
        binding = self.tools.get(name)
        started = time.monotonic()
        if binding is None:
            content, is_error, allowed = f"unknown tool {name!r}", True, False
        else:
            self.rt.usage.before_tool_call(self.name)
            denial = await self.rt.policy.check(self.name, name, binding.read_only, args)
            if denial:
                content, is_error, allowed = denial, True, False
            else:
                allowed = True
                try:
                    result = await binding.client.call_tool(name, args)
                    content, is_error = _result_text(result), bool(result.is_error)
                except Exception as e:  # transport / server failure becomes a tool error for the model
                    content, is_error = f"tool call failed: {e}", True
        limit = self.rt.usage.budget.max_tool_output_chars
        if len(content) > limit:
            content = content[:limit] + f"\n... [truncated {len(content) - limit} chars]"
        duration = int((time.monotonic() - started) * 1000)
        self.rt.usage.record_tool(self.name, blocked=not allowed)
        self.rt.audit.record(
            agent=self.name, tool=name, args=args, allowed=allowed, is_error=is_error, duration_ms=duration
        )
        self.rt.on_event(
            "tool",
            self.name,
            {
                "tool": name,
                "args": args,
                "allowed": allowed,
                "is_error": is_error,
                "read_only": binding.read_only if binding else None,
            },
        )
        return {"type": "tool_result", "tool_use_id": use["id"], "content": content, "is_error": is_error}

    async def run(self, task: str) -> str:
        """Run one delegated task to completion and return the agent's report."""
        self.rt.on_event("start", self.name, {"task": task})
        tools = [b.anthropic_tool() for b in self.tools.values()]
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        for _ in range(self.spec.max_turns):
            try:
                self.rt.usage.before_llm_call(self.name)
            except BudgetExceeded as e:
                return f"[{self.name}] stopped: {e}"
            resp = await self.rt.llm.complete(system=self.spec.system_prompt, messages=messages, tools=tools)
            self.rt.usage.record_llm(self.name, resp.input_tokens, resp.output_tokens)
            messages.append({"role": "assistant", "content": resp.content})
            if not resp.tool_uses:
                report = resp.text or "(no report)"
                self.rt.on_event("done", self.name, {"report": report})
                return report
            results = []
            for use in resp.tool_uses:
                try:
                    results.append(await self._call_tool(use))
                except BudgetExceeded as e:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use["id"],
                            "content": f"budget exhausted: {e}. Write your report now.",
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": results})
        return f"[{self.name}] stopped after {self.spec.max_turns} turns without a final report"
