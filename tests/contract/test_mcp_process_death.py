"""The four MCP servers, killed by a dependency they cannot reach.

Every one of the four `__main__` modules opens with the same claim:

> Resources are built here so a bad `DATABASE_URL` **kills the process** while
> the host is starting it, rather than surfacing as a tool error on the first
> call that the agent will try, and fail, to correct its way out of.

That is a reliability behaviour stated in prose in four files and demonstrated
in none (`docs/project/ENGINEERING_MATRIX.md` section 30). This asserts it as a
real process: launched with `subprocess`, pointed at a closed port, and watched
until it exits.

**Why the claim matters more than it sounds.** An MCP server that started
successfully and failed per call would look healthy to the host. The host would
advertise its tools, the agent would call one, get an error, and *self-correct*
-- rewriting a perfectly good query in response to an infrastructure failure,
burning a generation per attempt, and eventually reporting that it could not
answer the question. Failing at startup produces one clear error in the host's
log instead, and no tool.

**Three assertions per server, and the second is the one to read.**

1. It exits, within a timeout. A server that *hangs* waiting for stdio it will
   never usefully serve is the worst outcome: the host waits, the agent waits,
   and nothing says why.
2. **stdout is empty.** stdout is the MCP protocol channel and nothing else --
   the reason `claim_stdout` exists. A crash that printed one line there would
   corrupt the transport's framing for a host that had already started reading.
3. The exit status is non-zero, so a supervisor can tell this apart from a
   clean shutdown.

**No Docker needed**, which is why this sits apart from `test_mcp_stdio.py`.
The failure being injected is a refused connection, and port 1 is closed
everywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

SERVERS: Final = (
    "mcp_servers.schema_search",
    "mcp_servers.validate_sql",
    "mcp_servers.execute_sql",
    "mcp_servers.profile_table",
)

STARTUP_TIMEOUT_SECONDS: Final = 30.0
"""Generous on purpose.

The measured time to fail is a little over the connect timeout below -- about
three seconds. This is not a latency assertion; it is the difference between
"exited" and "hung", and a tight bound here would turn a slow CI runner into a
red build that says nothing about the behaviour under test.
"""


def unreachable_env() -> dict[str, str]:
    """Everything the servers read, with both databases pointed at a closed port.

    Set explicitly rather than inherited. `pydantic-settings` reads a developer's
    real `.env` from the working directory, and a variable left unset here would
    quietly pick up their configuration -- which for this suite means a server
    that *connects successfully* and never exits, failing the test for a reason
    that has nothing to do with the code.

    The two URLs differ because they must: settings refuse a read-only URL equal
    to the owner's, and that check fires *before* any connection is attempted.
    Identical URLs would exit non-zero for the wrong reason and the test would
    pass without ever exercising a connection failure.
    """
    return {
        **os.environ,
        "DATABASE_URL": "postgresql://owner:nothing@127.0.0.1:1/none",
        "DATABASE_RO_URL": "postgresql://reader:nothing@127.0.0.1:1/none",
        "DB_CONNECT_TIMEOUT_MS": "1000",
        "DATASET": "failure_injection",
        "EMBEDDER_PROVIDER": "hashing",  # no model download
        "LLM_PROVIDER": "fake",
        "LLM_MODEL": "",
        "SCHEMA_SAMPLE_VALUES": "false",
        "PROFILE_ALLOW_VALUE_SAMPLING": "false",
        "PYTHONPATH": "src",
    }


def launch(module: str) -> subprocess.CompletedProcess[str]:
    """Run one server to completion against an unreachable database.

    `stdin=DEVNULL` rather than a pipe: a server that reached its serve loop
    would then see EOF and exit, which would look like the behaviour under test.
    It does not reach the loop -- that is the point -- and closing stdin means a
    regression that *did* reach it still terminates instead of hanging the suite.
    """
    return subprocess.run(
        [sys.executable, "-m", module],
        cwd=REPO_ROOT,
        env=unreachable_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture(scope="module", params=SERVERS)
def dead_server(request: pytest.FixtureRequest) -> subprocess.CompletedProcess[str]:
    """One launch per server, shared by the assertions about it.

    Module-scoped because each launch costs a process start and a connect
    timeout. Four launches, three assertions each; twelve launches would be
    three times the wall clock for no additional coverage -- the process is
    dead and its output does not change once it is.
    """
    return launch(str(request.param))


class TestABadDatabaseUrlKillsTheServer:
    def test_the_process_exits(self, dead_server: subprocess.CompletedProcess[str]) -> None:
        """Reaching this at all is the assertion.

        `subprocess.run` raises `TimeoutExpired` if the process is still alive,
        so a server that hung fails here rather than anywhere below -- and it
        fails with a timeout, which names the defect exactly.
        """
        assert dead_server.returncode is not None

    def test_the_exit_status_is_non_zero(
        self, dead_server: subprocess.CompletedProcess[str]
    ) -> None:
        """So a supervisor can tell a failed start from a clean shutdown.

        A server that caught the error, logged it and exited 0 would be
        restarted forever by a supervisor that believed each exit was
        intentional, or -- worse -- not restarted at all.
        """
        assert dead_server.returncode != 0

    def test_stdout_stays_empty(self, dead_server: subprocess.CompletedProcess[str]) -> None:
        """stdout is the protocol channel and nothing else.

        A single stray line -- a print left in, a library writing a banner, a
        traceback misrouted -- lands in the middle of a JSON-RPC stream that a
        host may already be reading. `claim_stdout` exists to prevent exactly
        that, and this is the failure path where it is least likely to have
        been thought about.
        """
        assert dead_server.stdout == ""

    def test_the_reason_reaches_stderr(self, dead_server: subprocess.CompletedProcess[str]) -> None:
        """An operator has to be able to tell *why* the host has no tools.

        Asserted on the variable name rather than on a driver message, because
        the driver message is the thing that must not appear -- see the next
        test.
        """
        assert "DATABASE" in dead_server.stderr

    def test_the_failure_does_not_print_the_password(
        self, dead_server: subprocess.CompletedProcess[str]
    ) -> None:
        """The one this project has already been burned by.

        A psycopg connection failure quotes the whole DSN back, password
        included -- SECURITY.md 14.2.10, and it recurred during the credential
        rotation in a script that bypassed `libpq_dsn`. The startup path is
        where that error is most likely to be raised and least likely to be
        looked at, since it only appears when something is already wrong.
        """
        assert "nothing@" not in dead_server.stderr
        assert "postgresql://" not in dead_server.stderr
