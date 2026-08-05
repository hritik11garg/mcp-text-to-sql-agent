# Benchmarks

**Append-only.** Rows are never edited or deleted, including regressions. The trajectory is the interesting part, and a number that quietly improved without an entry explaining why is not a result.

Metric definitions: [EVALUATION.md](EVALUATION.md). Every row must be reproducible from the recorded command.

> **First accuracy runs recorded — and they are smoke rows over 3 of 20 databases, not a benchmark result.** §0 records what the data those numbers are computed from is worth; §1 says exactly what its own sample covers, and why two of its four rows may not be quoted as a model's score. §4–§7 are still the recording format.

---

## Recording rules

Each row records:

| Field | Why |
|---|---|
| Date | Ordering |
| Commit | The only way to reproduce it. **The commit that contains the code that ran** — which is not always what the run's own manifest says, see below |
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

**On the commit field, and why the rows here differ from the manifests.** `manifest.json` records `git rev-parse HEAD` at the moment the run started. Every run in §1 was made from a working tree whose fixes were not yet committed, so each manifest names the *parent* of the commit that reproduces it — `063502d` where the table says `38f6457`, and `38f6457` where it says `12cd3d5`. The table is the useful attribution: a reader wanting to reproduce a number needs the commit containing the code, not the commit it was written on top of.

Runs from this point forward mark it themselves — `current_commit()` appends `-dirty` when the tree has uncommitted changes, and `-unverified` when the check could not run. A bare hash is now a positive claim that the tree was clean, which is what it always read as and never guaranteed.

---

## 0. Conversion fidelity

**Every row in §1 inherits this one.** Execution accuracy measured on a converted database is bounded above by how faithfully that database was converted, and a system score reported without this number is unattributable — a drop could be the model, the retriever, or the data.

Fidelity is `(match + ambiguous_order) / (questions − gold_error − transpile_error − dialect_error)`. Definitions in [DATASETS.md](DATASETS.md) §3.1.

| Date | Commit | Dataset | Databases | Questions | Fidelity | Excluded | Verified | Command |
|---|---|---|---|---|---|---|---|---|
| 2026-08-02 | `5551fb5` | Spider `dev.json`, digest `00636695…c85b121b` | 20 / 20 converted | 1034 | **912 / 937 = 97.3%** — 896 match, 16 ambiguous order, 25 mismatch, 0 postgres error | 97 dialect error (56 `GROUP BY`, 41 type affinity) | **10 / 20 databases** | `python -m benchmark.load verify --databases data/spider/spider_data/database --questions data/spider/spider_data/dev.json --benchmark spider --prefix spider_` |
| 2026-08-02 | `579e312` | Spider `dev.json`, digest `00636695…c85b121b` | 20 / 20 converted | 1034 | **915 / 921 = 99.3%** — 899 match, 16 ambiguous order, 6 mismatch, 0 postgres error | 97 dialect error · 16 undetermined limit | **19 / 20 databases** | *as above* |

**The two rows differ by diagnosis, not by conversion.** Nothing about the data changed between them. 3 of the 25 mismatches were SQLite's case-insensitive `LIKE`, a transpilation gap; 16 were `LIMIT` cutting a tie, which is a question with no single correct answer and now leaves the denominator. Both rows stay, per the append-only rule — a number that improved because the measurement got more accurate is exactly the kind of change worth being able to see.

**Residual, and it is not going away:** all 6 remaining mismatches are `wta_1.players.birth_date`, a column holding 20,144 integers and 518 empty strings. No static type is faithful to that, so an accuracy number over `wta_1` carries a known, bounded conversion difference on one column. The other 19 databases carry none.

## 1. Execution accuracy

> **Smoke rows, not benchmark results — and the distinction is the whole point of recording them.** Every row below covers **150 of the 921 scoreable questions**, and because the file is in database order those 150 are **3 of 20 databases** (`car_1`, `concert_singer`, `pets_1`). A full-split number does not exist yet. These are here because the *trajectory* between them is informative and two of the steps were defects worth 30 points.

Common to every row: Spider `dev.json`, digest `00636695…c85b121b`, conversion **verified** (§0, `579e312`, 19/20 databases) · **113 questions excluded** — 97 `dialect_error`, 16 `undetermined_limit` · metric **execution accuracy (single DB)**, *not* Spider's Test Suite Accuracy · retriever `sentence-transformers/all-MiniLM-L6-v2` · prompt `sql_gen/v1` · seed 0 · Windows 11, CPU-only inference, PostgreSQL 16 in Docker.

| Date | Commit | Split | Sample | Baseline | `k` | LLM (answered / matched) | Exec. acc. | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-08-02 | `38f6457` | Spider `dev.json` | 150 / 921 | `retrieval-only` | 10 | `openai/gpt-oss-120b` 75 / — | **42.7%** | **75 of 150 returned `CANNOT_ANSWER`.** Not a generation failure — Recall@10 was 0.94, and one missing element is enough for an honest refusal |
| 2026-08-02 | `38f6457` | Spider `dev.json` | 150 / 921 | `retrieval-only` | 30 | `openai/gpt-oss-120b` 150 / 109 | **72.7%** | Same code, same questions. The 30-point gain is the retrieval budget and nothing else |
| 2026-08-02 | `38f6457` | Spider `dev.json` | 150 / 921 | `retrieval-only` | 30 | `gpt-oss-120b` 123 / 97 · `qwen3.6-27b` 27 / 0 | **64.7%** ⚠️ | **A blend, and not a score for either model.** The primary hit its daily cap and the chain fell back to a model scoring 0% — see below |
| 2026-08-02 | `12cd3d5` | Spider `dev.json` | 150 / 921 | `retrieval-only` | 30 | `qwen3.6-27b` 68 / 65 · `llama-3.3-70b` 82 / 48 | **75.3%** ⚠️ | Blend again, and a wider one: **96%** and **59%**. `execution_failed` 31 → 4 |

