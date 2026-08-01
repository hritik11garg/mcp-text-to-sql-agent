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

**Residual.** The Stage 3 gate — re-run the eval, accuracy unchanged — is still open, because the Stage 2 harness does not exist yet. Thin adapters plus contract tests are an argument that behaviour is preserved; they are not a measurement of it.

### R-04 · Spider/BIRD → Postgres conversion corrupts the benchmark
**Medium × High.** Dynamic vs static typing, identifier case-folding, date-as-text columns. A silent conversion difference invalidates every number downstream.

**Mitigation.** Conversion is verified, not assumed: every gold query executes against both the SQLite original and the Postgres copy, result sets compared. Verification is a Stage 2 gate.

### R-05 · Self-correction burns budget without recovering
**Medium × Medium.** The loop can look busy while never converging — a plausible-looking feature that adds cost and latency for nothing.

**Mitigation.** `self_correction_success_total` measured against `retry_budget_exhausted_total`, and the invalid-query rate published pre- and post-correction so the gap is visible. Error-type-aware retry prompts (a timeout is not retried verbatim) are what make recovery likely; without them the loop resubmits queries that fail identically.

### R-06 · Prompt cache silently never hits
**Medium × Medium.** Fails silently — no error, just 5–10× the expected token bill.

**Mitigation.** Prefix stability is a Stage 1 test, and `cache_read_input_tokens` is a monitored metric with an alert when it collapses.

### R-07 · Read-only containment has a hole
**Low × Critical.** Low likelihood because Postgres does the enforcement; critical because it is the central claim of the project.

**Mitigation.** Negative tests that must fail, gating Stage 1 rather than Stage 6. The commonly-missed case is function execution — `pg_read_file` and `COPY ... TO PROGRAM` are reachable from a `SELECT`-only role unless `EXECUTE` is revoked. Explicitly tested.

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

**Mitigation.** Split by database. Disjointness is asserted in code, and splits are committed as a file rather than regenerated.

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

**Mitigation.** Per-stage fill-in with explicit `TBD — Stage N` markers ([ADR-012](../architecture/DECISIONS.md#adr-012--documentation-written-per-stage-not-up-front)), so a `TBD` is a real signal rather than filler. Stage close-out checklists include the doc updates. The PR template requires them.

**The honest read:** this risk is real and partly accepted. A document describing intent that the code later contradicts is worse than no document. The `TBD` markers and stage gates are what keep it manageable.

---

## Materialized risks

| Date | Risk | What happened | Resolution |
|---|---|---|---|
| — | — | — | *None yet* |
