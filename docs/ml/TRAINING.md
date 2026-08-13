# Training — Schema Linker

> **Status: TBD — Stage 5.** The plan below is design intent. Every hyperparameter is a starting point, not a result, and no numbers are reported until runs are committed to [BENCHMARKS.md](BENCHMARKS.md).

**Goal.** Beat the off-the-shelf embedding baseline on Recall@k for question→schema-element retrieval, measured on a held-out split, with the improvement isolated by ablation.

**Falsifiability.** If the fine-tune does not beat baseline-at-equal-k, that is a result and gets published here as one. See [ADR-006](../architecture/DECISIONS.md#adr-006--fine-tune-the-schema-linker-rather-than-retrieve-more-candidates).

**The baseline is final, and it constrains this stage before it starts.** Over the **whole** Spider dev split — 921 questions, all 20 databases ([BENCHMARKS.md](BENCHMARKS.md) §2): **R@1 0.7445, R@5 0.9435, R@10 0.9828, R@20 0.9973.**

Read those as a headroom budget rather than a scoreboard:

| Metric | Baseline | Headroom to perfect |
|---|---|---|
| R@1 | 0.7445 | **25.6 points** — where a fine-tune can actually show something |
| R@5 | 0.9435 | 5.65 points |
| R@10 | 0.9828 | 1.7 points |
| R@20 | 0.9973 | **0.27 points** — effectively none |

**A1 and A2 are therefore not equally winnable, and the plan says so now rather than discovering it.** A2 asks whether fine-tuning at `k=5` beats the baseline at `k=20`; the baseline at `k=20` retrieves the right elements 99.73% of the time, so A2 is close to unwinnable *on Spider* by construction. That is not a reason to drop it — it is ADR-006's core claim and a null result is publishable — but it is a reason to run it on **BIRD**, whose schemas are larger and where retrieval has somewhere to fail.

**Every one of these numbers moved as coverage grew, and two moved in the direction that matters here.** R@20 read 1.000 at 150 questions, 1.000 at 379, 0.9983 at 744, and 0.9973 at 921 — it was never a ceiling, it was a metric at its maximum with no visible variance. R@1 read 0.605 → 0.687 → 0.747 → 0.7445; the first three rises invited reading a trend, and the full split settled fractionally *below* the third. **Both are now measured over the entire corpus, so they have stopped moving** — which is exactly what a baseline has to do before anything is compared against it.

**Judge Stage 5 against this row and no earlier one.** A fine-tune compared against the 379-question baseline would be credited with about 6 points of R@1 that are sampling noise, and would be measured against an R@20 of 1.000 that never existed.

---

## 1. Task definition

Given a natural-language question, retrieve the schema elements (tables and columns) needed to answer it.

- **Query** — the question text.
- **Document** — a serialized schema element: `"{table}.{column} ({type}) — {comment}. Examples: {v1}, {v2}, {v3}"`.
- **Positive** — an element that appears in the gold SQL for that question.
- **Metric** — Recall@k. A column that is never retrieved can never appear in correct SQL, which makes recall the ceiling on everything downstream.

Serialization is itself a variable, not a given — see [Ablations](#8-ablation-studies).

## 2. Dataset

Source: Spider and BIRD training splits. Full provenance, licensing, and split definitions in [DATASETS.md](DATASETS.md).

Pair construction:
1. Parse each gold SQL query with sqlglot.
2. Extract every referenced table and column, resolving aliases to real names.
3. Emit `(question, element)` positives — one row per referenced element.

> **TBD:** pair counts per split after cleaning.

## 3. Cleaning

Planned filters, each with the count it removed recorded:

| Filter | Rationale |
|---|---|
| Drop unparseable gold SQL | Cannot extract reliable positives |
| Drop `SELECT *` queries | Column-level supervision is absent |
| Resolve aliases before extraction | `t1.id` must map to the real table |
| Drop elements not in the schema catalog | Benchmark/schema mismatches exist and would train on nonexistent targets |
| Deduplicate identical `(question, element)` pairs | Skews loss toward common columns |
| **Enforce database-level split disjointness** | The load-bearing one — see below |

**Databases must not straddle splits.** Splitting by *question* leaks: the same schema elements appear in train and eval, so the model memorizes the corpus instead of learning to link. Split by **database**, so eval schemas are entirely unseen. This is the difference between a real generalization number and a meaningless one.

> **This rule was in force and violated anyway, and the audit that found it is the reason this note exists.** Splitting by database is necessary and not sufficient: the split is a hash of the database name ([ADR-021](../architecture/DECISIONS.md#adr-021--splits-are-a-hash-of-the-database-name-not-a-seeded-shuffle)), while the *reported* evaluation set is Spider's own `dev.json` — defined outside the hash and invisible to it. On 2026-08-11 that put **11 of Spider's 20 dev databases in this project's `train` band, carrying 605 questions**, and five more in `dev`/`smoke`: 16 of the 20 databases every published number is measured on ([ADR-047](../architecture/DECISIONS.md#adr-047--spiders-own-split-is-the-evaluation-boundary-and-the-training-set-is-carved-around-it), [DATASETS §5](DATASETS.md)). No fine-tune had run, so nothing published is affected.
>
> **The rule is now a function rather than a paragraph.** `assign(reserved=...)` forces the reported evaluation databases to held-out regardless of hash, and `benchmark.splits.leaked_databases()` is checked against the committed assignment by `tests/unit/test_split_disjointness.py`. **Before starting any fine-tune, run that test rather than re-reading this section** — a rule stated in prose is exactly what was in place when the leak happened.

## 4. Embedding model

**Baseline / starting checkpoint.** A general-purpose sentence-transformer bi-encoder, chosen for a good quality/size tradeoff at 384 dimensions.

**Why a bi-encoder.** Element embeddings are precomputed once and indexed in pgvector; only the question is embedded at query time. A cross-encoder would score more accurately but needs a forward pass per candidate — incompatible with the sub-100ms retrieval budget in [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md). A cross-encoder *reranker* over the top-50 is a plausible follow-up; it is in [../project/FUTURE.md](../project/FUTURE.md), not v1.

> **TBD:** exact checkpoint, parameter count, and the sweep that chose it.

## 5. Training pipeline

```
Spider/BIRD gold SQL
        │
        ▼
  sqlglot parse ──▶ extract tables + columns (aliases resolved)
        │
        ▼
  (question, element) positives
        │
        ▼
  serialize elements ──▶ InputExample pairs
        │
        ▼
  SentenceTransformer.fit
    · MultipleNegativesRankingLoss
    · in-batch negatives (+ mined hard negatives)
        │
        ▼
  eval on held-out DBs ──▶ Recall@1/5/10/20
        │
        ▼
  export checkpoint ──▶ re-embed corpus under new model_version
```

Runs are driven by a `typer` CLI so every result is reproducible from a recorded command line.

## 6. Hyperparameters

> **TBD — v2.0.** Starting points, to be swept:

| Parameter | Starting value | Notes |
|---|---|---|
| Epochs | 3 | Watch for overfitting on a small pair set |
| Batch size | 64 | Larger is better with in-batch negatives — every other example is a negative |
| Learning rate | 2e-5 | Standard for sentence-transformer fine-tuning |
| Warmup | 10% of steps | |
| Max sequence length | 128 | Serialized elements are short; longer wastes compute |
| Optimizer | AdamW | |
| Precision | fp16 if GPU, fp32 on CPU | |
| Seed | 42, plus 2 more | Report variance across seeds, not one lucky run |

**Batch size is not a neutral knob here.** With `MultipleNegativesRankingLoss`, batch size *is* the number of negatives per example. Changing it changes the task difficulty, so it must be held fixed when comparing anything else.

## 7. Loss function

**`MultipleNegativesRankingLoss`.** For each `(question, positive_element)` in a batch, every other example's positive acts as a negative. Efficient — no explicit negative mining needed to start — and matches the retrieval objective directly.

**Hard negatives.** In-batch negatives are mostly easy: a random column from an unrelated database is trivially distinguishable. The distinctions that actually matter are the near-misses — `orders.total_amount` vs `orders.subtotal_amount`, or a same-named column on a different table. Plan: mine hard negatives with the baseline model (high-scoring, incorrect elements), and treat mined-vs-in-batch-only as an ablation rather than assuming it helps.

**Considered:** `TripletLoss` (needs explicit triplets and careful margin tuning — no advantage here), `CosineSimilarityLoss` (needs graded similarity labels this data does not have).

## 8. Ablation studies

> **TBD — v2.0.** The comparisons that determine whether the ML work earned its place:

| # | Comparison | Question answered |
|---|---|---|
| A1 | Baseline vs fine-tuned, equal k | **The headline result.** Did fine-tuning help? |
| A2 | Fine-tuned @ k=5 vs baseline @ k=20 | Does fine-tuning beat simply retrieving more? (ADR-006's core claim) |
| A3 | In-batch negatives vs mined hard negatives | Was hard-negative mining worth the pipeline complexity? |
| A4 | Serialization: name only / +type / +comment / +sample values | How much does each field contribute? |
| A5 | Recall@k → downstream execution accuracy | Does better retrieval actually produce better SQL, or does generation absorb the difference? |

**A5 is the one that matters most and is easiest to skip.** Recall@k improving without execution accuracy improving would mean the retrieval bottleneck was not the real bottleneck — an uncomfortable result, and exactly the kind worth reporting honestly.

## 9. Evaluation protocol

Defined in [EVALUATION.md](EVALUATION.md). Rules enforced here:

- Held-out databases only. No question from a training database appears in eval.
- Baseline and fine-tuned models are evaluated by the same harness on the same split, in the same run.
- Every reported number carries seed, commit, split, and hardware in [BENCHMARKS.md](BENCHMARKS.md).
- The development split is used for iteration; the held-out split is touched once per reported result, not per experiment.

## 10. Model export

- Checkpoints saved via `SentenceTransformer.save()`, versioned as `schema-linker-v{N}`.
- The version string is written into `schema_elements.model_version` — vectors from different models are **not comparable**, and queries filter on it. Mixing them silently corrupts retrieval.
- Switching the active retriever is a config change plus a re-embedding data migration; both vector sets coexist until the new one is verified. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §8.
- Checkpoints are not committed to git. Storage location and retrieval instructions: **TBD — Stage 5**.

## 11. Reproducing a run

> **TBD — v2.0.** Exact command line, expected wall-clock, and hardware requirements. Written so the run can be reproduced from a clean checkout — a training result that cannot be re-run is not a result.
