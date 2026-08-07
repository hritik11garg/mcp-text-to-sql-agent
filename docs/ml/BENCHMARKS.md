# Benchmarks

**Append-only.** Rows are never edited or deleted, including regressions. The trajectory is the interesting part, and a number that quietly improved without an entry explaining why is not a result.

Metric definitions: [EVALUATION.md](EVALUATION.md). Every row must be reproducible from the recorded command.

> **A full-split run is in progress — 744 of 921 questions, 15 of 20 databases over two days, pausing on the daily token cap and resuming into the same directory (§1.1). The four rows before it are smoke rows over 3 of 20 databases.** §0 records what the data all of it is computed from is worth; §1 says exactly what each sample covers, and why two of the smoke rows may not be quoted as a model's score. §4–§7 are still the recording format.

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

> **Smoke rows, not benchmark results — and the distinction is the whole point of recording them.** Every row below covers **150 of the 921 scoreable questions**, and because the file is in database order those 150 are **3 of 20 databases** (`car_1`, `concert_singer`, `pets_1`). These are here because the *trajectory* between them is informative and two of the steps were defects worth 30 points. **They are superseded by §1.1** — and by more than sample size: `car_1` turns out to be the weakest database in the corpus at 55.4%, so these three were not a small random sample, they were weighted toward the hard case.

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

### 1.1 The full-split run, in progress

**Started 2026-08-06 into `results/spider-full-20260806`. It is not finished, and it is not abandoned — it is paused on a spent daily budget and resumes into the same directory.** That distinction is the whole difference from the 2026-08-05 attempt below, which could not be resumed at all. **Day 2 was run on 2026-08-07 and it resumed correctly**, which is the first end-to-end proof that the resumption fix works on a real directory rather than a fixture.

| | Day 1 | **Day 2 (current)** |
|---|---|---|
| Recorded | 379 of 921 scoreable | **744** of 921 |
| Scored | 367 | **732** |
| Matched | 297 | **596** |
| **Execution accuracy** | 80.9% | **81.4%** |
| Model | `openai/gpt-oss-120b`, `single_model: true` | same |
| Gold errors | 0 | **0** |
| Infrastructure | 12 | **12** — all `llm_failed`, all the daily cap |
| Databases reached | 9 of 20 | **15 of 20** |
| Remaining | 542 untouched | **177 untouched** |

**Still not a benchmark row**, for the reason every row above is not: it covers 15 of 20 databases. It is recorded because the trajectory matters and because things were verified by it that no test could.

**The resumption worked, in the log, on the real directory.** Day 2 opened with:

> `resuming results/spider-full-20260806: 367 question(s) answered, 12 to re-attempt after infrastructure failure`

