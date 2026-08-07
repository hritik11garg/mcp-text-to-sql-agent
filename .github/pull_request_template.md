## What
One-paragraph summary of the change.

## Why
The problem this solves. Link the ROADMAP stage or TASKS item.

## How
Notable implementation decisions. If a decision is non-obvious or was
contested, add it to DECISIONS.md and link the entry here.

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests pass against a real Postgres
- [ ] Eval harness re-run (if retrieval, generation, or validation changed)

## Benchmarks
Before / after numbers, or "N/A — no measurable surface touched".

A number here needs the run behind it: commit, split, sample size, and `k`.
A sample that covers part of a split says which part.

## Security
- [ ] No new SQL reaches the database outside the read-only role
- [ ] No secrets in code, logs, traces, or fixtures
- [ ] Untrusted input (NL questions, schema names) is not string-interpolated
      into SQL
- [ ] Any new setting is in `.env.example` and CONFIG.md (asserted by
      `tests/unit/test_settings.py` — a control nobody can find is not applied)

## Docs
- [ ] Relevant docs/ pages updated
- [ ] CHANGELOG.md updated under [Unreleased]
- [ ] **Status blocks re-read, not just the body.** Drift lands in the
      paragraph *next to* the edit far more often than in the edit — a
      `> Status:` header saying a thing is unbuilt while §2 of the same file
      says it ships, a prose summary above a table that was updated without it
- [ ] **No *planned* entry describes something this PR built.** If it shipped
      under a different name or unit, say so where the old name was, rather
      than deleting the row — someone is searching for the old one
- [ ] **A measurement this PR overturns is corrected where it was published**,
      including in DEMO_SCRIPT.md if it was something to say out loud

These three are not generic diligence: [RISKS.md](../docs/project/RISKS.md)
R-17 has materialised six times and these are the shapes it took.
