"""Observability (Prometheus) MCP server: alerts, golden signals and PromQL. Read-only."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..backends import MetricsBackend, metrics_backend
from . import READ_ONLY, expected_errors


def build_server(backend: MetricsBackend) -> MCPServer:
    mcp = MCPServer("prometheus", instructions="Query metrics and alerts. All tools are read-only.")

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def list_firing_alerts() -> list[dict[str, Any]]:
        """Alerts currently firing, with severity, service and summary."""
        return backend.firing_alerts()

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_error_rate(service: str, window: str = "5m") -> dict[str, Any]:
        """5xx error ratio (0-1) for a service over a window like 5m or 1h."""
        rate = backend.error_rate(service, window)
        return {
            "service": service,
            "window": window,
            "error_rate": rate,
            "availability_pct": None if rate is None else round((1 - rate) * 100, 3),
        }

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_latency_p95(service: str, window: str = "5m") -> dict[str, Any]:
        """95th percentile request latency in seconds for a service."""
        return {"service": service, "window": window, "p95_seconds": backend.latency_p95(service, window)}

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_slo_status(service: str, slo_target: float = 0.999, window: str = "1h") -> dict[str, Any]:
        """Compare current availability with an SLO target and report error-budget burn rate."""
        rate = backend.error_rate(service, window)
        if rate is None:
            return {"service": service, "status": "no data"}
        budget = 1 - slo_target
        burn = rate / budget if budget else float("inf")
        return {
            "service": service,
            "slo_target": slo_target,
            "availability": round(1 - rate, 5),
            "burn_rate": round(burn, 1),
            "status": "breaching" if burn > 1 else "ok",
            "note": "burn_rate > 14.4 for 1h exhausts a 30-day budget in ~2 days (page-worthy)",
        }

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def query_promql(promql: str) -> list[dict[str, Any]]:
        """Run an instant PromQL query. Prefer the specific tools when they fit."""
        return backend.query(promql)

    return mcp


def main() -> None:
    build_server(metrics_backend()).run("stdio")


if __name__ == "__main__":
    main()
