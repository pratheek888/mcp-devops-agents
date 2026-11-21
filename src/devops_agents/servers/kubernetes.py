"""Kubernetes MCP server: read-only inspection plus gated remediation tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..backends import KubernetesBackend, kubernetes_backend
from . import DESTRUCTIVE, READ_ONLY, WRITE, expected_errors


def build_server(backend: KubernetesBackend) -> MCPServer:
    mcp = MCPServer(
        "kubernetes",
        instructions="Inspect workloads in a Kubernetes cluster. Write tools change the cluster.",
    )

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def list_namespaces() -> list[str]:
        """Namespaces in the cluster."""
        return backend.list_namespaces()

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def list_pods(namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        """List pods with status, readiness, restart count and image. selector example: app=payments-api"""
        return backend.list_pods(namespace, selector)

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def describe_pod(namespace: str, name: str) -> dict[str, Any]:
        """Pod details: status, last termination state, image, node and literal env values."""
        return backend.describe_pod(namespace, name)

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_pod_logs(namespace: str, name: str, tail: int = 50, previous: bool = False) -> str:
        """Last `tail` log lines of a pod. Set previous=true for the crashed container's logs."""
        return backend.pod_logs(namespace, name, min(tail, 500), previous)

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_events(namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]:
        """Recent events in a namespace, optionally filtered by object name (e.g. payments-api)."""
        return backend.events(namespace, involved_object)

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def list_deployments(namespace: str) -> list[dict[str, Any]]:
        """Deployments with desired/ready replicas, image and current revision."""
        return backend.list_deployments(namespace)

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_rollout_history(namespace: str, deployment: str) -> list[dict[str, Any]]:
        """Revision history of a deployment: revision, image, change cause and time."""
        return backend.rollout_history(namespace, deployment)

    @mcp.tool(annotations=WRITE)
    @expected_errors
    def restart_deployment(namespace: str, deployment: str) -> str:
        """Rolling restart of a deployment (same image). Changes the cluster."""
        return backend.restart_deployment(namespace, deployment)

    @mcp.tool(annotations=DESTRUCTIVE)
    @expected_errors
    def rollback_deployment(namespace: str, deployment: str, to_revision: int | None = None) -> str:
        """Roll a deployment back to the previous (or given) revision. Changes the cluster."""
        return backend.rollback_deployment(namespace, deployment, to_revision)

    @mcp.tool(annotations=DESTRUCTIVE)
    @expected_errors
    def scale_deployment(namespace: str, deployment: str, replicas: int) -> str:
        """Scale a deployment to a replica count (0-50). Changes the cluster."""
        if not 0 <= replicas <= 50:
            raise ValueError("replicas must be between 0 and 50")
        return backend.scale_deployment(namespace, deployment, replicas)

    return mcp


def main() -> None:
    build_server(kubernetes_backend()).run("stdio")


if __name__ == "__main__":
    main()
