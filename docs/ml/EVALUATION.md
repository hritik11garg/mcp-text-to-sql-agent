# Evaluation

> **Status: the harness runs end to end, and the first numbers are smoke rows over 3 of 20 databases.** Result comparison, Recall@k, the failure taxonomy, per-question artifacts, resumable runs and the answerer seam ship in `src/evals/`. Spider's dev split is converted and verified — 19 of 20 databases reproduce every gold result (§2) — indexed, and answered against. The first runs found five defects and moved execution accuracy 42.7% → 75.3% without a prompt or model change; see [BENCHMARKS.md](BENCHMARKS.md) §1, which states on every row why none of them is comparable to a published Spider number. This page defines what the numbers mean.

The eval harness is Stage 2 — deliberately before the MCP refactor, the agent layer, and the fine-tune. Without a baseline, every later change is an unfalsifiable claim of improvement.

---

## 1. Metrics

### 1.1 Execution accuracy (primary)

Fraction of generated queries whose **result set** matches the gold query's result set.

Comparison rules — each is a judgement call that changes the number, so each is stated explicitly:

| Rule | Choice | Why |
|---|---|---|
| Column order | Ignored | Semantically irrelevant |
| Row order | Ignored unless gold has `ORDER BY` | Ordering is only meaningful when requested |
| Column names | Ignored | Aliasing is stylistic |
| Duplicate rows | Significant | `DISTINCT` changes meaning |
| Float comparison | Equal to 6 decimal places | Aggregation ordering causes drift well below that. Implemented as rounding, not a tolerance — see below |
| NULL vs empty string | Distinct | They mean different things |
| Empty result | Matches only an empty gold result | Otherwise a broken query scores on every empty-result question |
| Numeric types | `int`, `float`, `Decimal` unified | `SUM(x)` returns `Decimal` where `x` returns `int`; no meaningful difference is being hidden |
| A number vs its string | **Distinct** | A query returning `'1'` where the reference returns `1` has a real defect |
| Booleans vs numbers | **Distinct** | Added when the harness was built — the table above did not cover it. Consistent with the row above, and strict, which understates accuracy rather than inflating it |

**Column order and column names are both ignored, which leaves nothing to match columns by except their contents.** So comparison searches for a bijection between predicted and gold columns rather than zipping them positionally. The search is bounded at 8 columns; above that, columns are matched positionally and the result records that it did — scoring under a *stricter* rule than this table documents, which an aggregate must not hide.

**Float equality is implemented as rounding to 6 decimal places, not as `|a-b| < 1e-6`.** A tolerance-based equality is not transitive: with a=1.0000000, b=1.0000005, c=1.0000010, a≈b and b≈c but a≉c. Sorting and multiset comparison both require transitivity, so a non-transitive equality would make the verdict depend on the order rows happened to arrive in — which is the one property a benchmark cannot have. See [ADR-018](../architecture/DECISIONS.md).

**Why execution accuracy, not exact match.** Exact match penalizes correct SQL written differently — a join reordered, a subquery written as a CTE. That measures stylistic conformity, not correctness. Exact match is recorded as a secondary signal only.

**Known weakness, stated up front.** Execution accuracy is vulnerable to coincidental matches: a wrong query can return the right rows on one dataset. This is exactly what Spider's official **Test Suite Accuracy** exists to catch — it re-runs against several randomly generated databases — so numbers here are systematically the more generous of the two. It is the best metric available without generating those databases, not a perfect one. See §2.

### 1.2 Recall@k (retrieval)

Of the schema elements referenced by the gold SQL, the fraction present in the retriever's top-k. Reported at k = 1, 5, 10, 20.

Recall@k bounds everything downstream — an unretrieved column cannot appear in correct SQL — which is why it is measured separately rather than inferred from end-to-end accuracy.

### 1.3 Invalid-query rate

Fraction of generated queries that fail to parse or fail `EXPLAIN`. Reported at two points:

- **Pre-correction** — first generation attempt.
- **Post-correction** — after the self-correction loop is exhausted.

