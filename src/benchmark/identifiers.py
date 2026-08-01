"""The identifier boundary between a benchmark and PostgreSQL.

Benchmark databases are third-party data. Their table and column names end up
in composed DDL and in the search path of the read-only role, so they are
treated as untrusted input and pass through here first.

Two rules, and the reasoning behind each.

**Names are folded to lower case.** Gold SQL is written for SQLite, where
identifiers are case-insensitive: ``SELECT Name FROM Stadium`` and
``SELECT name FROM stadium`` are the same query. PostgreSQL folds *unquoted*
identifiers to lower case and matches them case-sensitively, so the only way an
unmodified gold query resolves against the converted schema is if everything it
can name is lower case. Preserving the source casing and quoting on both sides
would work only if every gold query quoted every identifier, and none of them
do.

**Ambiguity is refused, never sanitised.** Folding creates the possibility of a
collision -- a source database with both ``Song`` and ``song``, or with a name
too long for PostgreSQL's 63-byte limit. The tempting fix is to mangle one of
them into something unique. That silently merges or renames a table: the
conversion succeeds, the load succeeds, and every question about the affected
table is scored against the wrong data with nothing anywhere to indicate it.
Refusing costs one database out of two hundred and says which one and why.

Everything composed downstream still goes through :class:`psycopg.sql.Identifier`.
This module decides *whether a name may be used at all*; quoting decides how it
is written. Escaping is not authorization -- see ADR-017.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from core.exceptions import UnsafeIdentifierError

MAX_IDENTIFIER_BYTES = 63
"""PostgreSQL's ``NAMEDATALEN - 1``. Longer names are *truncated* by the server,
not rejected, which is precisely the silent-collision case this module exists to
prevent."""

_ALLOWED = re.compile(r"[a-z0-9_][a-z0-9_ $-]*")
"""Deliberately narrow.

Quoting makes almost anything legal as a PostgreSQL identifier, so this is not
about what the server accepts. It is about what a gold query can reference and
what a human can read in an error message. Spaces and hyphens are allowed
because real benchmark schemas contain them; quotes, backslashes, semicolons,
dots and control characters are not, because a name containing one is either
unusable from gold SQL or an attempt at something.
"""


def to_pg_identifier(raw: str, *, kind: str = "identifier") -> str:
    """Fold one source name to the identifier that will represent it.

    Args:
        raw: The name as the benchmark wrote it.
        kind: What is being named, for the error message -- ``"table"``,
            ``"column"``, ``"schema"``. An error that says only "unsafe
            identifier" leaves an operator grepping a 200-database corpus.

    Raises:
        UnsafeIdentifierError: The name is empty, too long once encoded, or
            contains a character outside :data:`_ALLOWED`.
    """
    stripped = raw.strip()
    if not stripped:
        raise UnsafeIdentifierError(f"{kind} name is empty or whitespace-only")

    folded = stripped.lower()

    if not _ALLOWED.fullmatch(folded):
        offending = sorted({ch for ch in folded if not _ALLOWED.fullmatch(f"a{ch}")})
        raise UnsafeIdentifierError(
            f"{kind} {raw!r} contains characters that cannot be used safely: "
            f"{offending!r}. It is refused rather than rewritten, because a "
            f"rewrite that collides with another name merges two objects."
        )

    encoded = len(folded.encode("utf-8"))
    if encoded > MAX_IDENTIFIER_BYTES:
        raise UnsafeIdentifierError(
            f"{kind} {raw!r} is {encoded} bytes; PostgreSQL truncates at "
            f"{MAX_IDENTIFIER_BYTES} and two names sharing a prefix would "
            f"truncate to the same identifier"
        )

    return folded


@dataclass(frozen=True, slots=True)
class IdentifierMap:
    """Source names to target identifiers, with collisions refused up front.

    Built for a whole set at once rather than name by name, because a collision
    is a property of the set: ``to_pg_identifier`` cannot see it, and by the
    time the second ``CREATE TABLE`` runs the first one has already succeeded.
    """

    kind: str
    _forward: dict[str, str]

    @classmethod
    def build(cls, raws: Iterable[str], *, kind: str) -> IdentifierMap:
        """Fold every name, raising on the first collision with both originals named.

        Raises:
            UnsafeIdentifierError: Two source names fold to one identifier, or
                any single name is unusable.
        """
        forward: dict[str, str] = {}
        seen: dict[str, str] = {}
        for raw in raws:
            safe = to_pg_identifier(raw, kind=kind)
            if safe in seen and seen[safe] != raw:
                raise UnsafeIdentifierError(
                    f"{kind}s {seen[safe]!r} and {raw!r} both become {safe!r}. "
                    f"Case-folding is required for gold SQL to resolve, so this "
                    f"database cannot be converted without changing what its "
                    f"queries mean."
                )
            seen[safe] = raw
            forward[raw] = safe
        return cls(kind=kind, _forward=forward)

    def safe(self, raw: str) -> str:
        """The identifier for a source name.

        Raises:
            UnsafeIdentifierError: The name was not part of the set this map was
                built from. A lookup miss means the caller assembled the set and
                the query from different sources, which is worth failing on.
        """
        try:
            return self._forward[raw]
        except KeyError as exc:
            raise UnsafeIdentifierError(
                f"{self.kind} {raw!r} was not in the set this map was built from"
            ) from exc

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._forward.items())

    def __len__(self) -> int:
        return len(self._forward)


def quote_sqlite_identifier(raw: str) -> str:
    """Quote a name for a SQLite statement that cannot take a bind parameter.

    Reading a source table means ``SELECT * FROM <name>``, and no driver binds
    an identifier. The name has already been accepted by
    :func:`to_pg_identifier`, which excludes the double quote outright, so the
    doubling below is a second barrier rather than the only one -- if this is
    ever the only thing between a source file and a composed statement, it
    should still hold.
    """
    return '"' + raw.replace('"', '""') + '"'


__all__ = [
    "MAX_IDENTIFIER_BYTES",
    "IdentifierMap",
    "quote_sqlite_identifier",
    "to_pg_identifier",
]
