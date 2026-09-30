"""Builds Strands MCP tool clients for the configured AWS MCP servers.

Exposes a single context-manager, `mcp_tools_session()`, that opens every
configured MCP server (stdio + remote HTTP), collects their tools, and
guarantees clean shutdown even if the agent raises. Any individual server
that fails to *connect* at runtime (e.g. a transient network issue on first
`uvx` download) is skipped with a logged warning instead of taking down the
whole agent; config-time failures (e.g. a malformed server entry) are
already handled by `continue_on_error` in config.MCP_SERVERS.
"""
from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from typing import Iterator

from strands.tools.mcp import MCPClient

import config

logger = logging.getLogger(__name__)


@contextmanager
def mcp_tools_session() -> Iterator[list]:
    """Open all configured MCP servers and yield their combined tool list.

    Usage:
        with mcp_tools_session() as mcp_tools:
            agent = Agent(tools=[*mcp_tools, *custom_tools])
            agent(user_prompt)
    """
    clients = MCPClient.load_servers(
        config.MCP_SERVERS, continue_on_error=True, prefix_with_server_name=True
    )

    tools: list = []
    with ExitStack() as stack:
        for client in clients:
            try:
                stack.enter_context(client)
                tools.extend(client.list_tools_sync())
            except Exception:  # noqa: BLE001 - deliberately broad, see docstring
                logger.warning("An MCP server failed to start; continuing without its tools.", exc_info=True)
        yield tools
