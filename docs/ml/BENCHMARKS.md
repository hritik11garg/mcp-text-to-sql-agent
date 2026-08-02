# Benchmarks

**Append-only.** Rows are never edited or deleted, including regressions. The trajectory is the interesting part, and a number that quietly improved without an entry explaining why is not a result.

Metric definitions: [EVALUATION.md](EVALUATION.md). Every row must be reproducible from the recorded command.

> **No accuracy runs yet — Stage 2 produces the first.** The tables in §1–§7 are the recording format. §0 is real: it records what the data those numbers will be computed from is actually worth.

---

## Recording rules

Each row records:

| Field | Why |
|---|---|
| Date | Ordering |
| Commit | The only way to reproduce it |
| Split | `dev` / `held-out` / `smoke` — **never mix them in one table** |
| Dataset | Spider / BIRD / both, with subset size |
| Metric | `execution accuracy (single DB)` unless stated. **Not** Spider's official Test Suite Accuracy, which is stricter — see [EVALUATION.md](EVALUATION.md) §2 |
| Archive digest | The `sha256` from `data/artifacts.lock.json`. Both benchmarks have been re-released with corrections, so two rows computed from different archives are not comparable and nothing else in this table would say so |
| Conversion verified | That `benchmark.load verify` exited 0 for every database in the split. A number from an unverified conversion measures the conversion, not the system. §0 records the fidelity every row depends on |
| Questions excluded | How many left the denominator and why — `gold_error`, `transpile_error`, `dialect_error`. An exclusion that is not reported is indistinguishable from cheating |
| Retriever | `model_version` from `schema_elements` |
| LLM | Model ID + effort level |
| Prompt version | From [PROMPTS.md](PROMPTS.md) |
| Seed | Where sampling applies |
| Hardware | CPU/GPU — latency numbers are meaningless without it |
| Command | The exact invocation |

A row missing any of these is not a benchmark; it is an anecdote.

---

## 0. Conversion fidelity

**Every row in §1 inherits this one.** Execution accuracy measured on a converted database is bounded above by how faithfully that database was converted, and a system score reported without this number is unattributable — a drop could be the model, the retriever, or the data.

Fidelity is `(match + ambiguous_order) / (questions − gold_error − transpile_error − dialect_error)`. Definitions in [DATASETS.md](DATASETS.md) §3.1.

| Date | Commit | Dataset | Databases | Questions | Fidelity | Excluded | Verified | Command |
|---|---|---|---|---|---|---|---|---|
| 2026-08-02 | `5551fb5` | Spider `dev.json`, digest `00636695…c85b121b` | 20 / 20 converted | 1034 | **912 / 937 = 97.3%** — 896 match, 16 ambiguous order, 25 mismatch, 0 postgres error | 97 dialect error (56 `GROUP BY`, 41 type affinity) | **10 / 20 databases** | `python -m benchmark.load verify --databases data/spider/spider_data/database --questions data/spider/spider_data/dev.json --benchmark spider --prefix spider_` |

**Open:** the 25 mismatches — 22 `no_column_bijection`, 3 `shape_mismatch`, spread over the 10 unverified databases — are not yet diagnosed. Until they are, an accuracy number from those 10 databases carries an unknown share of conversion error and must say so.

## 1. Execution accuracy

> **TBD — Stage 2.**

| Date | Commit | Split | Dataset | Retriever | LLM | Prompt | Exec. acc. | Notes |
|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | *No runs yet* |

## 2. Schema-linking recall

> **TBD — Stage 2 (baseline) / Stage 5 (fine-tuned).**

| Date | Commit | Split | Retriever | R@1 | R@5 | R@10 | R@20 | Notes |
|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | *No runs yet* |

## 3. Invalid-query rate

> **TBD — Stage 2 (baseline) / Stage 4 (with self-correction).**

Pre-correction is the first-attempt rate; post-correction is after the retry budget is exhausted. **The gap between them is what the validation tier is worth** — reporting only the post number hides the contribution.

| Date | Commit | Split | Config | Invalid (pre) | Invalid (post) | Mean attempts | Notes |
|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | *No runs yet* |

## 4. Multi-step task success

> **TBD — Stage 4.** Grading method must be recorded per row — rubric-automatic vs human changes what the number means.

| Date | Commit | Split | Tasks | Success | Grading | Notes |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | *No runs yet* |

## 5. Latency

> **TBD — Stage 6.** Hardware is mandatory. Targets in [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md).

| Date | Commit | Component | p50 | p95 | p99 | Hardware | Notes |
|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | *No runs yet* |

## 6. Cost

> **TBD — Stage 2 onward.** Necessary context for every accuracy comparison — an accuracy gain at 4× cost is a different result from a free one.

| Date | Commit | LLM | Effort | Input tok/q | Output tok/q | USD/q | Exec. acc. | Notes |
|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | *No runs yet* |

## 7. Ablations

> **TBD — Stage 5.** Design in [TRAINING.md](TRAINING.md) §8.

| ID | Comparison | Result | Verdict |
|---|---|---|---|
| A1 | Baseline vs fine-tuned @ equal k | TBD | — |
| A2 | Fine-tuned @ k=5 vs baseline @ k=20 | TBD | — |
| A3 | In-batch vs mined hard negatives | TBD | — |
| A4 | Serialization field contribution | TBD | — |
| A5 | Recall@k → execution accuracy | TBD | — |

---

## Regression log

Regressions get their own entries. A change that made something worse is a finding, and hiding it makes every other number here less trustworthy.

| Date | Commit | Metric | From | To | Cause | Resolution |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | *None yet* |
