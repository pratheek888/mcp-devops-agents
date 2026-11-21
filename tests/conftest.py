from __future__ import annotations

import itertools
from typing import Any

import pytest

from devops_agents.agents.llm import LLMResponse
from devops_agents.agents.registry import CICD, KUBERNETES, OBSERVABILITY, ServerSpec, SpecialistSpec
from devops_agents.backends.mock import MockJenkins, MockKubernetes, MockPrometheus, load_scenario
from devops_agents.servers import jenkins, kubernetes, prometheus


@pytest.fixture(autouse=True)
def isolated_mock_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVOPS_MOCK_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DEVOPS_BACKEND", "mock")


@pytest.fixture
def scenario():
    return load_scenario()


@pytest.fixture
def in_process_specialists(scenario) -> tuple[SpecialistSpec, ...]:
    """Default specialists wired to in-process MCP servers (no subprocesses)."""

    def swap(spec: SpecialistSpec, server) -> SpecialistSpec:
        return SpecialistSpec(
            spec.name,
            spec.description,
            spec.system_prompt,
            (ServerSpec(spec.servers[0].name, target=server),),
            spec.max_turns,
        )

    return (
        swap(KUBERNETES, kubernetes.build_server(MockKubernetes(scenario))),
        swap(CICD, jenkins.build_server(MockJenkins(scenario))),
        swap(OBSERVABILITY, prometheus.build_server(MockPrometheus(scenario))),
    )


_ids = itertools.count(1)


def tool_use(_tool: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": _tool, "input": inp}


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


class ScriptedLLM:
    """Fake LLM. `script` maps a system-prompt marker to a queue of responses (lists of blocks)."""

    def __init__(self, script: dict[str, list[list[dict[str, Any]]]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, system, messages, tools, max_tokens=4096) -> LLMResponse:
        key = next(k for k in self.script if k in system)
        self.calls.append({"agent": key, "messages": messages, "tools": [t["name"] for t in tools]})
        blocks = self.script[key].pop(0)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return LLMResponse(blocks, stop, input_tokens=1000, output_tokens=200)
