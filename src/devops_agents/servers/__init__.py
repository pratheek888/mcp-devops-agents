"""MCP servers exposing DevOps tools. Run any of them standalone, e.g.:

    python -m devops_agents.servers.kubernetes

They speak MCP over stdio, so they also work with any MCP client (Claude Desktop, Claude Code, ...).
"""

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

F = TypeVar("F", bound=Callable[..., Any])

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def expected_errors(fn: F) -> F:
    """Surface backend errors (not found, bad input, API failures) to the model as readable tool errors.

    Without this, MCP hides the message of unexpected exceptions and the agent only sees
    "Error executing tool", which it can't act on.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (LookupError, ValueError, RuntimeError, PermissionError, TimeoutError) as e:
            raise ToolError(str(e)) from e
        except Exception as e:  # e.g. kubernetes ApiException / httpx errors in live mode
            if type(e).__name__ in {"ApiException", "HTTPStatusError", "ConnectError", "ReadTimeout"}:
                raise ToolError(f"{type(e).__name__}: {e}") from e
            raise

    return wrapper  # type: ignore[return-value]
