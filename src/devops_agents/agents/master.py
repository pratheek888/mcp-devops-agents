"""Master (orchestrator) agent: plans, delegates to specialists in parallel, writes the incident report."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .guardrails import BudgetExceeded
from .registry import DEFAULT_SPECIALISTS, SpecialistSpec
from .specialist import AgentRuntime, SpecialistAgent

MASTER_PROMPT = """
You are the incident commander for a platform/DevOps team. You do not have direct access to
systems. Specialist agents do, and you reach them through the ask_<name>_agent tools.

How to work:
1. Work out what you need to know. Send each specialist a specific, self-contained task
   (namespace, service, time frame, what evidence you want). Specialists don't see each other's
   work or this conversation, so include the context they need.
2. Investigate in parallel: call several specialists in the same turn when their work is independent.
3. Correlate evidence across infrastructure, CI/CD and metrics. Ask follow-ups if something doesn't add up.
4. Remediation: only if the user asked for it. Tell the specialist exactly what to change. Then have the
   observability agent verify recovery. If an action was BLOCKED or DENIED, say so and give the command.
5. Never claim something was done unless a specialist reported that the tool succeeded.

Final answer (markdown):
## Summary         one or two sentences
## Impact          services, error rate / latency / SLO, alerts
## Root cause      with evidence and confidence (high / medium / low)
## Timeline        key timestamps
## Actions         each marked DONE, PROPOSED or BLOCKED
## Follow-ups      prevention items (tests, alerts, pipeline checks)
""".strip()


@dataclass
class Delegation:
    agent: str
    task: str
    report: str


@dataclass
class MasterResult:
    answer: str
    delegations: list[Delegation] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str | None = None


class MasterAgent:
    name = "master"

    def __init__(
        self,
        runtime: AgentRuntime,
        specialists: tuple[SpecialistSpec, ...] = DEFAULT_SPECIALISTS,
        max_turns: int = 8,
    ):
        self.rt = runtime
        self.specs = specialists
        self.max_turns = max_turns
        self.agents: dict[str, SpecialistAgent] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MasterAgent:
        self._stack = AsyncExitStack()
        try:
            for spec in self.specs:
                self.agents[spec.name] = await self._stack.enter_async_context(SpecialistAgent(spec, self.rt))
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack:
            await self._stack.aclose()

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"ask_{spec.name}_agent",
                "description": spec.description,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Self-contained task for the agent"}
                    },
                    "required": ["task"],
                },
            }
            for spec in self.specs
        ]

    async def _delegate(self, use: dict[str, Any], log: list[Delegation]) -> dict[str, Any]:
        agent_name = use["name"].removeprefix("ask_").removesuffix("_agent")
        task = (use.get("input") or {}).get("task", "")
        agent = self.agents.get(agent_name)
        if agent is None:
            report, is_error = f"no such specialist {agent_name!r}", True
        else:
            try:
                report, is_error = await agent.run(task), False
            except Exception as e:
                report, is_error = f"specialist {agent_name} failed: {e}", True
        log.append(Delegation(agent_name, task, report))
        return {"type": "tool_result", "tool_use_id": use["id"], "content": report, "is_error": is_error}

    async def run(self, question: str) -> MasterResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        delegations: list[Delegation] = []
        tools = self._tools()
        last_text = ""
        for _ in range(self.max_turns):
            try:
                self.rt.usage.before_llm_call(self.name)
            except BudgetExceeded as e:
                return MasterResult(
                    last_text or "Stopped before a final answer.",
                    delegations,
                    self.rt.usage.report(),
                    stopped_reason=str(e),
                )
            resp = await self.rt.llm.complete(
                system=MASTER_PROMPT, messages=messages, tools=tools, max_tokens=4096
            )
            self.rt.usage.record_llm(self.name, resp.input_tokens, resp.output_tokens)
            messages.append({"role": "assistant", "content": resp.content})
            last_text = resp.text or last_text
            if not resp.tool_uses:
                return MasterResult(resp.text, delegations, self.rt.usage.report())
            self.rt.on_event("delegate", self.name, {"to": [u["name"] for u in resp.tool_uses]})
            # Independent delegations run concurrently.
            results = await asyncio.gather(*(self._delegate(u, delegations) for u in resp.tool_uses))
            messages.append({"role": "user", "content": list(results)})
        return MasterResult(
            last_text or "Stopped before a final answer.",
            delegations,
            self.rt.usage.report(),
            stopped_reason=f"max turns ({self.max_turns}) reached",
        )
