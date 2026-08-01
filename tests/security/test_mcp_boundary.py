"""The MCP layer as an attack surface, and as a place to leak from.

Exposing four capabilities over a protocol adds two risks that the components
underneath do not have.

**A channel that can be corrupted.** Over stdio, stdout *is* the JSON-RPC
stream. Anything else written there is a protocol violation, and the resulting
failure names a JSON decode error rather than the `print` that caused it.

**A boundary that failures cross.** Component exceptions carry driver text,
role names and connection strings; a tool result goes to a language model and
from there to a provider. MCP.md section 6 forbids raw driver output crossing
this line, and this is where that is checked.

The third thing checked here is that the protocol did not become a way *around*
the existing controls: a query sent through `execute_sql` must be refused by
exactly the same layers as one sent to `SQLExecutor` directly.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

SRC = Path(__file__).resolve().parents[2] / "src"
MCP_SERVERS = SRC / "mcp_servers"


def code_only(source: str) -> str:
    """Source with docstrings and comments removed.

    A source-level assertion that counts occurrences in prose counts its own
    explanation of what it is looking for -- which is how a guard ends up
    reporting a violation that is a paragraph about the violation.
    """
    stripped = re.sub(r'"""(?:.|\n)*?"""', "", source)
    return "\n".join(line.split("#")[0] for line in stripped.splitlines())


def run_script(body: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet in a fresh interpreter, capturing the two streams apart.

    A subprocess rather than a monkeypatch, because the property under test is
    what lands on file descriptor 1 -- and rebinding ``sys.stdout`` inside this
    process would be testing the mock.
    """
    return subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        check=False,
    )


class TestStdoutBelongsToTheProtocol:
    def test_a_stray_print_does_not_reach_stdout(self) -> None:
        """The single most damaging accident in an stdio server.

        One `print` -- in this code, a dependency, or a debugging session
        somebody forgot to undo -- writes a line the host cannot parse, and the
        session dies naming a decode error rather than the cause.
        """
        result = run_script(
            "from mcp_servers.common import claim_stdout\nclaim_stdout()\nprint('CONTAMINATION')\n"
        )

        assert "CONTAMINATION" not in result.stdout

    def test_it_lands_on_stderr_instead(self) -> None:
        """Redirected, not swallowed. A discarded diagnostic is its own bug."""
        result = run_script(
            "from mcp_servers.common import claim_stdout\nclaim_stdout()\nprint('CONTAMINATION')\n"
        )

        assert "CONTAMINATION" in result.stderr

    def test_the_protocol_keeps_the_real_stdout(self) -> None:
        """The guard must not cost the transport its channel -- otherwise the
        server is protected from corruption by being unable to reply."""
        result = run_script(
            "import anyio\n"
            "from mcp_servers.common import claim_stdout\n"
            "stream = claim_stdout()\n"
            "print('noise')\n"
            "anyio.run(stream.write, 'PROTOCOL\\n')\n"
        )

        assert "PROTOCOL" in result.stdout
        assert "noise" not in result.stdout

    def test_logging_is_configured_onto_stderr(self) -> None:
        result = run_script(
            "import logging\n"
            "from mcp_servers.common import configure_logging\n"
            "configure_logging()\n"
            "logging.getLogger('t').warning('LOGLINE')\n"
        )

        assert "LOGLINE" in result.stderr
        assert "LOGLINE" not in result.stdout

    def test_logging_configuration_wins_over_an_earlier_handler(self) -> None:
        """``basicConfig`` is a no-op once any handler exists, so a library that
        configured logging first -- onto stdout -- would otherwise keep it."""
        result = run_script(
            "import logging, sys\n"
            "logging.basicConfig(stream=sys.stdout)\n"
            "from mcp_servers.common import configure_logging\n"
            "configure_logging()\n"
            "logging.getLogger('t').warning('LOGLINE')\n"
        )

        assert "LOGLINE" not in result.stdout

    def test_no_server_module_calls_print(self) -> None:
        """A source-level guard, because the runtime one above is a safety net
        rather than a licence. `print` in a server is always a mistake here."""
        offenders = [
            f"{path.name}:{number}"
            for path in MCP_SERVERS.rglob("*.py")
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if line.strip().startswith("print(")
        ]

        assert offenders == []


