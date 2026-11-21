from __future__ import annotations

import json

import pytest

from devops_agents.agents.guardrails import AuditLog, Budget, BudgetExceeded, ToolPolicy, UsageTracker, redact


async def test_read_only_tools_always_allowed():
    assert await ToolPolicy().check("k8s", "list_pods", True, {}) is None


async def test_writes_blocked_by_default():
    reason = await ToolPolicy().check("k8s", "rollback_deployment", False, {})
    assert reason and reason.startswith("BLOCKED")


async def test_writes_need_approval():
    asked = []

    def approver(agent, tool, args):
        asked.append(tool)
        return False

    reason = await ToolPolicy(allow_writes=True, approver=approver).check(
        "k8s", "rollback_deployment", False, {}
    )
    assert asked == ["rollback_deployment"]
    assert reason and reason.startswith("DENIED")


async def test_async_approver_and_auto_approve():
    async def yes(*_):
        return True

    assert await ToolPolicy(allow_writes=True, approver=yes).check("k8s", "restart", False, {}) is None
    assert await ToolPolicy(allow_writes=True, auto_approve=True).check("k8s", "restart", False, {}) is None


async def test_denylist_beats_everything():
    p = ToolPolicy(allow_writes=True, auto_approve=True, denied_tools=frozenset({"query_promql"}))
    assert await p.check("obs", "query_promql", True, {}) is not None


def test_budget_limits_and_cost():
    u = UsageTracker(
        Budget(max_llm_calls=2, max_tokens=10_000, price_input_per_mtok=3, price_output_per_mtok=15)
    )
    u.before_llm_call("a")
    u.record_llm("a", 1_000_000, 100_000)
    with pytest.raises(BudgetExceeded):
        u.before_llm_call("a")  # token budget
    total = u.report()[-1]
    assert total["agent"] == "TOTAL" and total["est_cost_usd"] == 4.5


def test_tool_budget():
    u = UsageTracker(Budget(max_tool_calls=1))
    u.before_tool_call("a")
    u.record_tool("a")
    with pytest.raises(BudgetExceeded):
        u.before_tool_call("a")


def test_audit_redacts_secrets(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(
        agent="cicd",
        tool="trigger_build",
        args={"job": "x", "parameters": {"API_TOKEN": "abc"}},
        allowed=False,
        is_error=True,
        duration_ms=1,
    )
    line = json.loads((tmp_path / "audit.jsonl").read_text())
    assert line["args"]["parameters"]["API_TOKEN"] == "***"
    assert redact({"password": "p", "user": "u"}) == {"password": "***", "user": "u"}
