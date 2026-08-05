# Contributing

Solo project, but the conventions are written down so they stay consistent across a 6-week build — and so the repo reads like something a team could pick up.

---

## Development setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
```

Python 3.12 is pinned in `.python-version` and enforced by `requires-python` in `pyproject.toml`. See [DECISIONS.md](docs/architecture/DECISIONS.md) for why.

## Coding style

Full standards live in [CODE_STYLE.md](docs/development/CODE_STYLE.md). The short version:

- `ruff` for lint and format — it replaces black, isort, and flake8. Config in `pyproject.toml`.
- `mypy` in strict mode on `src/`. New code is fully annotated; no bare `Any` at module boundaries.
- Async everywhere on the I/O path (MCP, HTTP, database). No blocking calls inside `async def`.
- Dependencies are injected, not imported at call sites, so tests can substitute fakes.

Run everything before committing:

```powershell
ruff format .
ruff check . --fix
mypy src
pytest
```

## Git workflow

Trunk-based with short-lived branches. `main` is always demoable — that is the whole point of the stage structure in [ROADMAP.md](docs/project/ROADMAP.md).

### Branch naming

```
<type>/<short-kebab-description>
```

| Type | Use for |
|---|---|
| `feat` | New capability |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Behaviour-preserving restructure |
| `test` | Tests only |
| `perf` | Performance work |
| `chore` | Tooling, deps, CI |

Examples: `feat/schema-search-mcp-server`, `fix/statement-timeout-not-applied`, `docs/decisions-why-sqlglot`.

## Commit style

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body — what changed and why, not how>

<footer — refs, breaking changes>
```

Scopes track the architecture: `agent`, `mcp`, `retrieval`, `validation`, `execution`, `eval`, `training`, `api`, `db`, `obs`.

```
feat(mcp): add profile_table server with column stats and sample rows

Disambiguation needs distributions, not just names — "revenue" matches
three columns in the sales schema and only cardinality separates them.
Sample rows are capped at 5 and text columns are truncated to 100 chars
so a wide table cannot blow the context budget.

Refs: docs/architecture/MCP.md
```

Rules:
- Subject in imperative mood, lowercase, no trailing period, ≤72 chars.
- Explain *why* in the body. The diff already shows what.
- One logical change per commit.
- **Never commit measured numbers without the eval run that produced them.** If a commit changes a benchmark, it updates [BENCHMARKS.md](docs/ml/BENCHMARKS.md) in the same commit.

## Pull request template

Even solo, open a PR per branch — it forces a diff review before merge.

Committed at [`.github/pull_request_template.md`](.github/pull_request_template.md), so GitHub prefills it rather than relying on anyone copying the block below. It was a copy-paste block for long enough that [RISKS.md](docs/project/RISKS.md) came to describe it as a control that "requires" its checklist, which it could not do while nothing populated it.

```markdown
## What
One-paragraph summary of the change.

## Why
The problem this solves. Link the ROADMAP stage or TASKS item.

## How
Notable implementation decisions. If a decision is non-obvious or was
contested, add it to DECISIONS.md and link the entry here.

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests pass against a real Postgres (testcontainers)
- [ ] Eval harness re-run (if retrieval, generation, or validation changed)

## Benchmarks
Before / after numbers, or "N/A — no measurable surface touched".

## Security
- [ ] No new SQL reaches the database outside the read-only role
- [ ] No secrets in code, logs, traces, or fixtures
- [ ] Untrusted input (NL questions, schema names) is not string-interpolated
      into SQL

## Docs
- [ ] Relevant docs/ pages updated
- [ ] CHANGELOG.md updated under [Unreleased]
```

## Documentation conventions

- Docs are filled in **as their stage lands**, not up front. A doc with no measured content carries an explicit `TBD — Stage N` marker rather than invented numbers.
- Every important engineering decision goes in [DECISIONS.md](docs/architecture/DECISIONS.md) with the alternatives considered and the tradeoff accepted.
- Every benchmark run goes in [BENCHMARKS.md](docs/ml/BENCHMARKS.md) with date, commit, dataset split, and hardware. Numbers are never overwritten — new rows are appended.

## Definition of done

A stage is done when:

1. It runs end to end from a clean checkout following the README.
2. Its tests pass in CI. **There is no CI pipeline yet** — it is Stage 6 work ([TASKS.md](docs/project/TASKS.md)). Until it exists this rule means the local gate: `ruff check` · `ruff format --check` · `mypy src/` · `pytest`, with the marker rows in [TESTING.md](docs/development/TESTING.md) §11 as the shape CI will take. Stating that rather than leaving the line to imply a pipeline that would enforce it.
3. Its documentation section is filled in with real content.
4. Its demo path is verified in [DEMO_SCRIPT.md](docs/project/DEMO_SCRIPT.md).
5. Its numbers, if any, are in BENCHMARKS.md and reproducible.
