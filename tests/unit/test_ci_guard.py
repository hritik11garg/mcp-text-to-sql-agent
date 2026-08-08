"""The guard that stops CI reporting green over a gate it never ran.

`tests/conftest.py` starts PostgreSQL through testcontainers, and without a
Docker daemon that fixture cannot produce a database. Locally it skips, which
is correct. In CI a skip is the worst available outcome: the integration and
security layers disappear, everything remaining passes, and the run reports
success over the release gate — the negative suite proving an LLM cannot write
to the database.

These tests exist because the guard is itself a safety mechanism, and this
project keeps finding that an untested control is one nobody has seen fail.
"""

from __future__ import annotations

import pytest
from tests import conftest


class TestWithoutDocker:
    def test_it_skips_on_a_developer_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nobody should be blocked by a stopped daemon; the unit layer still runs."""
        monkeypatch.setattr(conftest, "_docker_available", lambda: False)
        monkeypatch.delenv("CI", raising=False)

        with pytest.raises(pytest.skip.Exception):
            conftest.require_docker()

    def test_it_refuses_to_skip_in_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The case the guard exists for. A skip here is a false green."""
        monkeypatch.setattr(conftest, "_docker_available", lambda: False)
        monkeypatch.setenv("CI", "true")

        with pytest.raises(pytest.UsageError, match="Refusing to skip"):
            conftest.require_docker()

    def test_the_failure_says_to_fix_the_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An error that reads as a flaky assertion invites deleting the check.

        Whoever hits this at 2am should be pointed at the runner, not at the
        guard — so the message names the consequence and the correct fix.
        """
        monkeypatch.setattr(conftest, "_docker_available", lambda: False)
        monkeypatch.setenv("CI", "true")

        with pytest.raises(pytest.UsageError) as caught:
            conftest.require_docker()

        message = str(caught.value)
        assert "release gate" in message
        assert "Fix the runner" in message

    @pytest.mark.parametrize("value", ["true", "1", "yes"])
    def test_any_truthy_ci_value_counts(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """GitHub sets `CI=true`; other runners set other strings. Presence is
        the signal, not the value — checking for the literal "true" would let a
        runner that sets `CI=1` skip the gate silently."""
        monkeypatch.setattr(conftest, "_docker_available", lambda: False)
        monkeypatch.setenv("CI", value)

        with pytest.raises(pytest.UsageError):
            conftest.require_docker()


class TestWithDocker:
    def test_it_gets_out_of_the_way(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard must be invisible on the ordinary path, in CI or not."""
        monkeypatch.setattr(conftest, "_docker_available", lambda: True)
        monkeypatch.setenv("CI", "true")

        assert conftest.require_docker() is None
