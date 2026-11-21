"""Live HTTP backends against fake transports (no network)."""

from __future__ import annotations

import httpx

from devops_agents.backends.live import LiveJenkins, LivePrometheus


def test_prometheus_error_rate_and_alerts():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/api/v1/query":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"result": [{"metric": {}, "value": [1732200000, "0.25"]}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "alerts": [
                        {
                            "labels": {"alertname": "HighErrors", "severity": "critical", "service": "api"},
                            "annotations": {"summary": "x"},
                            "state": "firing",
                            "activeAt": "t",
                        },
                        {"labels": {"alertname": "Pending"}, "state": "pending"},
                    ]
                }
            },
        )

    prom = LivePrometheus("http://prom", transport=httpx.MockTransport(handler))
    assert prom.error_rate("api", "10m") == 0.25
    q = seen[0].url.params["query"]
    assert 'service="api"' in q and 'status=~"5.."' in q and "[10m]" in q
    alerts = prom.firing_alerts()
    assert [a["name"] for a in alerts] == ["HighErrors"]


def test_jenkins_recent_builds_parses_parameters():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/job/team/job/deploy/api/json"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "builds": [
                    {
                        "number": 7,
                        "result": "FAILURE",
                        "timestamp": 1732200000000,
                        "duration": 61000,
                        "actions": [{"parameters": [{"name": "ENV", "value": "prod"}]}, {}],
                    },
                ]
            },
        )

    j = LiveJenkins("http://jenkins", "u", "t", transport=httpx.MockTransport(handler))
    builds = j.recent_builds("team/deploy", 1)
    assert builds[0]["parameters"] == {"ENV": "prod"} and builds[0]["duration_s"] == 61
