"""The servers work as real subprocesses over stdio, exactly as the agents (or Claude Desktop) run them."""

from __future__ import annotations

import pytest

from devops_agents.agents.guardrails import ToolPolicy
from devops_agents.agents.registry import DEFAULT_SPECIALISTS
from devops_agents.agents.specialist import AgentRuntime, SpecialistAgent


class _NoLLM:
    async def complete(self, **_):
        raise AssertionError("not used")


@pytest.mark.parametrize("spec", DEFAULT_SPECIALISTS, ids=lambda s: s.name)
async def test_stdio_server_starts_and_lists_tools(spec):
    async with SpecialistAgent(spec, AgentRuntime(llm=_NoLLM(), policy=ToolPolicy())) as agent:
        assert agent.tools
        assert any(t.read_only for t in agent.tools.values())


async def test_stdio_tool_call():
    k8s = DEFAULT_SPECIALISTS[0]
    async with SpecialistAgent(k8s, AgentRuntime(llm=_NoLLM())) as agent:
        result = await agent._call_tool({"id": "t1", "name": "list_namespaces", "input": {}})
        assert result["is_error"] is False
        assert "prod" in result["content"]
