"""The probe contract: what each endpoint checks, and what it refuses to say."""

from __future__ import annotations

import pytest

from api.health import DOWN, UP, Probe, Readiness


def failing() -> None:
    raise RuntimeError("postgresql://sql_agent_login:hunter2@db.internal:5432/prod")


def passing() -> None:
    return None


class TestReadiness:
    def test_all_probes_up_is_ready(self) -> None:
        readiness = Readiness([Probe("database", passing), Probe("catalog", passing)])
        ready, detail = readiness.status()
        assert ready
        assert detail == {"database": UP, "catalog": UP}

    def test_one_probe_down_is_not_ready(self) -> None:
        readiness = Readiness([Probe("database", passing), Probe("catalog", failing)])
        ready, detail = readiness.status()
        assert not ready
        assert detail == {"database": UP, "catalog": DOWN}

    def test_unconfigured_is_not_ready(self) -> None:
        """`all([])` is True, so the naive implementation says yes during startup."""
        ready, detail = Readiness().status()
        assert not ready
        assert detail == {"startup": DOWN}

    def test_configure_replaces_the_probes_and_the_cache(self) -> None:
        readiness = Readiness([Probe("database", failing)])
        assert readiness.status()[0] is False
        readiness.configure([Probe("database", passing)])
        assert readiness.status()[0] is True

    def test_the_failure_reason_never_reaches_the_status(self) -> None:
        """A driver error carries the DSN. Only the verdict may be published."""
        _, detail = Readiness([Probe("database", failing)]).status()
        assert detail == {"database": DOWN}
        assert "hunter2" not in str(detail)
        assert "db.internal" not in str(detail)


class TestReadinessCaching:
    def test_a_repeated_call_does_not_rerun_the_probe(self) -> None:
        """An unauthenticated endpoint must not cost a round trip per request."""
        calls = 0

        def counting() -> None:
            nonlocal calls
            calls += 1

        readiness = Readiness([Probe("database", counting)], ttl=60.0)
        for _ in range(10):
            readiness.status()
        assert calls == 1

    def test_the_cache_expires(self) -> None:
        calls = 0

        def counting() -> None:
            nonlocal calls
            calls += 1

        readiness = Readiness([Probe("database", counting)], ttl=-1.0)
        readiness.status()
        readiness.status()
        assert calls == 2

    def test_the_returned_mapping_is_a_copy(self) -> None:
        """A caller mutating the response must not poison the next one."""
        readiness = Readiness([Probe("database", passing)], ttl=60.0)
        _, first = readiness.status()
        first["database"] = "tampered"
        _, second = readiness.status()
        assert second == {"database": UP}


class TestProbesAreIsolated:
    def test_one_failing_probe_does_not_stop_the_others(self) -> None:
        readiness = Readiness(
            [Probe("a", failing), Probe("b", passing), Probe("c", failing)],
        )
        _, detail = readiness.status()
        assert detail == {"a": DOWN, "b": UP, "c": DOWN}

    @pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt, MemoryError])
    def test_any_exception_type_is_contained(self, exc: type[BaseException]) -> None:
        """Including the ones a bare `except Exception` would miss by inheritance."""

        def raiser() -> None:
            raise exc()

        readiness = Readiness([Probe("database", raiser)])
        if issubclass(exc, Exception):
            assert readiness.status() == (False, {"database": DOWN})
        else:
            # BaseException subclasses are deliberately *not* swallowed: a
            # KeyboardInterrupt caught by a health check is a process that
            # cannot be stopped.
            with pytest.raises(exc):
                readiness.status()
