# Evaluation

> **Status: TBD — Stage 2.** Metric definitions and harness design below are decided. **No results appear here until the harness is committed and reproducible.** Results live in [BENCHMARKS.md](BENCHMARKS.md); this page defines what the numbers mean.

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
| Float comparison | Tolerance 1e-6 | Aggregation ordering causes drift |
| NULL vs empty string | Distinct | They mean different things |
| Empty result | Matches only an empty gold result | Otherwise a broken query scores on every empty-result question |

**Why execution accuracy, not exact match.** Exact match penalizes correct SQL written differently — a join reordered, a subquery written as a CTE. That measures stylistic conformity, not correctness. Exact match is recorded as a secondary signal only.

**Known weakness, stated up front.** Execution accuracy is vulnerable to coincidental matches: a wrong query can return the right rows on one dataset. It is the best available metric, not a perfect one.

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

## 3. Harness design

> **TBD — Stage 2** for implementation. Requirements:

- **Deterministic and re-runnable** from a clean checkout via one command.
- **Records everything** needed to reproduce: commit, model, prompt version, retriever `model_version`, split, seed, hardware, timestamp.
- **Persists per-question artifacts** — generated SQL, gold SQL, both result sets, every validation attempt, timings. Aggregate scores without artifacts cannot be debugged.
- **Isolated database per question** where the benchmark requires it, so cross-question contamination is impossible.
- **Parallelism-safe** and bounded, so a full run finishes in reasonable wall-clock without overwhelming the database.
- **Emits both** a human-readable summary and machine-readable JSON for BENCHMARKS.md rows.

```powershell
python -m evals.run --split held-out --retriever fine-tuned --model $env:LLM_MODEL --out results/
```

## 4. Baselines

Every improvement is measured against something. Baselines to establish in Stage 2:

| Baseline | Purpose |
|---|---|
| No retrieval (full schema in prompt) | Is retrieval helping at all, or just saving tokens? |
| Baseline retriever + generation, no validation | What does the validation tier contribute? |
| Baseline retriever + validation, no self-correction | What does error feedback contribute? |
| Full pipeline, baseline retriever | The number the fine-tune must beat |

Attributing a gain to the fine-tuned retriever requires knowing what the rest of the pipeline was already worth.

## 5. Failure analysis

> **TBD — Stage 2 onward.** Aggregate scores say *how much* is broken; a failure taxonomy says *what*. Planned categories, each with a count and representative examples:

| Category | Description |
|---|---|
| Retrieval miss | Required element not in top-k — the fine-tune's target |
| Wrong column, right table | Retrieved correctly, linked incorrectly |
| Join error | Wrong join path or missing condition |
| Aggregation error | Wrong function, wrong grouping |
| Filter error | Wrong predicate, wrong literal format |
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
