"""Builds Strands MCP tool clients for the configured AWS MCP servers.

Exposes `get_mcp_tools()`, which connects every configured MCP server
(stdio + remote HTTP) ONCE per container and caches the combined tool list
for the container's lifetime - NOT once per request. Starting these
subprocesses is expensive (the AWS MCP Server's own docs warn "first
connection can take a while", hence the 90s startup_timeout in
config.MCP_SERVERS) - reconnecting on every single chat message was adding
several seconds of pure fixed overhead to every request, including ones
that don't use any MCP tool at all. Any individual server that fails to
*connect* at runtime (e.g. a transient network issue on first `uvx`
download) is skipped with a logged warning instead of taking down the whole
agent; config-time failures (e.g. a malformed server entry) are already
handled by `continue_on_error` in config.MCP_SERVERS.
"""
from __future__ import annotations

import logging
import threading
from contextlib import ExitStack

from strands.tools.mcp import MCPClient

import config

logger = logging.getLogger(__name__)

# Never closed/exited on purpose - these MCP subprocess connections are
# meant to outlive every individual request and only go away when the
# container itself stops.
_exit_stack = ExitStack()
_tools_cache: list | None = None
_init_lock = threading.Lock()


def get_mcp_tools() -> list:
    """Return the shared MCP tool list, connecting to every configured
    server on the first call only. Thread-safe: concurrent first calls
    (e.g. two requests landing while the container is still cold) will
    only trigger one real connection attempt."""
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache

    with _init_lock:
        if _tools_cache is not None:  # another thread won the race while we waited for the lock
            return _tools_cache

        clients = MCPClient.load_servers(
            config.MCP_SERVERS, continue_on_error=True, prefix_with_server_name=True
        )
        tools: list = []
        for client in clients:
            try:
                _exit_stack.enter_context(client)
                tools.extend(client.list_tools_sync())
            except Exception:  # noqa: BLE001 - deliberately broad, see module docstring
                logger.warning("An MCP server failed to start; continuing without its tools.", exc_info=True)
        _tools_cache = tools
        return _tools_cache