```powershell
python -m evals.run --questions <spider dev.json as JSONL> --split dev `
    --gold data/splits/spider-dev-gold.jsonl --prefix spider_ `
    --baseline retrieval-only --top-k 30 --limit 150 --out results/
```

**Row 1 → 2 is the finding worth keeping.** `RETRIEVAL_TOP_K` defaults to 10, which is tuned for a large schema; a Spider database holds 10–67 catalog elements total, so `k=10` shows the model a partial schema and it correctly refuses rather than guessing. That refusal being a *distinct outcome* — `unanswerable`, separate from a malformed answer — is what pointed at retrieval instead of the prompt. Recall@20 is 1.0, which is why 30 is enough.

**Row 3 → 4 is a bug, not a model improvement.** Several open-weight models emit their reasoning in the `content` field and the answer after `</think>`; the whole monologue was being submitted as a query. `qwen3.6-27b` went from **0% to 96%** once it was stripped. It was invisible for as long as the configured model answered every question, which is precisely how a fallback chain hides a defect it was added to prevent.

**⚠️ marks a run more than one model answered.** The free tier's daily cap moves the chain mid-run, so a single accuracy figure is a weighted average of two systems — 96% and 59% in row 4. The summary carries `answered_by` and `single_model` for this reason, and **no row marked ⚠️ may be quoted as a model's score**.

**A full-split run was attempted on 2026-08-05 and produced no row.** It is recorded here because a failed measurement attempt is evidence about the measurement setup, and discarding it would leave the impression that nothing had been tried.

| Attempted | Recorded | Answered | Infrastructure | Why no row |
|---|---|---|---|---|
| 1034 | 777 (stopped) | 395 | **382** — 308 `llm_failed`, 74 `scope_unavailable` | Two independent causes, one of them a defect |

- **308 `llm_failed`** — the free tier's daily token cap, with the fallback chain deliberately disabled so the run could not silently become a blend. The run was stopped rather than allowed to finish, because `resume()` skips any question already recorded *including a failed one*, so every further failure would have permanently consumed a question this run could never retry.
- **74 `scope_unavailable`** — a real defect, found only because the run reached a database the 3-database sample never touches. See the CHANGELOG entry for `cre_Doc_Template_Mgt`. Fixed.

The accuracy over what was answered was **79.0% (312/395), single model** — and it is not a row, because 395 of 921 with one database missing entirely is a narrower sample than the 150-question smoke rows above, not a wider one.

**None of these are comparable to a published Spider number**, for three independent reasons: a 3-database sample, single-database execution accuracy rather than Test Suite Accuracy, and 113 excluded questions. The first is fixable by running more; the other two are stated on every row by design.

## 2. Schema-linking recall

> **Baseline established. Fine-tuned comparison is Stage 5.**

Measured over the same 150 questions, from gold-SQL elements with aliases resolved. Recall is computed whether or not the model answered, so a generation failure does not remove a retrieval data point.

| Date | Commit | Split | Retriever | R@1 | R@5 | R@10 | R@20 | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-08-02 | `12cd3d5` | Spider `dev.json` (150) | `all-MiniLM-L6-v2` (baseline) | 0.605 | 0.889 | 0.960 | **1.000** | 0 unresolved references. This is the number the fine-tune must beat |

**Recall@20 = 1.0 is why `k=30` was enough, and it also bounds what Stage 5 can buy on Spider.** A retriever that already finds every needed element by rank 20 cannot be improved into a higher execution accuracy here — only into finding them *sooner*, which matters for prompt cost and for schemas too large to show 30 elements of. That is the argument for BIRD, and [R-01](../project/RISKS.md) predicted exactly this shape of null result.

**The gap that does matter is R@1 = 0.605 against R@10 = 0.960.** Ranking, not coverage, is where this retriever is weak on Spider.

## 3. Invalid-query rate

> **Pre-correction only. Self-correction is Stage 4, and the gap between the two columns is what the retry loop will be worth.**

| Date | Commit | Split | Config | Invalid (pre) | Invalid (post) | Mean attempts | Notes |
|---|---|---|---|---|---|---|---|
| 2026-08-02 | `38f6457` | Spider `dev.json` (150) | `retrieval-only`, k=30 | **31 / 150 = 20.7%** | — | 1.0 | Before the `<think>` strip. 27 of the 31 were one model's reasoning submitted as SQL |
| 2026-08-02 | `12cd3d5` | Spider `dev.json` (150) | `retrieval-only`, k=30 | **4 / 150 = 2.7%** | — | 1.0 | After. No validation tier in this baseline — these reached the database and were refused |

**20.7% → 2.7% is a client-side parsing fix, not a model or prompt change.** Worth separating, because an invalid-query rate is normally read as a statement about the model.

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
