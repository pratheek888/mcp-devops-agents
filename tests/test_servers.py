"""MCP servers over the real protocol (in-process transport) against mock backends."""

from __future__ import annotations

import json

from mcp import Client

from devops_agents.backends.mock import MockJenkins, MockKubernetes, MockPrometheus
from devops_agents.servers import jenkins, kubernetes, prometheus


def _data(result):
    return (
        json.loads(result.content[0].text)
        if result.content[0].text.strip()[:1] in "[{"
        else result.content[0].text
    )


async def test_kubernetes_tools_and_annotations(scenario):
    async with Client(kubernetes.build_server(MockKubernetes(scenario))) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["list_pods"].annotations.read_only_hint is True
        assert tools["rollback_deployment"].annotations.read_only_hint is False
        assert tools["rollback_deployment"].annotations.destructive_hint is True

        pods = await c.call_tool("list_pods", {"namespace": "prod", "selector": "app=payments-api"})
        statuses = {p["status"] for p in pods.structured_content["result"]}
        assert statuses == {"CrashLoopBackOff"}

        logs = await c.call_tool(
            "get_pod_logs", {"namespace": "prod", "name": "payments-api-6c8f9d7b5-x2kqp"}
        )
        assert "PAYMENTS_DB_HOST is empty" in logs.content[0].text


async def test_unknown_namespace_is_tool_error(scenario):
    async with Client(kubernetes.build_server(MockKubernetes(scenario))) as c:
        r = await c.call_tool("list_pods", {"namespace": "nope"})
        assert r.is_error
        assert "not found" in r.content[0].text


async def test_scale_validation(scenario):
    async with Client(kubernetes.build_server(MockKubernetes(scenario))) as c:
        r = await c.call_tool(
            "scale_deployment", {"namespace": "prod", "deployment": "payments-api", "replicas": 500}
        )
        assert r.is_error


async def test_rollback_recovers_metrics_across_servers(scenario):
    k8s = kubernetes.build_server(MockKubernetes(scenario))
    prom = prometheus.build_server(MockPrometheus(scenario))
    async with Client(prom) as p:
        before = (await p.call_tool("get_error_rate", {"service": "payments-api"})).structured_content
        assert before["error_rate"] == 0.38
        slo = (await p.call_tool("get_slo_status", {"service": "payments-api"})).structured_content
        assert slo["status"] == "breaching" and slo["burn_rate"] == 380.0
    async with Client(k8s) as k:
        msg = await k.call_tool("rollback_deployment", {"namespace": "prod", "deployment": "payments-api"})
        assert "revision 13" in msg.content[0].text
        deps = (await k.call_tool("list_deployments", {"namespace": "prod"})).structured_content["result"]
        pay = next(d for d in deps if d["name"] == "payments-api")
        assert pay["ready"] == 3 and pay["image"].endswith(":1.7.3")
    async with Client(prom) as p:
        after = (await p.call_tool("get_error_rate", {"service": "payments-api"})).structured_content
        assert after["error_rate"] == 0.001
        alerts = (await p.call_tool("list_firing_alerts", {})).structured_content["result"]
        assert alerts == []


async def test_jenkins_change_set(scenario):
    async with Client(jenkins.build_server(MockJenkins(scenario))) as c:
        b = (await c.call_tool("get_build", {"job": "payments-api-deploy", "number": 142})).structured_content
        assert b["changes"][0]["message"].startswith("refactor(config): rename DB_HOST")
        log = await c.call_tool("get_build_log", {"job": "payments-api-deploy", "number": 142})
        assert "still sets env DB_HOST" in log.content[0].text
