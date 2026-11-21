"""Which specialist agents exist, what they are for, and which MCP servers each one gets."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

from mcp import StdioServerParameters

_COMMON = """
Rules:
- Investigate with read-only tools first. Quote exact evidence (resource names, statuses,
  log lines, build numbers, metric values). Never invent data you did not get from a tool.
- If a tool result says BLOCKED or DENIED, do not retry it. Report what you would have done
  and give the exact command a human can run.
- Only call write tools when the task explicitly asks you to remediate.
- Finish with a short, structured report: Findings, Evidence, Assessment (with confidence),
  and Suggested next step. Keep it under 250 words.
""".strip()


@dataclass(frozen=True)
class ServerSpec:
    """How to reach an MCP server: a module run over stdio, or an in-process server (tests)."""

    name: str
    module: str | None = None
    target: Any | None = None  # e.g. an MCPServer instance for in-process use
    env: dict[str, str] = field(default_factory=dict)

    def connect_target(self) -> Any:
        if self.target is not None:
            return self.target
        if not self.module:
            raise ValueError(f"server {self.name!r} needs a module or a target")
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self.module],
            env={**os.environ, **self.env},
        )


@dataclass(frozen=True)
class SpecialistSpec:
    name: str
    description: str
    system_prompt: str
    servers: tuple[ServerSpec, ...]
    max_turns: int = 10


KUBERNETES = SpecialistSpec(
    name="kubernetes",
    description=(
        "Kubernetes SRE. Inspects namespaces, pods, logs, events, deployments and rollout history, "
        "and (only when asked and allowed) restarts, scales or rolls back deployments."
    ),
    system_prompt=(
        "You are a senior Kubernetes SRE agent with tools for one cluster. Find out what is wrong with "
        "workloads: status, restarts, termination reasons, logs, events, images and rollout history. "
        "For remediation prefer rolling back to the last healthy revision over restarts.\n\n" + _COMMON
    ),
    servers=(ServerSpec("kubernetes", module="devops_agents.servers.kubernetes"),),
)

CICD = SpecialistSpec(
    name="cicd",
    description=(
        "CI/CD release engineer. Finds which builds and deployments ran, when, with what parameters, "
        "and exactly what changed (commits, authors, files, console log warnings)."
    ),
    system_prompt=(
        "You are a CI/CD release-engineering agent with Jenkins tools. Correlate deployments with "
        "incidents: identify the build that shipped the current version, the previous good build, the "
        "code/config changes between them, and any warnings in the pipeline logs.\n\n" + _COMMON
    ),
    servers=(ServerSpec("jenkins", module="devops_agents.servers.jenkins"),),
)

OBSERVABILITY = SpecialistSpec(
    name="observability",
    description=(
        "Observability / SLO analyst. Reports firing alerts, error rates, p95 latency and SLO burn "
        "rate for services, and confirms recovery after a fix."
    ),
    system_prompt=(
        "You are an observability agent with Prometheus tools. Quantify user impact: firing alerts, "
        "error rate, availability, p95 latency and SLO burn rate for affected and dependent services. "
        "When asked to verify a fix, compare against the incident values.\n\n" + _COMMON
    ),
    servers=(ServerSpec("prometheus", module="devops_agents.servers.prometheus"),),
)

DEFAULT_SPECIALISTS: tuple[SpecialistSpec, ...] = (KUBERNETES, CICD, OBSERVABILITY)
