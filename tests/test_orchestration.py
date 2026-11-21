"""End-to-end orchestration with a scripted LLM: master → specialists → MCP tools → report."""

from __future__ import annotations

from conftest import ScriptedLLM, text, tool_use

from devops_agents.agents import AgentRuntime, Budget, MasterAgent, ToolPolicy, UsageTracker

POD = "payments-api-6c8f9d7b5-x2kqp"


def incident_script(remediate: bool) -> dict:
    master = [
        # Turn 1: fan out to three specialists in parallel.
        [
            text("Investigating in parallel."),
            tool_use("ask_kubernetes_agent", task="Why is payments-api failing in namespace prod?"),
            tool_use("ask_cicd_agent", task="What was deployed for payments-api recently and what changed?"),
            tool_use("ask_observability_agent", task="Quantify impact for payments-api."),
        ],
    ]
    if remediate:
        master += [
            [tool_use("ask_kubernetes_agent", task="Roll back prod/payments-api to the previous revision.")],
            [tool_use("ask_observability_agent", task="Verify payments-api recovered.")],
        ]
    master.append([text("## Summary\nBad config in 1.8.0 (PAYMENTS_DB_HOST unset).")])

    k8s = [
        [tool_use("list_pods", namespace="prod", selector="app=payments-api")],
        [tool_use("get_pod_logs", namespace="prod", name=POD)],
        [text("Pods CrashLoopBackOff: PAYMENTS_DB_HOST is empty.")],
    ]
    if remediate:
        k8s += [
            [tool_use("rollback_deployment", namespace="prod", deployment="payments-api")],
            [text("Rollback result reported.")],
        ]
    cicd = [
        [tool_use("get_recent_builds", job="payments-api-deploy", limit=2)],
        [tool_use("get_build", job="payments-api-deploy", number=142)],
        [text("Build #142 renamed DB_HOST to PAYMENTS_DB_HOST.")],
    ]
    obs = [
        [tool_use("get_error_rate", service="payments-api"), tool_use("list_firing_alerts")],
        [text("Error rate 38%.")],
    ]
    if remediate:
        obs += [[tool_use("get_error_rate", service="payments-api")], [text("Recovered: 0.1%.")]]
    return {
        "incident commander": master,
        "Kubernetes SRE": k8s,
        "CI/CD release-engineering": cicd,
        "observability agent": obs,
    }


def _tool_results(llm: ScriptedLLM, agent_key: str) -> list[dict]:
    out = []
    for call in llm.calls:
        if call["agent"] != agent_key:
            continue
        for m in call["messages"]:
            if m["role"] == "user" and isinstance(m["content"], list):
                out += [c for c in m["content"] if c.get("type") == "tool_result"]
    return out


async def test_read_only_investigation(in_process_specialists):
    llm = ScriptedLLM(incident_script(remediate=False))
    rt = AgentRuntime(llm=llm)
    async with MasterAgent(rt, in_process_specialists) as master:
        result = await master.run("payments-api is down")

    assert result.answer.startswith("## Summary")
    assert {d.agent for d in result.delegations} == {"kubernetes", "cicd", "observability"}
    # Specialists saw only their own tools.
    k8s_call = next(c for c in llm.calls if c["agent"] == "Kubernetes SRE")
    assert "list_pods" in k8s_call["tools"] and "get_build" not in k8s_call["tools"]
    # Real MCP data reached the model.
    k8s_results = _tool_results(llm, "Kubernetes SRE")
    assert any("PAYMENTS_DB_HOST is empty" in r["content"] for r in k8s_results)
    assert all(e["allowed"] for e in rt.audit.entries)
    total = result.usage[-1]
    assert total["agent"] == "TOTAL" and total["llm_calls"] == len(llm.calls)


async def test_write_blocked_in_read_only_mode(in_process_specialists):
    llm = ScriptedLLM(incident_script(remediate=True))
    rt = AgentRuntime(llm=llm, policy=ToolPolicy(allow_writes=False))
    async with MasterAgent(rt, in_process_specialists) as master:
        await master.run("fix payments-api")

    rollback = next(e for e in rt.audit.entries if e["tool"] == "rollback_deployment")
    assert rollback["allowed"] is False
    blocked = [r for r in _tool_results(llm, "Kubernetes SRE") if r["is_error"]]
    assert any("BLOCKED by policy" in r["content"] for r in blocked)
    # Without the rollback, metrics are still bad.
    obs_results = _tool_results(llm, "observability agent")
    assert '"error_rate": 0.38' in obs_results[-1]["content"]


async def test_approved_rollback_recovers_service(in_process_specialists):
    approvals = []

    def approve(agent, tool, args):
        approvals.append((agent, tool, args["deployment"]))
        return True

    llm = ScriptedLLM(incident_script(remediate=True))
    rt = AgentRuntime(llm=llm, policy=ToolPolicy(allow_writes=True, approver=approve))
    async with MasterAgent(rt, in_process_specialists) as master:
        await master.run("fix payments-api")

    assert approvals == [("kubernetes", "rollback_deployment", "payments-api")]
    obs_results = _tool_results(llm, "observability agent")
    assert '"error_rate": 0.001' in obs_results[-1]["content"]
    assert rt.usage.per_agent["kubernetes"].blocked_tool_calls == 0


async def test_budget_stops_session(in_process_specialists):
    llm = ScriptedLLM(incident_script(remediate=False))
    rt = AgentRuntime(llm=llm, usage=UsageTracker(Budget(max_llm_calls=2)))
    async with MasterAgent(rt, in_process_specialists) as master:
        result = await master.run("payments-api is down")
    assert result.stopped_reason and "LLM call budget" in result.stopped_reason
    assert rt.usage.totals.llm_calls <= 2 + 3  # master + at most one call per parallel specialist