The gap between the two is what the validation tier plus error-feedback loop is actually worth. Reporting only the post-correction number would hide it.

### 1.4 Multi-step task success

For compound questions: whether the final synthesized answer is correct. Binary per task, graded against a rubric — intermediate queries may differ from any reference and still produce a correct answer.

Grading method (rubric-based automatic grading, human grading, or both) is **TBD — Stage 4**, along with an inter-rater agreement check if human grading is used. A metric whose grading procedure is unspecified is not a metric.

### 1.5 Latency

End-to-end and per-component p50/p95/p99. Targets and measured results in [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md).

### 1.6 Cost

Tokens and USD per question, split by prompt/completion and by agent step. Necessary context for any accuracy comparison across models or effort levels — an accuracy gain at 4× cost is a different result from a free one.

## 2. Datasets

Spider and BIRD subsets. Split construction, licensing, and exact sizes in [DATASETS.md](DATASETS.md).

Three splits, with strictly different jobs:

| Split | Used for | Frequency |
|---|---|---|
| **Dev** | Iteration, prompt tuning, debugging | Continuously |
| **Held-out** | Reported numbers | Once per reported result |
| **Smoke** (~20 questions) | Fast regression check | Every commit |

**The held-out split is not an iteration surface.** Tuning against it converts it into a dev split and the reported number becomes optimistic. If it gets used for iteration, that is recorded and a fresh held-out split is carved.

**Execution accuracy here is not Spider's official metric.** Spider has used **Test Suite Accuracy** since November 2020 — the query is run against several randomly generated databases so that a query returning the right rows by coincidence on one instance is caught. This harness executes against a single database, which is the *more generous* of the two: it counts false positives a test suite would reject. Every BENCHMARKS.md row must name which metric it is, and a number from here belongs in a published *range*, not next to a leaderboard entry. See [DATASETS.md](DATASETS.md) §1.

