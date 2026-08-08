"""Resolve every relative link and ``#anchor`` across the documentation tree.

**A broken link is the cheapest possible documentation defect and the one
readers hit first.** This project has 25 documents that cross-reference each
other heavily -- ADR numbers, benchmark sections, security findings -- and a
heading rename silently invalidates every anchor pointing at it. Nothing else
in the suite would notice.

Run it directly, or as the ``docs`` job in CI::

    python scripts/check_docs_links.py

Exits non-zero listing every unresolved target. Deliberately checks **only**
relative links: external URLs are somebody else's uptime, and a CI job that
fails when an unrelated website is down is a job people learn to ignore.

The private ``learn/`` tree is excluded because it is local-only and never
published; ``node_modules`` and ``.venv`` because their markdown is vendored.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
EXCLUDED_PARTS = frozenset({"learn", "node_modules", ".venv", "data", "results", "dist"})


def slug(text: str) -> str:
    """GitHub's anchor rule, closely enough to be useful.

    Inline code, bold and links are unwrapped to their text; punctuation and
    symbols are dropped; spaces become dashes. The interesting cases in this
    repo are the separators the headings actually use -- ``·`` and ``—`` --
    which are symbols and therefore vanish, leaving the dashes around them.
    """
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*?([^*]*)\*\*?", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    kept = [
        ch
        for ch in text.lower()
        if ch.isalnum() or ch in "-_ " or not unicodedata.category(ch).startswith(("P", "S", "Z"))
    ]
    return "".join(kept).strip().replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    """Every ``#fragment`` a heading in this file would answer to."""
    found: set[str] = set()
    for _level, title in HEADING.findall(path.read_text(encoding="utf-8")):
        base = slug(title)
        found.add(base)
        # Consecutive separators collapse differently across renderers; accept
        # both rather than fail a link that works on GitHub.
        found.add(base.replace("--", "-"))
    return found


def documents() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md") if not EXCLUDED_PARTS & set(p.relative_to(ROOT).parts)
    )


def main() -> int:
    files = documents()
    anchors: dict[Path, set[str]] = {p: anchors_of(p) for p in files}
    problems: list[str] = []

    for path in files:
        for _label, target in LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, anchor = target.partition("#")

            if file_part == "":
                destination = path
            else:
                destination = (path.parent / file_part).resolve()
                if not destination.exists():
                    problems.append(f"{path.relative_to(ROOT)} -> missing file: {target}")
                    continue

            if anchor and destination.suffix == ".md":
                known = anchors.get(destination)
                if known is None:
                    known = anchors_of(destination)
                    anchors[destination] = known
                if anchor.lower() not in known:
                    problems.append(f"{path.relative_to(ROOT)} -> missing anchor: {target}")

    print(f"checked {len(files)} markdown file(s)")
    for problem in problems:
        print(f"  {problem}")
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
