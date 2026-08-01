"""Entrypoint: `python -m mcp_servers.profile_table`.

Launched by an MCP host as a subprocess. Resources are built here so a bad
DATABASE_URL kills the process while the host is starting it, rather than
surfacing as a tool error on the first call that the agent will try, and fail,
to correct its way out of.
"""

from __future__ import annotations

from core.settings import Settings
from mcp_servers.common import configure_logging, main
from mcp_servers.profile_table.server import build
from mcp_servers.resources import Resources

if __name__ == "__main__":
    configure_logging()
    main(build(Resources(Settings.load())))
