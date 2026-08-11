"""The fingerprint guards the code that answered, not the directory it stood in.

``RunManifest.config_fingerprint`` used to hash the **commit**, and that was
wrong in both directions at once.

It refused too much: a documentation commit changes the hash, so every resume of
the three-day full-split run had to be made from a detached ``git worktree`` at
the recorded commit -- an operational procedure invented entirely to work around
a guard firing on prose.

And it permitted too much: the commit is read from the repository the *process
stands in*, while an editable install can import ``src/`` from a different one.
The guard could therefore pass on exactly the run it exists to refuse.

So these tests are about a single property -- **the digest tracks the bytes the
interpreter loaded** -- plus the one that keeps the module list honest, because a
hand-maintained list of "code that matters" is a list that silently stops being
one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evals.artifacts import (
    ANSWERING_PATH_MODULES,
    RunManifest,
    answering_path_digest,
)

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

# Packages whose every module shapes an answer. The exclusions are named rather
# than filtered by a rule, because "which code can change a number" is a
# judgement and a rule that looked principled would hide it.
ANSWERING_PACKAGES: dict[str, frozenset[str]] = {
    "answering": frozenset(),
    "generation": frozenset({"check"}),  # a provider round-trip CLI, not the path
    "validation": frozenset(),
    "execution": frozenset(),
    "adapters/embedding": frozenset(),
    "adapters/llm": frozenset(),
    "schema": frozenset(
        # Both build the catalog *before* a run. What they produced is already
        # pinned by `dataset` and `retriever_model_version`.
        {"introspection", "indexer"}
    ),
}


def _manifest(**overrides: object) -> RunManifest:
    base: dict[str, object] = {
        "run_id": "r1",
        "dataset": "spider",
        "split": "dev",
        "model": "m",
        "retriever_model_version": "v1",
        "prompt_version": "sql_gen/v1",
        "commit": "abc123",
        "code_digest": "deadbeefdeadbeef",
    }
    return RunManifest(**{**base, **overrides})  # type: ignore[arg-type]


class TestTheDigestTracksLoadedCode:
    def test_it_is_stable_across_calls(self) -> None:
        """A guard that moved on its own would refuse every resume."""
        assert answering_path_digest() == answering_path_digest()

    def test_it_is_sixteen_hex_characters(self) -> None:
        digest = answering_path_digest()
        assert len(digest) == 16
        assert set(digest) <= set("0123456789abcdef")

    def test_every_named_module_contributes(self) -> None:
        """Dropping any one module changes the digest.

        Without this, a module could be listed and silently not hashed -- the
        list would look like coverage while providing none.
        """
        full = answering_path_digest()
        for name in ANSWERING_PATH_MODULES:
            reduced = tuple(m for m in ANSWERING_PATH_MODULES if m != name)
            assert answering_path_digest(reduced) != full, f"{name} does not affect the digest"

    def test_order_does_not_matter(self) -> None:
        """The tuple is sorted before hashing, so a reordering is not a change."""
        assert answering_path_digest(tuple(reversed(ANSWERING_PATH_MODULES))) == (
            answering_path_digest()
        )

    def test_changed_source_changes_the_digest(self, tmp_path: Path) -> None:
        """The property the whole design rests on, proved against a real file.

        A module is written, imported, hashed, rewritten and hashed again. If
        the digest were derived from anything but the file's bytes -- a version
        string, a path, a commit -- this would not move.
        """
        import sys

        module_dir = tmp_path / "pkg_under_test"
        module_dir.mkdir()
        (module_dir / "__init__.py").write_text("", encoding="utf-8")
        target = module_dir / "answerer.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")

        sys.path.insert(0, str(tmp_path))
        try:
            before = answering_path_digest(("pkg_under_test.answerer",))
            target.write_text("VALUE = 2\n", encoding="utf-8")
            after = answering_path_digest(("pkg_under_test.answerer",))
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop("pkg_under_test.answerer", None)
            sys.modules.pop("pkg_under_test", None)

        assert before != after

    def test_a_module_that_cannot_be_imported_raises(self) -> None:
        """Loudly, rather than degrading to a sentinel.

        ``current_commit`` fails soft because a run from a tarball genuinely has
        no repository. There is no equivalent excuse here: every module named is
        one this process imports to do its work, so an unreadable one means the
        digest is weaker than the guard it replaced -- silently.
        """
        with pytest.raises(ModuleNotFoundError):
            answering_path_digest(("evals.no_such_module",))


class TestTheFingerprintUsesItInsteadOfTheCommit:
    def test_the_commit_no_longer_moves_the_fingerprint(self) -> None:
        """The defect this change exists to fix.

        Two runs of identical code at different commits -- a documentation
        commit between them -- must resume into each other.
        """
        assert _manifest(commit="aaa").config_fingerprint == (
            _manifest(commit="bbb").config_fingerprint
        )

    def test_changed_code_refuses_the_resume(self) -> None:
        assert _manifest(code_digest="1111111111111111").config_fingerprint != (
            _manifest(code_digest="2222222222222222").config_fingerprint
        )

    def test_the_commit_is_still_recorded(self) -> None:
        """Not hashed is not the same as not kept.

        It remains the human-readable provenance a reader starts from, and the
        ``-dirty`` suffix still says whether it is a lower bound.
        """
        assert _manifest(commit="abc123-dirty").to_dict()["commit"] == "abc123-dirty"

    def test_the_digest_is_recorded_too(self) -> None:
        assert _manifest().to_dict()["code_digest"] == "deadbeefdeadbeef"

    @pytest.mark.parametrize(
        "field",
        ["dataset", "split", "model", "retriever_model_version", "prompt_version", "baseline"],
    )
    def test_the_other_guarded_fields_still_guard(self, field: str) -> None:
        assert _manifest(**{field: "one"}).config_fingerprint != (
            _manifest(**{field: "two"}).config_fingerprint
        )

    @pytest.mark.parametrize("field", ["run_id", "started_at", "notes"])
    def test_the_unguarded_fields_still_do_not(self, field: str) -> None:
        """These differ between an attempt and its resume by construction."""
        assert _manifest(**{field: "one"}).config_fingerprint == (
            _manifest(**{field: "two"}).config_fingerprint
        )


class TestTheModuleListDoesNotGoStale:
    """The failure mode of a hand-maintained list, caught mechanically.

    Someone adds a stage to the validator or a second LLM adapter, and the
    digest keeps passing runs that the new code changed. The list cannot be
    derived from ``sys.modules`` -- that is not deterministic across
    configurations -- so it is derived from the *filesystem* here instead, which
    is.
    """

    def test_every_module_in_an_answering_package_is_listed(self) -> None:
        listed = set(ANSWERING_PATH_MODULES)
        missing: list[str] = []
        for package, excluded in ANSWERING_PACKAGES.items():
            for path in sorted((SRC / package).glob("*.py")):
                if path.stem == "__init__" or path.stem in excluded:
                    continue
                name = f"{package.replace('/', '.')}.{path.stem}"
                if name not in listed:
                    missing.append(name)

        assert not missing, (
            f"new answering-path modules are not in ANSWERING_PATH_MODULES: {missing}. "
            f"Add them, or add the module to ANSWERING_PACKAGES' exclusions with a "
            f"comment saying why it cannot change an answer"
        )

    def test_every_listed_module_exists(self) -> None:
        """The reverse drift: a module renamed and the list left behind."""
        for name in ANSWERING_PATH_MODULES:
            path = SRC / (name.replace(".", "/") + ".py")
            assert path.is_file(), f"{name} is listed but no longer exists at {path}"

    def test_the_excluded_packages_are_real(self) -> None:
        """An exclusion naming a package that moved would silently cover nothing."""
        for package in ANSWERING_PACKAGES:
            assert (SRC / package).is_dir(), f"{package} is not a package under src/"

    def test_named_exclusions_still_exist(self) -> None:
        """Same argument one level down.

        An exclusion for a module that has since been deleted is a comment
        asserting something about code nobody can check.
        """
        for package, excluded in ANSWERING_PACKAGES.items():
            for stem in excluded:
                assert (SRC / package / f"{stem}.py").is_file(), (
                    f"{package}/{stem}.py is excluded from the digest but does not exist"
                )

    def test_the_list_matches_what_is_hashed(self) -> None:
        """Belt and braces: the digest really is over these files' bytes."""
        expected = hashlib.sha256()
        for name in sorted(ANSWERING_PATH_MODULES):
            expected.update(name.encode())
            expected.update((SRC / (name.replace(".", "/") + ".py")).read_bytes())
        assert answering_path_digest() == expected.hexdigest()[:16]
