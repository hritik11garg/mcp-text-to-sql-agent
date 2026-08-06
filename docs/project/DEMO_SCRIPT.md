# Demo Script

> **Status: Segment 1 is real and recorded. The rest are TBD per stage.** Structure and discipline below were decided at Stage 0; the first segment now has output that was actually produced rather than imagined.

Exact commands, exact questions, expected output. Written so the demo can be run under pressure without improvising — an interview is not the place to discover that the database needs re-seeding.

**Rule: every question in this file has been run successfully at least once, and the output recorded here is real.** A demo question that was never actually run is how a demo fails live.

---

## Pre-flight checklist

Run 10 minutes before, not 30 seconds before.

- [ ] `docker compose ps` — Postgres healthy
- [ ] `curl localhost:8000/ready` — all dependencies green
- [ ] Vectors present for the configured `RETRIEVER_MODEL`
- [ ] `LLM_API_KEY` set and working — `python -m generation.check` verifies the provider in one round trip
- [ ] Target dataset loaded **and verified** — `python -m benchmark.load verify ...` exits 0
- [ ] Terminal font large enough to read on a shared screen
- [ ] Recorded fallback video accessible offline
- [ ] An MCP host configured (MCP Inspector via `npx` is the zero-account option; any stdio host works)

**The demo runs entirely locally** apart from the LLM API. That is a deliberate property — it removes almost every environmental dependency from the room.

---

## Segment 1 — Single-query text-to-SQL (Stage 1) — **runnable**

**Point being made:** English in, correct SQL out, against a real database under real constraints.

**Setup.** Spider's `concert_singer` converted into PostgreSQL by this repo's own loader, indexed, and served:

```powershell
$env:DB_TARGET_SCHEMA = "spider_concert_singer"
$env:DATASET          = "spider_concert_singer"
$env:RETRIEVAL_TOP_K  = "30"
python -m api
```

`DB_TARGET_SCHEMA` and `DATASET` must name the same schema — the catalog and the session's `search_path` describe *one* database, and disagreeing is the one failure here that returns a plausible answer from the wrong tables.

| Step | Command | Expected |
|---|---|---|
| 1 | `curl localhost:8000/ready` | `{"status":"ready","dependencies":{"database":"up","database_readonly":"up"}}` |
| 2 | Ask the question below | Correct answer, with the SQL |
| 3 | Point at `steps[]` | Generation is 99.9% of the time |

**Recorded output** — run 2026-08-06, commit `ec4b23f`:

```console
$ curl -s -X POST localhost:8000/v1/query -H 'Content-Type: application/json'     -d '{"question": "How many singers are there?"}'
{"sql": "SELECT COUNT(*) FROM singer;",
 "columns": ["count"], "rows": [[6]], "row_count": 1,
 "truncated": false, "executed": true,
 "steps": [{"stage": "answer",  "duration_ms": 29081.0, "status": "ok"},
           {"stage": "execute", "duration_ms": 27.5,    "status": "ok"}],
 "usage": {"input_tokens": 501, "output_tokens": 43}}
```

**Say the latency out loud rather than hoping nobody notices.** 29 seconds is a free-tier provider under load, and `steps[]` is what proves it: everything this project owns took 28 ms. That is a better answer than a fast demo on a paid key, because it shows the instrumentation working. See [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md).

**Second question, showing the row limit is real:**

```console
$ curl -s -X POST localhost:8000/v1/query -H 'Content-Type: application/json'     -d '{"question": "Names and capacities of stadiums, highest capacity first?",
         "options": {"max_rows": 3}}'
{"sql": "SELECT name, capacity FROM stadium ORDER BY capacity DESC;",
 "rows": [["Hampden Park", 52500], ["Somerset Park", 11998], ["Stark's Park", 10104]],
 "truncated": true, ...}
```

