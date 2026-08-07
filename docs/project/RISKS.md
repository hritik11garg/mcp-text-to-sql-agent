# Risks

Scored `likelihood × impact`, both low/medium/high. Reviewed at each stage boundary. A risk that materializes gets its outcome recorded here — the log of what actually went wrong is more useful than the predictions.

---

## Technical

### R-01 · Fine-tuned retriever does not beat baseline
**High × Medium.** Small pair set, strong pretrained baselines, and a genuine possibility that off-the-shelf embeddings are already good enough on these schemas.

**Mitigation.** Baseline is established in Stage 2, before any training, so the comparison is honest. Ablations are designed to isolate the contribution rather than assert it. **A null result is publishable** ([ADR-006](../architecture/DECISIONS.md#adr-006--fine-tune-the-schema-linker-rather-than-retrieve-more-candidates)) — "I measured it and it didn't help, here's why" demonstrates more judgment than an unmeasured claim that it did.

**Trigger.** A1 shows no improvement, or improvement inside seed variance.

### R-02 · Recall@k improves but execution accuracy doesn't
**Medium × High.** Retrieval may not be the real bottleneck. If generation is what's failing, better retrieval buys nothing end to end.

**Mitigation.** Ablation A5 measures exactly this, and it is a required Stage 5 deliverable rather than a nice-to-have. If it comes out flat, the finding is that the bottleneck is elsewhere — which redirects the remaining effort correctly.

### R-03 · MCP refactor reads as a wrapper
**Medium × High.** The stated failure mode in PROJECT.md: "I wrapped three functions in a protocol." Spotted in two minutes.

**Mitigation — implemented.** Contract quality was the Stage 3 deliverable, not the plumbing. Descriptions that say *when* to call, asserted by contract tests. Schema-enforced limits whose published ceilings are **imported from** the components that clamp them, so the advertised number and the enforced one cannot drift. Structured errors the agent can act on, with `error_type` ordered most-specific-first. `execute_sql` re-validating independently, because another host can call it directly. The `validate_sql`/`execute_sql` split is the design argument, recorded in [ADR-002](../architecture/DECISIONS.md#adr-002--validation-and-execution-are-separate-mcp-servers).

**What actually reduced this risk most** was building the servers *last*, over components that had been designed without any knowledge of MCP. Every bound already lived where all callers pass, so the servers had nothing left to enforce — which is what makes them thin rather than what makes them wrappers. A protocol wrapper is what you get when the protocol layer is where the thinking happens.

**Residual.** The Stage 3 gate — re-run the eval, accuracy unchanged — is still open. The harness, the loader and now a verified benchmark all exist; the one thing missing is the pipeline wired into the answerer seam. Thin adapters plus contract tests are an argument that behaviour is preserved; they are not a measurement of it, and the measurement is now blocked on exactly one connection.

### R-04 · Spider/BIRD → Postgres conversion corrupts the benchmark
**Medium × High.** Dynamic vs static typing, identifier case-folding, date-as-text columns. A silent conversion difference invalidates every number downstream.

**Mitigation — implemented.** Conversion is verified, not assumed: every gold query executes against both the SQLite original and the Postgres copy, result sets compared with the eval harness's *own* comparator ([ADR-022](../architecture/DECISIONS.md#adr-022--the-conversion-is-verified-by-the-eval-harnesss-own-comparator)). A database is verified only if **every** comparable query agrees, and `benchmark.load verify` exits 3 otherwise, so a CI step cannot pass while reporting the data is wrong. Gold errors — reference queries that fail on their own SQLite database — are counted separately and never blamed on the conversion.

**Exercised, and it caught things.** Spider dev has now been through it: 20 of 20 databases converted, **915 of 921 comparable gold results reproduced, 19 of 20 databases reproducing every one**. The control did its job in the way that matters — it found defects that were *not* obvious, including a foreign key joining two types in 35 of 769 cases and a column whose type was inferred from a sample that could not reach the one value that broke it. The full record is [BENCHMARKS.md](../ml/BENCHMARKS.md) §0, which keeps both measurements rather than only the better one.

**Residual, and it is now specific rather than hypothetical.** Six questions still differ, all of them `wta_1.players.birth_date` — 20,144 integers and 518 empty strings, a column no static type is faithful to. That is the risk materialising in its mildest form: bounded, named, and reported instead of silent. It is unfixable without changing what the data *is*, so any number computed over `wta_1` carries it.

**Still least proven where it matters most.** BIRD has not been downloaded. Spider is the *clean* benchmark, and mixed-storage columns like the one above are BIRD's common case rather than its rare one — so the residual above is a preview, not a total.

### R-05 · Self-correction burns budget without recovering
**Medium × Medium.** The loop can look busy while never converging — a plausible-looking feature that adds cost and latency for nothing.

**Mitigation.** `self_correction_success_total` measured against `retry_budget_exhausted_total`, and the invalid-query rate published pre- and post-correction so the gap is visible. Error-type-aware retry prompts (a timeout is not retried verbatim) are what make recovery likely; without them the loop resubmits queries that fail identically.

### R-06 · Prompt cache silently never hits
**Medium × Medium.** Fails silently — no error, just 5–10× the expected token bill.

**Mitigation.** Prefix stability is a Stage 1 test, and `cache_read_input_tokens` is a monitored metric with an alert when it collapses.

### R-07 · Read-only containment has a hole
**Low × Critical.** Low likelihood because Postgres does the enforcement; critical because it is the central claim of the project.

**Mitigation.** Negative tests that must fail, gating Stage 1 rather than Stage 6. The commonly-missed case is function execution — `pg_read_file` and `COPY ... TO PROGRAM` are reachable from a `SELECT`-only role unless `EXECUTE` is revoked. Explicitly tested.

**The hole this risk missed was one level up.** Every one of those tests asserts what `sql_agent_ro` may do. None of them asserted that `DATABASE_RO_URL` *points at that role* — and the only check that existed compared the two DSN strings, which two spellings of the same superuser pass. A deployment configured that way passes every negative test in the suite, because the suite builds its own role and never looks at the one production connects as. `composition.assert_read_only` now proves it at startup ([SECURITY.md §13.2](../operations/SECURITY.md)). The general shape is worth keeping: **a control tested only against the fixture that satisfies it has been tested against itself.**

**If it materializes:** treat as a security incident, not a bug ([TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md)).

### R-08 · Large schemas overwhelm the context budget
**Medium × Medium.** BIRD schemas are large; naive retrieval plus profiling can fill the context window.

**Mitigation — implemented.** `k` is clamped to 50 at the retriever. `profile_table` truncation is mandatory rather than best-effort: `PROFILE_MAX_COLUMNS` caps a profile at 30 columns and reports how many it dropped, values are truncated in SQL at `PROFILE_MAX_VALUE_CHARS`, and frequent values are capped at `PROFILE_TOP_K`. An unbounded profile of a wide table remains the single most likely way to blow the budget in one tool result — which is why the cap truncates and *says so* rather than refusing, so the agent asks for the columns it actually needs instead of concluding the rest do not exist.

### R-09 · torch/CUDA install friction on Windows
**High × Low.** Near-certain, cheap to fix, expensive if hit during a demo.

**Mitigation.** Pre-documented in [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md); CPU-only wheel index is the documented path since inference does not need CUDA.

### R-10 · LLM non-determinism makes tests flaky
**Medium × Medium.** Asserting on model output produces a suite nobody trusts.

**Mitigation.** Strict separation: plumbing is tested deterministically with a fake LLM; model quality is *measured* by the eval harness. The two are never conflated ([TESTING.md](../development/TESTING.md) §1).

---

## Data

### R-11 · Benchmark gold queries are wrong
**High × Low.** Known to occur, especially in BIRD. Caps achievable accuracy.

**Mitigation.** Counted as a failure category and reported, never silently dropped — dropping them inflates the score.

### R-12 · Train/eval leakage inflates Recall@k
**Medium × High.** Splitting by question instead of database puts the same schema elements on both sides, producing an impressive, meaningless number.

**Mitigation — implemented.** Split by database. Disjointness holds by construction: a `db_id` hashes into exactly one band, and a question naming a database with no assignment raises rather than defaulting into dev. Splits are committed as a file rather than regenerated, and membership depends only on the database's own name, so growing the corpus cannot move a held-out database into train ([ADR-021](../architecture/DECISIONS.md#adr-021--splits-are-a-hash-of-the-database-name-not-a-seeded-shuffle)).

### R-13 · Held-out set gets used for iteration
**Medium × Medium.** Easy to do accidentally; converts the reported number into an optimistic one.

**Mitigation.** Dev split exists for iteration. If it happens, it is recorded and a fresh held-out split is carved from unused databases.

---

## Cost and operations

### R-14 · LLM spend runs away during eval
**Medium × Medium.** Full-benchmark runs across configurations multiply quickly, and a non-converging agent loop multiplies further.

**Mitigation.** Smoke split for iteration; full runs only for reported results. `MAX_TOOL_CALLS_PER_REQUEST` is a hard cap. Token-spend metric with an anomaly alert.

### R-15 · Demo fails during an interview
**Medium × High.** The scenario every other risk feeds into.

**Mitigation.** [DEMO_SCRIPT.md](DEMO_SCRIPT.md) with exact commands, expected output, and a pre-flight checklist. Recorded fallback video. **The demo runs entirely locally** — no dependency on network conditions in the room beyond the LLM API.

---

## Schedule

### R-16 · A stage overruns and cascades
**High × Medium.** 5–6 weeks is tight, and Stage 1 is the most likely to overrun.

**Mitigation.** The stage structure is the mitigation — each produces something demoable, so a stall costs a capability rather than the whole build. Stage ordering already puts the highest-variance work (Stage 5) late, so a null result there costs one stage.

**If Stage 1 overruns:** cut scope inside it (fewer datasets, simpler retrieval), never cut the security tests. The containment story is the differentiator.

### R-17 · Documentation drifts from implementation
**High × Medium.** 28 documents written before the code is a lot of surface to keep honest.

**Mitigation.** Per-stage fill-in with explicit `TBD — Stage N` markers ([ADR-012](../architecture/DECISIONS.md#adr-012--documentation-written-per-stage-not-up-front)), so a `TBD` is a real signal rather than filler. Stage close-out checklists include the doc updates. The PR template at `.github/pull_request_template.md` requires them.

**Where the mitigation is now enforced rather than asked for:** `tests/unit/test_settings.py` fails if a setting exists in code and not in `.env.example` and CONFIG.md. That is the only part of this risk that is *checkable*, and it was added after an audit found 18 of 50 settings undocumented — the drift happened, silently, exactly as this risk predicted.

**The honest read:** this risk is real and partly accepted, and it is **the most frequently materialised risk on this page** — see the table below, which now records six occurrences. A document describing intent that the code later contradicts is worse than no document. The `TBD` markers and stage gates keep it manageable; only assertions keep it honest, and most of this surface cannot be asserted.

**The pattern across all six is worth naming: the drift is almost never in the document that was edited.** A slice updates the page it is about, and the stale claim turns up somewhere adjacent — a status block at the top of the same file, a *planned* table naming something that shipped under another name, a demo script narrating a number that a later measurement overturned. That is why the audits are periodic and whole-tree rather than per-slice, and why two of the six were found by a script rather than by reading.

---

## Materialized risks

**This table said *"None yet"* while [R-17](#r-17--documentation-drifts-from-implementation) three screens above said the risk had materialised three times.** A register of materialised risks that contradicts the register entries above it is the same defect it exists to track, so it is now filled in.

| Date | Risk | What happened | Resolution |
|---|---|---|---|
| 2026-08-02 | **R-04** · conversion corrupts the benchmark | `wta_1.players.birth_date` holds 20,144 integers *and* 518 empty strings, so no static type is faithful. 6 of 921 gold results do not reproduce | **Caught by the mitigation**, which is the point — `benchmark.load verify` compares every gold result and exits 3. The residual is bounded, recorded in [BENCHMARKS.md](../ml/BENCHMARKS.md) §0, and inherited by every accuracy row |
| 2026-08-02 | **R-11** · benchmark gold queries are wrong | 3 mismatches were SQLite's case-insensitive `LIKE`; 16 were `LIMIT` cutting a tie, a question with no single correct answer | Excluded from the denominator as `undetermined_limit` and reported as excluded. Both fidelity rows kept |
| 2026-08-04 | **R-17** · documentation drifts | An audit found **18 of 50 settings** in the code and in neither `.env.example` nor CONFIG.md — two of them security controls whose safe defaults made the omission invisible | The only *assertable* part of this risk: `tests/unit/test_settings.py` now fails on it. It has caught one since |
| 2026-08-05 | **R-07** · read-only containment has a hole | Thirty negative tests proved the *migration* builds a correct role and none checked that `DATABASE_RO_URL` **points at it**. The only existing check compared DSN strings, which two spellings of one superuser pass | `composition.assert_read_only` proves it at startup ([ADR-033](../architecture/DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing)). Nineteen versions of a verified-looking claim |
| 2026-08-06 | **R-14** · LLM spend runs away during eval | The free tier's daily cap halted the full-split run twice, on consecutive days, at questions 414 and 791 | **Working as designed rather than a failure.** `--halt-after` stops on 10 consecutive infrastructure failures instead of asking a dead provider 500 more times, and resumption re-attempts them. The run is multi-day by budget, not by size |
| 2026-08-07 | **R-17** · documentation drifts | A whole-tree audit found three more: SYSTEM_ARCHITECTURE's status block saying `POST /v1/query` was not built while §2.1 of the same file said it was; CONFIG.md's *planned* table naming four settings that had shipped under different names and one in the wrong unit; DEMO_SCRIPT narrating a latency figure a later measurement overturned | Fixed. Two of the three were found by a script checking mechanical claims, which is now the argument for keeping one |