class TestFailuresDoNotNarrateInfrastructure:
    def test_an_unreachable_database_fails_at_startup_rather_than_hanging(self) -> None:
        """A server that neither responds nor exits is worse than one that dies.

        The default libpq connect timeout is *none*, so without
        `DB_CONNECT_TIMEOUT_MS` being passed through this blocks until the OS
        gives up on the TCP connection -- and an MCP host sees a subprocess
        that has started and will never answer. This test is what found that
        the setting existed and was wired to nothing.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.settings import Settings\n"
                "from mcp_servers.resources import Resources\n"
                "from mcp_servers.validate_sql.server import build\n"
                "try:\n"
                "    build(Resources(Settings.load()))\n"
                "except Exception as exc:\n"
                "    import sys; print('FAILED_AT_STARTUP', type(exc).__name__, file=sys.stderr)\n",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "PYTHONPATH": str(SRC),
                # 10.255.255.1 is non-routable, so this is a connect timeout
                # rather than a refused connection -- the case that hangs.
                "DATABASE_URL": "postgresql://u:hunter2@10.255.255.1:5999/nope",
                "DATABASE_RO_URL": "postgresql://v:hunter3@10.255.255.1:5999/nope",
                "DB_CONNECT_TIMEOUT_MS": "2000",
                "LLM_PROVIDER": "fake",
                "LLM_MODEL": "",
            },
            check=False,
        )

        assert "FAILED_AT_STARTUP" in result.stderr

    def test_that_failure_never_reaches_stdout(self) -> None:
        """Startup diagnostics are the operator's. stdout is the protocol's,
        and a traceback written there would carry the connection string --
        password included -- into the JSON-RPC stream."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_servers.validate_sql"],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "PYTHONPATH": str(SRC),
                "DATABASE_URL": "postgresql://u:hunter2@10.255.255.1:5999/nope",
                "DATABASE_RO_URL": "postgresql://v:hunter3@10.255.255.1:5999/nope",
                "DB_CONNECT_TIMEOUT_MS": "2000",
                "LLM_PROVIDER": "fake",
                "LLM_MODEL": "",
            },
            check=False,
        )

        assert "hunter2" not in result.stdout
        assert result.stdout.strip() == ""

    def test_the_generic_message_carries_no_detail(self) -> None:
        from mcp_servers.common import GENERIC_FAILURE

        assert "://" not in GENERIC_FAILURE
        assert "postgres" not in GENERIC_FAILURE.lower()

    def test_the_dispatcher_never_formats_an_unknown_exception(self) -> None:
        """``str(exc)`` on an arbitrary exception is how a connection string
        ends up in a tool result -- which is exactly what the SDK's own
        catch-all does, and the reason this project catches first.

        A source assertion because it is a property of the code's shape: the
        only ``str(exc)`` in the module is the one guarded by an
        ``isinstance(exc, TextToSQLError)`` check.
        """
        source = code_only((MCP_SERVERS / "common.py").read_text(encoding="utf-8"))

        assert source.count("str(exc)") == 1
        assert "str(exc) if isinstance(exc, TextToSQLError)" in source


class TestTheProtocolIsNotAWayAround:
    """Every bound lives in the component, not the server. These check the
    server did not quietly widen one on the way past."""

    def test_the_published_k_ceiling_is_the_retrievers(self) -> None:
        from mcp_servers.schema_search.server import INPUT_SCHEMA
        from schema.retrieval import MAX_K

        assert INPUT_SCHEMA["properties"]["k"]["maximum"] == MAX_K

    def test_the_published_row_ceiling_cannot_exceed_the_configured_one(self) -> None:
        from core.settings import ExecutionSettings
        from mcp_servers.execute_sql.server import input_schema

        settings = ExecutionSettings(max_rows_default=10, max_rows_ceiling=100)
        schema = input_schema(settings.max_rows_ceiling, settings.statement_timeout_ceiling_ms)

        assert schema["properties"]["max_rows"]["maximum"] == 100
        assert settings.clamp_rows(10_000) == 100

    def test_profile_sampling_stays_shut_when_the_flag_is_off(self) -> None:
        """The published `sample_rows` parameter must not become a way to open
        a gate that configuration closed."""
        from core.settings import ProfilingSettings

        settings = ProfilingSettings(profile_allow_value_sampling=False)

        assert settings.clamp_sample_rows(20) == 0

    def test_execute_sql_constructs_its_own_validator(self) -> None:
        """It must not accept a pre-validated flag or a caller-supplied
        validator: a separate host can connect to this server alone, and a tool
        that is only safe when invoked in the right order is not safe."""
        source = (MCP_SERVERS / "execute_sql" / "server.py").read_text(encoding="utf-8")

        assert "SQLValidator(" in source

    def test_execute_sql_runs_on_the_readonly_connection(self) -> None:
        """The boundary that holds when everything above it has failed. Using
        `resources.owner` here would leave every other control in place and
        remove the only one that cannot be reasoned around."""
        source = (MCP_SERVERS / "execute_sql" / "server.py").read_text(encoding="utf-8")

        assert "SingleConnectionSource(resources.readonly)" in source

    def test_profiling_runs_on_the_readonly_connection(self) -> None:
        source = (MCP_SERVERS / "profile_table" / "server.py").read_text(encoding="utf-8")

        assert "resources.readonly" in source

    def test_the_audit_log_runs_on_the_owner_connection(self) -> None:
        """Deliberately the other way round: the trail must be unreachable from
        the role running the query it records."""
        source = (MCP_SERVERS / "execute_sql" / "server.py").read_text(encoding="utf-8")

        assert "AuditLog(resources.owner" in source

    def test_search_results_never_include_the_serialized_text(self) -> None:
        """`serialized` carries sampled row values when sampling is on, and a
        tool result goes to a model. Same exclusion as the prompt path, and it
        needs its own guard because this is a second renderer."""
        source = (MCP_SERVERS / "schema_search" / "server.py").read_text(encoding="utf-8")
        rendered = source.split("def render(")[1].split("def build(")[0]

        assert "serialized" not in rendered.replace("element.serialized``", "")
