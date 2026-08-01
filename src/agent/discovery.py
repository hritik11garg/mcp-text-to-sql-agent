"""The client half of the contract: connect, ask what exists, dispatch on that.

This is what makes "MCP-native" a claim rather than a slogan. The agent holds
no hardcoded list of tools. It connects to whatever servers it is configured
with, calls ``tools/list``, and builds its capability set from the answers.

Three properties follow, and they are the reason for the design:

**Adding a capability needs no agent change.** A fifth server appears in the
registry the moment it is configured.

**A missing server degrades rather than crashes.** :meth:`ToolRegistry.connect`
records the failure and continues, so a dead `profile_table` costs
disambiguation and nothing else -- the degradation table in
docs/architecture/MCP.md section 7 is implemented here rather than described.

**The registry does not filter to an allowlist.** A tool the agent has no
policy for is still callable. Filtering would quietly reintroduce the hardcoded
list that discovery exists to remove.

Nothing drives this yet -- the agent loop is Stage 4. It exists now because it
is the other half of the contract, and a contract tested only from the side
that implements it is tested against itself.
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

DEFAULT_SERVERS: tuple[str, ...] = (
    "mcp_servers.schema_search",
    "mcp_servers.validate_sql",
    "mcp_servers.execute_sql",
    "mcp_servers.profile_table",
)


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    """One tool as its server described it.

    ``server`` travels with the tool because two servers may legitimately
    expose the same name, and because a trace has to record which contract
    actually ran -- see docs/architecture/MCP.md section 8.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    server: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A tool call's outcome, with the protocol flag and the payload separated.

    ``is_error`` is the protocol's answer to "did this call fail?"; ``payload``
    is the structured content the agent reads to correct itself. Keeping them
    apart matters because a *successful* call can carry ``valid: false`` -- the
    SQL was wrong, the tool worked perfectly -- and collapsing the two would
    make those indistinguishable.
    """

    is_error: bool
    payload: dict[str, Any]
    text: str = ""

    @property
    def error_type(self) -> str | None:
        value = self.payload.get("error_type")
        return str(value) if value is not None else None


class ToolRegistry:
    """Connects to MCP servers and holds what they said they can do.

    **Use it as an async context manager, and open and close it in the same
    task.** The stdio transport is built on anyio task groups, whose cancel
    scopes must be exited by the task that entered them -- splitting the two
    across tasks raises ``Attempted to exit cancel scope in a different task``
    at teardown, after every call has already succeeded, which is a confusing
    place to learn about it.

    That is a real constraint of the transport rather than a limitation worth
    engineering around here: a registry is meant to live for the length of an
    agent run, which is one task. It is stated because the failure surfaces
    late and names nothing about the cause.

    Args:
        modules: Module paths launched as ``python -m <module>``. Defaults to
            this project's four.
        python: Interpreter to launch them with. Defaults to the current one,
            so a virtualenv is inherited rather than assumed to be on PATH.
        env: Environment for the subprocesses. ``None`` inherits.
    """

    def __init__(
        self,
        modules: tuple[str, ...] = DEFAULT_SERVERS,
        *,
        python: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        import sys

        self._modules = modules
        self._python = python or sys.executable
        self._env = env
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, DiscoveredTool] = {}
        self._unavailable: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def connect(self) -> None:
        """Start every configured server and discover its tools.

        A server that fails to start is recorded in :attr:`unavailable` and
        skipped. Raising instead would make one broken capability fatal to all
        four, which inverts the degradation contract: the agent is supposed to
        lose a *capability*, not a session.
        """
        for module in self._modules:
            try:
                await self._connect_one(module)
            except Exception as exc:
                # Deliberately broad: a subprocess can fail to launch in more
                # ways than are worth enumerating, and every one of them means
                # the same thing here -- this capability is not available.
                self._unavailable[module] = type(exc).__name__
                logger.warning("server %s unavailable: %s", module, type(exc).__name__)

    async def _connect_one(self, module: str) -> None:
        params = StdioServerParameters(command=self._python, args=["-m", module], env=self._env)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        self._sessions[module] = session
        listed = await session.list_tools()
        for tool in listed.tools:
            self._tools[tool.name] = DiscoveredTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema),
                server=module,
            )
        logger.info("discovered %d tool(s) on %s", len(listed.tools), module)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Invoke a discovered tool.

        Raises:
            KeyError: the tool was never discovered. Distinct from a tool
                *error* on purpose -- an agent asking for something no server
                advertises is a bug in the agent, not a query to correct.
        """
        tool = self._tools[name]
        result = await self._sessions[tool.server].call_tool(name, arguments)
        return _to_result(result)

    @property
    def tools(self) -> dict[str, DiscoveredTool]:
        return dict(self._tools)

    @property
    def unavailable(self) -> dict[str, str]:
        """Servers that failed to start, and the exception type each raised."""
        return dict(self._unavailable)

    def describe(self) -> str:
        """The tool set as the model will be shown it."""
        return "\n\n".join(
            f"{tool.name}: {tool.description}"
            for tool in sorted(self._tools.values(), key=_by_name)
        )

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()


def _by_name(tool: DiscoveredTool) -> str:
    return tool.name


def _to_result(result: types.CallToolResult) -> ToolResult:
    """Read a payload out of a result, preferring structured content.

    Falls back to parsing the JSON text block, because ``structuredContent`` is
    an optional part of the protocol and a host or server that predates it
    still returns the same payload as text. See MCP.md section 5.
    """
    text = "".join(block.text for block in result.content if isinstance(block, types.TextContent))

    payload: dict[str, Any] = {}
    if isinstance(result.structuredContent, dict):
        payload = dict(result.structuredContent)
    elif text:
        try:
            decoded = json.loads(text)
            payload = decoded if isinstance(decoded, dict) else {"value": decoded}
        except json.JSONDecodeError:
            payload = {"message": text}

    return ToolResult(is_error=bool(result.isError), payload=payload, text=text)


__all__ = ["DEFAULT_SERVERS", "DiscoveredTool", "ToolRegistry", "ToolResult"]