The generated SQL has **no `LIMIT`** and three rows came back with `truncated: true`. The limit was injected into the AST, not asked for in the prompt — an instruction to a model is not an enforcement mechanism ([ADR-005](../architecture/DECISIONS.md#adr-005--limits-enforced-at-the-ast-level-not-by-prompting)). Worth 20 seconds of narration; it is the difference between a bound and a request.

**If the provider is out of quota**, the answer is `429 rate_limited` with the model named. That is a legitimate thing to show — it is the failure mode a free tier actually produces, and the envelope handles it — but have the recorded output above on a second screen.

## Segment 2 — The validation tier (Stage 1)

> **TBD — Stage 1.**

**Point being made:** invalid SQL never reaches the database, and validation is free to retry.

Show a question that produces a wrong column reference on the first attempt: `validate_sql` rejects it with the specific unknown identifier, the model corrects, the second attempt validates, and only then does anything execute. The trace shows two `validate_sql` spans and one `execute_sql` span.

**This is the segment that demonstrates the central design decision** ([ADR-002](../architecture/DECISIONS.md#adr-002--validation-and-execution-are-separate-mcp-servers)). Worth rehearsing until the narration is crisp.

**Recorded output:** TBD

## Segment 3 — Bounded blast radius (Stage 1)

> **TBD — Stage 1.**

**Point being made:** the containment claim is provable, not asserted.

| Step | Action | Expected |
|---|---|---|
| 1 | Ask the agent to delete data | Refused / generated SQL rejected as not read-only |
| 2 | Connect as the read-only role manually and `DELETE` | `permission denied` |
| 3 | `SELECT pg_read_file('/etc/passwd')` as that role | `permission denied` |
| 4 | `SELECT * FROM agent_meta.query_audit` as that role | `permission denied` |
| 5 | Run the negative test suite | All green |

Step 3 is the one worth doing deliberately. Blocking `DELETE` is the obvious control; revoking function execution is the one people forget, and demonstrating it shows the threat model was actually thought through.

**Recorded output:** TBD

## Segment 4 — Eval harness (Stage 2)

> **TBD — Stage 2.**

**Point being made:** the numbers are reproducible, and the baseline exists before any claimed improvement.

Run the harness on the smoke split live (fast), then show the full held-out results from BENCHMARKS.md. Walk through the failure taxonomy — including gold errors, counted rather than hidden.

## Segment 5 — MCP from any host (Stage 3)

> **TBD — Stage 3.**

**Point being made:** this is not a closed demo app; the capabilities are usable by any MCP host.

| Step | Action | Expected |
|---|---|---|
| 1 | Show the host config | Four servers |
| 2 | Show the tools Claude discovered | Runtime discovery, not hardcoded |
| 3 | Ask a question through the host | Correct answer via the same servers |
| 4 | Show a tool description | Says **when** to call it, with enforced limits |

**The strongest segment for a portfolio.** Someone can point their own host at these servers and query their own database — rare enough that people actually try it.

## Segment 6 — Multi-step decomposition (Stage 4)

> **TBD — Stage 4.**

**Question:** "Compare Q3 vs Q4 growth by region and flag anomalies."

Expected: the planner decomposes it, sub-queries execute, session memory holds intermediates, synthesis composes the answer. Then a follow-up ("just the top three") resolving against memory without re-running everything.

## Segment 7 — Fine-tuned retriever ablation (Stage 5)

> **TBD — Stage 5.**

**Point being made:** a measured ML contribution, honestly reported.

Side-by-side retrieval on a question the baseline misses. Then the A1/A2/A5 ablation table.

**Report the result whichever way it went.** If the fine-tune did not help, saying so with the measurement that shows it is a stronger position than an unmeasured claim.

## Segment 8 — Observability (Stage 6)

> **TBD — Stage 6.**

A trace of a self-correcting multi-step query: retries as sibling spans, latency attributed per component, cache hit rate, token spend.

---

## Interview questions to be ready for

Worth having crisp answers to, since PROJECT.md names these as where the depth lives:

1. **Why are validation and execution separate capabilities?** → [ADR-002](../architecture/DECISIONS.md#adr-002--validation-and-execution-are-separate-mcp-servers). Validation is side-effect-free and freely retryable; execution is neither. A `dry_run` boolean makes safety a runtime argument instead of a property of the capability.
2. **What does the agent see on a timeout vs a syntax error?** → Different `error_type`s with different correct responses. A syntax error is deterministic — fix the structure. A timeout is a resource signal — narrow the query. Retrying a timeout verbatim wastes the retry budget, which is why the retry prompt branches on error type.
3. **How do you bound blast radius?** → Five layers, and layer 5 (Postgres role) is the only one that actually holds. Function execution revoked, not just DML. Proven by negative tests, not asserted.
4. **Why fine-tune rather than retrieve more candidates?** → [ADR-006](../architecture/DECISIONS.md#adr-006--fine-tune-the-schema-linker-rather-than-retrieve-more-candidates). Raising k trades a retrieval problem for a context-precision problem — more chances for the generator to pick a plausible wrong column, on every query. Ablation A2 tests exactly this.
5. **How do you know the MCP layer isn't a wrapper?** → Contract design: descriptions that say when to call, schema constraints enforced server-side (with tests proving enforcement, not declaration), structured errors the agent acts on, and `execute_sql` re-validating because another host can call it directly.
6. **What would you do differently?** → Answer honestly from RISKS.md and the materialized-risks table. The limitation worth naming unprompted is single-tenancy — one read-only role sees the whole schema, documented in SECURITY.md §4 rather than glossed over.

---

## If something breaks

1. **Do not debug live past ~30 seconds.** Switch to the recorded video and narrate.
2. Have the trace UI and BENCHMARKS.md open in tabs — the numbers survive a broken demo.
3. A failure that *is* the point (validation rejecting bad SQL, permission denied) is not a broken demo. Say so clearly, because it looks identical to a real failure from the audience's side.
