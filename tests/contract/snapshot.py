"""Regenerate the published tool-contract snapshot.

Run deliberately -- ``python -m tests.contract.snapshot`` -- never from a test.
A test that regenerates its own expectation on mismatch passes forever and
detects nothing, which is precisely the failure the snapshot exists to catch.

Read MCP.md section 8 before running this. Additive changes are fine; removing
a field, changing a type, tightening an enum, or editing a description are all
breaking, and a description change is a prompt change that invalidates
benchmark numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.contract.test_tool_schemas import SNAPSHOT, published

if __name__ == "__main__":
    Path(SNAPSHOT).write_text(
        json.dumps(published(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {SNAPSHOT}")