**Splits are files, and the assignment is a property of the database name.** `python -m benchmark.load splits` writes one JSONL per split plus a committed assignment map. Membership is `blake2b(seed:db_id)` into a fixed band rather than a seeded shuffle, so adding databases never moves the ones already assigned — a shuffle would silently move held-out databases into train, and the split file would look exactly as deterministic as before ([ADR-021](../architecture/DECISIONS.md#adr-021--splits-are-a-hash-of-the-database-name-not-a-seeded-shuffle)).

**The questions must come from a *verified* conversion.** Every gold query is executed on both the original SQLite and the converted PostgreSQL copy before a database is eligible to be scored against, using this document's own comparator ([DATASETS.md](DATASETS.md) §3.1). A conversion defect does not raise — it lowers an accuracy number, and the investigation that follows looks at the model.

Measured on Spider dev: **19 of 20 databases reproduce every gold result; 99.3% of comparable questions do.** That number is the ceiling on any accuracy computed from those databases, and BENCHMARKS.md §0 is where it is recorded so a later row can be read against it. The single unverified database, `wta_1`, differs on one column that has no faithful static type.

**A question that cannot be scored leaves the denominator, and that must be reported.** §5's rule for gold errors extends to two more classes:

- `dialect_error` — PostgreSQL rejects the query for a reason no conversion could fix (`GROUP BY` rules SQLite does not enforce, comparisons relying on type affinity). **97** of Spider dev's 1034.
- `undetermined_limit` — the gold `ORDER BY` ties across its `LIMIT`, so the question has several equally correct answers and no comparison can score it. **16** of 1034.

Excluding them is correct, because they cannot be scored either way; **not saying so is not**, since the exclusion raises every percentage computed afterwards. 113 of 1034 questions on this split are unscoreable, and every row reporting a score over it says so.

> **Open — and it bounds what any number here may be compared against.** The splits above are hash-assigned over the *combined* corpus, so `dev` is not Spider's `dev.json`. Published Spider results are computed on Spider's own dev set. Until this is decided (see [DATASETS.md](DATASETS.md) §5), a score from this harness is comparable only to another score from this harness.

## 3. Harness design

Requirements, and where each stands:

- [x] **Deterministic and re-runnable** from a clean checkout via one command.
- [x] **Records everything** needed to reproduce: commit, model, prompt version, retriever `model_version`, split, seed, timestamp — written as a manifest *before* the first question, so an interrupted run still records what it was trying to do. Hardware is not captured yet.
- [x] **Persists per-question artifacts** — generated SQL, gold SQL, both result sets, attempts, timings, and which model actually answered. Aggregate scores without artifacts cannot be debugged.
- [x] **Resumable.** Not in the original list, and it turned out to be the requirement that shaped the design — see below.
- [x] **Isolated database per question** where the benchmark requires it. One converted schema, one catalog namespace, one retriever, resolved per question and cached ([ADR-031](../architecture/DECISIONS.md#adr-031--one-database-one-schema-one-catalog-namespace-resolved-per-question)). Spider's 20 dev databases share table names, so this is what stops one question being answered with another database's columns.
- [ ] **Parallelism-safe** and bounded. Sequential today; on a free tier the binding constraint is tokens per minute, not wall-clock.
- [x] **Emits both** a human-readable progress stream (stderr) and machine-readable JSON (stdout), so the command composes.

**Resumability is not a convenience here.** Free-tier models cap tokens per model per day, so a 200-question run spans most of a budget and being stopped at question 140 is an ordinary operating condition. Every question is written as it completes and a re-run skips what is already on disk.

The corollary is a refusal: a resumed run must have the **same configuration fingerprint** — dataset, split, model, retriever version, prompt version, commit and seed. Resuming after any of those change would produce a results directory that is half one configuration and half another, and every number computed from it would be a weighted average of two things nobody meant to average. The commit is in the fingerprint deliberately: fixing a bug and re-running is the easiest way to do this by accident.

**The pipeline is injected, not built in.** The runner takes an *answerer* — anything that turns a question into candidate SQL — so the five baselines in §4 are five answerers over one orchestration, and the whole harness is testable with a scripted one. It also means gold and predicted SQL go through the *same* query runner: different connections or type adapters would return `Decimal('1')` on one side and `1.0` on the other, and a correct answer would be reported as a value mismatch.

**Gold SQL is an input, not a derivation.** `--gold` is required. The split file holds the benchmark's own SQLite SQL; the file named by `--gold` is what `benchmark.load verify --emit-gold` wrote — the PostgreSQL statement each gold query became *and* the outcome of comparing its results against SQLite. Re-transpiling here would mean an edit to the transpiler changed every reference answer with nothing re-checking it ([ADR-030](../architecture/DECISIONS.md#adr-030--the-eval-runs-the-gold-sql-verification-produced-and-never-re-derives-it)).

**The denominator comes from the same file.** Questions verification marked unscoreable — a gold query PostgreSQL cannot express, a `LIMIT` that cut a tie — are dropped before the run and reported in the summary as `excluded_by_outcome`. On Spider dev that is **921 scoreable of 1034**. Scoring against all 1034 would report a number about eleven points lower for reasons that have nothing to do with the model.

### 3.1 Running it

Three commands, in order. The first two are per conversion; the third is per run.

```powershell
# 1. Convert (once per archive) and emit the verified gold
python -m benchmark.load verify --databases data/spider/spider_data/database `
    --questions data/spider/spider_data/dev.json --benchmark spider --prefix spider_ `
    --emit-gold data/splits/spider-dev-gold.jsonl --report reports/verify-dev.json

# 2. Build the schema catalog for every converted database
python -m benchmark.load index --databases data/spider/spider_data/database --prefix spider_

# 3. Run a baseline
python -m evals.run --questions data/splits/spider-dev.jsonl --split dev `
    --gold data/splits/spider-dev-gold.jsonl --prefix spider_ `
    --baseline retrieval-only --dataset spider --out results/
```

Step 2 is not optional and is easy to forget. Without a catalog every identifier resolves to nothing, retrieval returns nothing, and every question fails for the same uninformative reason — so the run refuses at the first question instead, naming the command.

## 4. Baselines

Every improvement is measured against something. Baselines to establish in Stage 2:

| Baseline | `--baseline` | Purpose |
|---|---|---|
| No retrieval (full schema in prompt) | `full-schema` | Is retrieval helping at all, or just saving tokens? |
| Baseline retriever + generation, no validation | `retrieval-only` | What does the validation tier contribute? |
| Baseline retriever + validation, no self-correction | `with-validation` | What does error feedback contribute? |
| Full pipeline, `ENABLE_PROFILE_TABLE=false` | *Stage 4* | What does profiling contribute? Expected to move **filter errors** specifically, not accuracy uniformly — a profile tells the model a column stores `'FI'` rather than `'Finland'`, which nothing else in the pipeline can |
| Full pipeline, baseline retriever | *Stage 5* | The number the fine-tune must beat |

Attributing a gain to the fine-tuned retriever requires knowing what the rest of the pipeline was already worth.

**The first three are configurations of the answerer, not flags in the runner** — which is what the seam was designed for, and the reason adding them changed nothing in `EvalRunner`. `with-validation` deliberately does **not** retry: validation alone cannot raise accuracy, it can only convert an execution failure into a refusal that names its reason, and its contribution shows in the invalid-query columns rather than the accuracy one. Self-correction is the next baseline and is Stage 4. If accuracy moves between `retrieval-only` and `with-validation`, something other than validation did it.

**The profiling ablation needs its failure category read, not just its total.** If it improves execution accuracy by a point while halving filter errors, the total is hiding the effect — and if it improves the total without moving filter errors, the gain came from somewhere else and the attribution is wrong.

## 5. Failure analysis

> **TBD — Stage 2 onward.** Aggregate scores say *how much* is broken; a failure taxonomy says *what*. Planned categories, each with a count and representative examples:

| Category | Description |
|---|---|
| Retrieval miss | Required element not in top-k — the fine-tune's target |
| Wrong column, right table | Retrieved correctly, linked incorrectly |
| Join error | Wrong join path or missing condition |
| Aggregation error | Wrong function, wrong grouping |
| Filter error | Wrong predicate, wrong literal format. **Split these two**: a wrong predicate is a reasoning failure, a wrong literal format (`'Finland'` for a column holding `'FI'`) is an information failure and is the one profiling is supposed to fix. Reported together, the ablation above is unreadable |
| Ambiguity | Question genuinely underspecified — arguably not a model failure |
| Gold error | The benchmark's reference query is wrong (this happens, especially in BIRD) |
| Timeout | Query correct but too expensive |
| Unrecoverable syntax | Retry budget exhausted |

**Gold errors are counted and reported, not silently discarded.** They cap achievable accuracy, and quietly dropping them inflates the score.

## 6. Comparison table

> **TBD — Stage 2 onward.** Filled from BENCHMARKS.md as stages land. Shape:

| Configuration | Exec. acc. | Recall@5 | Invalid (pre) | Invalid (post) | p95 latency | $/query |
|---|---|---|---|---|---|---|
| Full schema, no retrieval | TBD | — | TBD | TBD | TBD | TBD |
| Baseline retriever | TBD | TBD | TBD | TBD | TBD | TBD |
| Baseline + validation | TBD | TBD | TBD | TBD | TBD | TBD |
| Baseline + validation + self-correction | TBD | TBD | TBD | TBD | TBD | TBD |
| **Fine-tuned retriever (full pipeline)** | TBD | TBD | TBD | TBD | TBD | TBD |

## 7. Reporting rules

1. Every number carries the run that produced it. No number appears in the README that is not traceable to a BENCHMARKS.md row.
2. Results are **appended**, never overwritten. The trajectory is informative, including the regressions.
3. Negative results are published. A fine-tune that does not help is a finding.
4. Comparisons change one variable at a time, or the confound is stated explicitly.
5. Held-out numbers are labelled as such and distinguished from dev numbers everywhere they appear.