That is [ADR-037](../architecture/DECISIONS.md#adr-037--resumption-skips-answered-questions-not-recorded-ones) doing exactly what it was written for. Day 1's 12 quota failures were re-attempted rather than retired as answers, and the run carried on from question 431.

**It halted the same way twice, which is the halt rule working rather than a coincidence.** Day 2's cap arrived after 377 more questions; ten consecutive `llm_failed` tripped `--halt-after` and the run stopped with **177 untouched** rather than recording them as failures. One more day finishes the split.

**Resuming required checking out the recorded commit.** The manifest names `15dcdf7`; the working tree had moved four commits past it, and **the fingerprint guard refused the resume** — correctly, since two of those commits touched code the harness runs through. The run was resumed from a `git worktree` at `15dcdf7` so that the remaining questions were answered by the same code as the first 379. That is the guard being obeyed rather than overridden, and it is the cost ADR-037 predicted arriving for the second time. See §1.3.

Per-database, where the spread is the finding:

| Database | Scored | Accuracy |
|---|---|---|
| **`poker_player`** | **40/40** | **100.0%** |
| `employee_hire_evaluation` | 33/34 | 97.1% |
| `battle_death` | 14/15 | 93.3% |
| `pets_1` | 39/42 | 92.9% |
| `museum_visit` | 10/11 | 90.9% |
| `cre_Doc_Template_Mgt` | 67/74 | 90.5% |
| `concert_singer` | 27/30 | 90.0% |
| `tvshow` | 52/59 | 88.1% |
| `voter_1` | 12/14 | 85.7% |
| `course_teach` | 22/26 | 84.6% |
| `wta_1` | 43/52 | 82.7% |
| `world_1` | 88/108 | 81.5% |
| `flight_2` | 61/76 | 80.3% |
| `student_transcripts_tracking` | 42/67 | 62.7% |
| **`car_1`** | **46/84** | **54.8%** |

**The spread widened at both ends, which is the argument against the single figure.** `poker_player` is a clean 40/40 and `car_1` is 54.8% — and `student_transcripts_tracking` arrived at 62.7% as a second hard database, so `car_1` is no longer a lone outlier that could be dismissed. A system described as "81.4%" is one that is somewhere between perfect and coin-flip depending on the schema, and **which schema you get is not something the accuracy figure tells you**.

`car_1` was one of the three databases every smoke row used, and it remains the worst of the fifteen. The smoke rows were not a small random sample; they were weighted toward the hardest database in the set.

**Recall, and the claim that did not survive:**

| | Day 1 (379 q) | **Day 2 (744 q)** |
|---|---|---|
| R@1 | 0.687 | **0.747** |
| R@5 | 0.936 | **0.943** |
| R@10 | 0.982 | **0.984** |
| R@20 | **1.000** | **0.998** |

**R@20 is no longer 1.000, and the argument built on it has to be weakened accordingly.** §2 used "R@20 held at 1.000 across four times the questions" as evidence that there is essentially no rank headroom on Spider for a fine-tune to recover. Over 744 questions it is 0.9983 — about one question in 600 where the right table is not in the top 20 at all. That is still very little headroom, and it is no longer *none*. The honest form of the claim is that a fine-tune's gain on Spider is bounded at roughly 5.7 points of R@5 and effectively nothing at R@20, which remains the argument for measuring on BIRD instead.

Common to this run, per the recording rules: Spider `dev.json`, digest `00636695…c85b121b`, conversion **verified** (§0, `579e312`, 19/20 databases) · **113 questions excluded** — 97 `dialect_error`, 16 `undetermined_limit` · metric **execution accuracy (single DB)**, *not* Spider's Test Suite Accuracy · commit `15dcdf7` · retriever `sentence-transformers/all-MiniLM-L6-v2` · prompt `sql_gen/v1` · seed 0 · Windows 11, CPU-only inference, PostgreSQL 16 in Docker · 361k input / 136k output tokens cumulative, 44m13s wall clock cumulative.

```powershell
$env:LLM_MODEL_FALLBACKS = ""     # single model, or the number is a blend
python -m evals.run --questions data/splits/spider-official-dev.jsonl --split dev `
    --gold data/splits/spider-dev-gold.jsonl --prefix spider_ `
    --baseline retrieval-only --top-k 30 `
    --run-id spider-full-20260806 --out results/
```

**Re-run that exact command to continue it** — from a checkout of the commit in the manifest, for the reason in §1.3. The same `--run-id` resumes: answered questions are skipped and the ones that failed on quota are re-attempted. After day 2 that means 732 skipped and 12 re-attempted, carrying on from question 757.

### 1.3 The fingerprint refused the resume, and was right to

Day 2 could not simply re-run the command. `RunManifest.config_fingerprint` includes the **commit**, the manifest records `15dcdf7`, and the working tree had reached `f74f9e3` — so `RunStore` refused to open the directory:

```
recorded  15dcdf7 -> e651ff75ca239eec
current   f74f9e3 -> a99c100e0254a905
```

**This is not a false positive.** Of the four commits in between, two are documentation, but `d2c146e` fixed defects in the resumption code itself and `ec4b23f` modified `src/answering/answerer.py` — both on the path the harness runs through. A run whose first half was answered by one version and second half by another is exactly the result the guard exists to prevent, and "the change probably didn't matter" is a claim about behaviour that nobody had measured.

**The resolution was to obey it, not to override it.** A detached `git worktree` at `15dcdf7`, with `--out` and the split files pointed back at the main checkout, so the fingerprint matched and the remaining questions were answered by the same code as the first 379.

**The structural problem this exposes is real and unfixed.** The free tier forces multi-day runs; multi-day runs mean either development stops for the duration or the fingerprint invalidates the next day's resume. Every day of work makes the next resume harder, and the guard cannot distinguish a documentation commit from a change to the answering path because a commit hash carries no such information. A fingerprint over the *code the harness actually loads* — a digest of `src/`, or of the modules on the answering path — would let docs commits pass and refuse the ones that matter. That is a change to what the fingerprint means and it is logged in [TASKS.md](../project/TASKS.md) rather than made mid-run, for the same reason as the defect below.

**Known defect in this run's manifest, found while filling in the row above.** `retriever_model_version` is recorded as the **empty string**, because `--retriever` defaults to empty and the command did not pass it. The retriever is `sentence-transformers/all-MiniLM-L6-v2` — read back from `agent_meta.schema_elements`, which is where it should have come from in the first place — so the row is correct, and the *manifest* is not.

That matters beyond bookkeeping: `retriever_model_version` is **in the configuration fingerprint**, the guard that refuses to resume a run whose configuration changed. Left to its default it is the same empty string for every retriever, so the one check standing between a baseline run and a fine-tuned one resuming into each other is inert unless an operator remembers a flag. It is the same shape as the two defects above it in the CHANGELOG: the check exists, its input is optional, nothing supplies it, and it passes silently.

**Deliberately not fixed yet.** Deriving it from the catalog changes the fingerprint, which orphans this run's answered questions — now **732** of them, two full days of free-tier budget. That is the exact cost recorded in ADR-037's tradeoffs: *the commit is in the fingerprint, so a multi-day run cannot absorb a fix*. This is the first multi-day run and the constraint arrived immediately, then arrived again on day 2 in the sharper form described in §1.3. The fix lands when the run completes; until then, **the retriever field on this row comes from the database, not the manifest.**

### 1.2 The attempt that produced no row

**A full-split run was attempted on 2026-08-05 and produced no row.** It is recorded here because a failed measurement attempt is evidence about the measurement setup, and discarding it would leave the impression that nothing had been tried.

| Attempted | Recorded | Answered | Infrastructure | Why no row |
|---|---|---|---|---|
| 1034 | 777 (stopped) | 395 | **382** — 308 `llm_failed`, 74 `scope_unavailable` | Two independent causes, one of them a defect |

- **308 `llm_failed`** — the free tier's daily token cap, with the fallback chain deliberately disabled so the run could not silently become a blend. The run was stopped rather than allowed to finish, because `resume()` skipped any question already recorded *including a failed one*, so every further failure would have permanently consumed a question this run could never retry. **That was itself a defect**, and the more expensive of the two: it meant a run could only ever be completed in a single sitting on a tier that cannot provide one. Fixed — infrastructure failures are now re-attempted on resume, and a run halts after ten consecutive ones rather than grinding through the remainder ([ADR-037](../architecture/DECISIONS.md#adr-037--resumption-skips-answered-questions-not-recorded-ones)).
- **74 `scope_unavailable`** — a real defect, found only because the run reached a database the 3-database sample never touches. See the CHANGELOG entry for `cre_Doc_Template_Mgt`. Fixed.

The accuracy over what was answered was **79.0% (312/395), single model** — and it is not a row, because 395 of 921 with one database missing entirely is a narrower sample than the 150-question smoke rows above, not a wider one.

**None of these are comparable to a published Spider number**, for three independent reasons: a 3-database sample, single-database execution accuracy rather than Test Suite Accuracy, and 113 excluded questions. The first is fixable by running more; the other two are stated on every row by design.

## 2. Schema-linking recall

> **Baseline established. Fine-tuned comparison is Stage 5.**

Measured from gold-SQL elements with aliases resolved. Recall is computed whether or not the model answered, so a generation failure does not remove a retrieval data point.

| Date | Commit | Split | Retriever | R@1 | R@5 | R@10 | R@20 | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-08-02 | `12cd3d5` | Spider `dev.json` (150) | `all-MiniLM-L6-v2` (baseline) | 0.605 | 0.889 | 0.960 | **1.000** | 0 unresolved references |
| 2026-08-06 | `15dcdf7` | Spider `dev.json` (379, 9 DBs) | `all-MiniLM-L6-v2` (baseline) | 0.687 | 0.936 | 0.982 | 1.000 | 2 unresolved references. Superseded by the row below |
| 2026-08-07 | `15dcdf7` | Spider `dev.json` (744, 15 DBs) | `all-MiniLM-L6-v2` (baseline) | 0.747 | **0.943** | 0.984 | **0.998** | 16 unresolved references. **This is the number the fine-tune must beat.** R@20 stopped being 1.000 as coverage doubled — the headroom is small, not absent |

**R@20 held at exactly 1.000 across both rows** — 150 questions over 3 databases and 379 over 9. A ceiling that survives a 2.5× increase in sample and a 3× increase in databases is a property of the corpus, not an artefact of a small sample, which is a materially stronger claim than the first row could make on its own.

**Recall@20 = 1.0 is why `k=30` was enough, and it also bounds what Stage 5 can buy on Spider.** A retriever that already finds every needed element by rank 20 cannot be improved into a higher execution accuracy here — only into finding them *sooner*, which matters for prompt cost and for schemas too large to show 30 elements of. That is the argument for BIRD, and [R-01](../project/RISKS.md) predicted exactly this shape of null result.

**The gap that does matter is R@1 = 0.687 against R@10 = 0.982.** Ranking, not coverage, is where this retriever is weak on Spider — and the gap narrowed with more data (0.605 → 0.687 at R@1) rather than widening, so the earlier sample was pessimistic about the baseline as well as about accuracy.

## 3. Invalid-query rate

> **Pre-correction only. Self-correction is Stage 4, and the gap between the two columns is what the retry loop will be worth.**

| Date | Commit | Split | Config | Invalid (pre) | Invalid (post) | Mean attempts | Notes |
|---|---|---|---|---|---|---|---|
| 2026-08-02 | `38f6457` | Spider `dev.json` (150) | `retrieval-only`, k=30 | **31 / 150 = 20.7%** | — | 1.0 | Before the `<think>` strip. 27 of the 31 were one model's reasoning submitted as SQL |
| 2026-08-02 | `12cd3d5` | Spider `dev.json` (150) | `retrieval-only`, k=30 | **4 / 150 = 2.7%** | — | 1.0 | After. No validation tier in this baseline — these reached the database and were refused |
| 2026-08-06 | `15dcdf7` | Spider `dev.json` (367 scored, 9 DBs) | `retrieval-only`, k=30 | **12 / 367 = 3.3%** | — | 1.0 | Same code path, wider corpus. The rise over 2.7% is sample, not regression — and it is the honest baseline for the Stage 4 comparison |

**20.7% → 2.7% is a client-side parsing fix, not a model or prompt change.** Worth separating, because an invalid-query rate is normally read as a statement about the model.

**2.7% → 3.3% is not a regression.** It is the same code measured over 367 questions and 9 databases instead of 150 and 3. Recorded rather than quietly kept at the lower figure, because the number Stage 4 must improve on has to come from the widest sample available — and quoting the more flattering of two measurements of the same code is how a self-correction loop comes to look better than it is.

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
