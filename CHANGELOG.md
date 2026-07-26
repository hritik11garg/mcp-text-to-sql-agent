# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions map to build stages in [ROADMAP.md](docs/project/ROADMAP.md). Every entry that claims a number links to the [BENCHMARKS.md](docs/ml/BENCHMARKS.md) row it came from.

---

## [Unreleased]

### Added
- Documentation scaffolding: 28 documents across `docs/` plus root-level README, CHANGELOG, CONTRIBUTING, LICENSE.
- `requirements.txt` / `requirements-dev.txt` pinned to versions verified against PyPI for Python 3.12.
- `.python-version` pinning the interpreter to 3.12.

---

## Planned releases

These are targets, not shipped versions. Each becomes a real entry above when the stage lands.

### v0.1 — Core loop
Schema retrieval, SQL generation, validation, sandboxed execution. Working single-query text-to-SQL against a real database.

### v0.2 — Eval harness
Spider/BIRD subset wired up, execution accuracy measured. Establishes the baseline everything else improves against.

### v0.3 — MCP servers + client refactor
The four capabilities become MCP servers; the agent becomes an MCP client with runtime tool discovery. Runnable from any MCP host.

### v0.4 — Agent layer
Multi-step decomposition, session memory, self-correction on database errors. Multi-step task success metric.

### v0.5 — Fine-tuned schema linker
Contrastive training on question→column pairs. Recall@k before/after ablation committed.

### v0.6 — Hardening
Statement timeouts, row limits, cost caps, OpenTelemetry tracing, test suite. Production-style repo.

### v1.0 — Release
All stages complete, benchmarks committed, demo script verified end to end.
