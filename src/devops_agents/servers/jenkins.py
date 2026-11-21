"""CI/CD (Jenkins) MCP server: builds, change sets, console logs and gated build triggers."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..backends import CIBackend, ci_backend
from . import READ_ONLY, WRITE, expected_errors


def build_server(backend: CIBackend) -> MCPServer:
    mcp = MCPServer("jenkins", instructions="Inspect CI/CD pipelines, builds and what changed in them.")

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def list_jobs() -> list[dict[str, Any]]:
        """All jobs with their last build number and result."""
        return backend.list_jobs()

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_recent_builds(job: str, limit: int = 5) -> list[dict[str, Any]]:
        """Most recent builds of a job: number, result, start time, duration and parameters."""
        return backend.recent_builds(job, min(limit, 20))

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_build(job: str, number: int) -> dict[str, Any]:
        """One build's result, parameters and SCM change set (commits, authors, files)."""
        return backend.build_details(job, number)

    @mcp.tool(annotations=READ_ONLY)
    @expected_errors
    def get_build_log(job: str, number: int, tail: int = 80) -> str:
        """Last `tail` lines of a build's console log."""
        return backend.build_log(job, number, min(tail, 1000))

    @mcp.tool(annotations=WRITE)
    @expected_errors
    def trigger_build(job: str, parameters: dict[str, str] | None = None) -> str:
        """Queue a new build, optionally with parameters. Starts a pipeline."""
        return backend.trigger_build(job, parameters)

    return mcp


def main() -> None:
    build_server(ci_backend()).run("stdio")


if __name__ == "__main__":
    main()
