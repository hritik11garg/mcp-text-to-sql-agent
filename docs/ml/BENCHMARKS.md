# Benchmarks

**Append-only.** Rows are never edited or deleted, including regressions. The trajectory is the interesting part, and a number that quietly improved without an entry explaining why is not a result.

Metric definitions: [EVALUATION.md](EVALUATION.md). Every row must be reproducible from the recorded command.

> **The full-split run is complete — all 921 scoreable questions, all 20 databases, `openai/gpt-oss-120b` alone, at 79.9% execution accuracy (§1.1).** It took three days and three daily token budgets, resuming into the same directory each time. The four rows before it are smoke rows over 3 of 20 databases and are superseded by it. §0 records what the underlying data is worth; §1 says exactly what each sample covers, and why two of the smoke rows may not be quoted as a model's score. §2, §3, §5 and §6 now carry full-split rows. §4 and §7 are still the recording format.
>
> **The headline is 79.9%, and the number under it is the 45-point spread**: 100% on `poker_player`, 54.8% on `car_1`. Read §1.1's per-database table before quoting the average anywhere.

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

> **Smoke rows, not benchmark results — and the distinction is the whole point of recording them.** Every row below covers **150 of the 921 scoreable questions**, and because the file is in database order those 150 are **3 of 20 databases** (`car_1`, `concert_singer`, `pets_1`). These are here because the *trajectory* between them is informative and two of the steps were defects worth 30 points. **They are superseded by §1.1** — and by more than sample size: `car_1` turns out to be the weakest database in the corpus at 54.8%, so these three were not a small random sample, they were weighted toward the hard case.

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

**Row 1 → 2 is the finding worth keeping.** `RETRIEVAL_TOP_K` defaults to 10, which is tuned for a large schema; a Spider database holds 10–67 catalog elements total, so `k=10` shows the model a partial schema and it correctly refuses rather than guessing. That refusal being a *distinct outcome* — `unanswerable`, separate from a malformed answer — is what pointed at retrieval instead of the prompt. Recall@20 over that sample was 1.0, which is why 30 is enough — **0.9973 over the complete split**, which does not change the conclusion.

**Row 3 → 4 is a bug, not a model improvement.** Several open-weight models emit their reasoning in the `content` field and the answer after `</think>`; the whole monologue was being submitted as a query. `qwen3.6-27b` went from **0% to 96%** once it was stripped. It was invisible for as long as the configured model answered every question, which is precisely how a fallback chain hides a defect it was added to prevent.

**⚠️ marks a run more than one model answered.** The free tier's daily cap moves the chain mid-run, so a single accuracy figure is a weighted average of two systems — 96% and 59% in row 4. The summary carries `answered_by` and `single_model` for this reason, and **no row marked ⚠️ may be quoted as a model's score**.
### 1.1 The full-split run — complete

**Finished 2026-08-08 in `results/spider-full-20260806`, over three days and three daily token budgets. All 921 scoreable questions answered, all 20 databases reached, zero infrastructure errors and zero gold errors in the final run.** This is the first row on this page that is a benchmark row rather than a smoke row, and the first number here that may be quoted without a caveat about coverage.

| | Day 1 (08-06) | Day 2 (08-07) | **Day 3 (08-08) — final** |
|---|---|---|---|
| Recorded | 379 of 921 | 744 of 921 | **921 of 921** |
| Scored | 367 | 732 | **921** |
| Matched | 297 | 596 | **736** |
| **Execution accuracy** | 80.9% | 81.4% | **79.9%** |
| Gold errors | 0 | 0 | **0** |
| Infrastructure errors | 12 | 12 | **0** |
| Databases reached | 9 of 20 | 15 of 20 | **20 of 20** |
| Remaining | 542 | 177 | **none** |

Model `openai/gpt-oss-120b` throughout, `single_model: true` on every day — no row here is a blend.

**The accuracy went down when the run finished, and that is the point of finishing it.** 81.4% at 744 questions became **79.9%** at 921. The last 177 questions were not a random remainder; they were whatever the alphabetical walk had not reached, and they included `dog_kennels` at 62.5% and the rest of `world_1`. A partial run is a biased sample of its own corpus, and the direction of the bias is unknowable until it completes — which is the argument for not quoting partial numbers, and the reason the earlier rows on this page carry the coverage they were measured at.

**Infrastructure errors went to zero, and that is the resumption feature finishing its job.** Days 1 and 2 each ended with 12 questions marked `llm_failed` against a spent quota. Day 3 re-attempted them rather than retiring them as answers — [ADR-037](../architecture/DECISIONS.md#adr-037--resumption-skips-answered-questions-not-recorded-ones) — and all 12 succeeded. The day-3 log opens:

> `resuming ...\results\spider-full-20260806: 732 question(s) answered, 12 to re-attempt after infrastructure failure`

So the final denominator is the whole corpus with nothing excluded for a reason that was really about a rate limit. **Scored equals total for the first time.**

Per-database, where the spread is the finding:

| Database | Scored | Accuracy |
|---|---|---|
| **`poker_player`** | **40/40** | **100.0%** |
| `employee_hire_evaluation` | 33/34 | 97.1% |
| `battle_death` | 14/15 | 93.3% |
| `singer` | 28/30 | 93.3% |
| `pets_1` | 39/42 | 92.9% |
| `museum_visit` | 10/11 | 90.9% |
| `cre_Doc_Template_Mgt` | 67/74 | 90.5% |
| `concert_singer` | 27/30 | 90.0% |
| `tvshow` | 53/60 | 88.3% |
| `orchestra` | 30/34 | 88.2% |
| `voter_1` | 12/14 | 85.7% |
| `course_teach` | 22/26 | 84.6% |
| `wta_1` | 43/52 | 82.7% |
| `flight_2` | 61/76 | 80.3% |
| `world_1` | 91/116 | 78.4% |
| `network_1` | 31/40 | 77.5% |
| `real_estate_properties` | 2/3 | 66.7% |
| `dog_kennels` | 45/72 | 62.5% |
| `student_transcripts_tracking` | 42/68 | 61.8% |
| **`car_1`** | **46/84** | **54.8%** |

**The single figure is the least useful number on this page.** The system is 100% on `poker_player` and 54.8% on `car_1` — a 45-point spread across schemas in the same corpus, measured by the same code on the same day. "79.9%" describes no database in the set particularly well, and **which schema a user brings is not something the average tells them.** Three databases now sit below 63%, so `car_1` is not an outlier to be explained away; the hard end has a population.

`car_1` was one of the three databases every smoke row on this page used. Those rows were not a small random sample — they were weighted toward the hardest database in the set, which is worth remembering when reading the 42.7% → 72.7% jump above.

**Where the 185 non-matches went:**

| Outcome | Count | What it means |
|---|---|---|
| `wrong_shape` | 102 | Ran, returned a different column set or arity than gold |
| `unanswerable` | 37 | The model refused rather than guessed |
| `wrong_values` | 32 | Right shape, different rows |
| `execution_failed` | 13 | Valid-looking SQL PostgreSQL rejected |
| `row_order` | 1 | Ordering only |
| `gold_error` | 0 | — |
| `infrastructure` | 0 | — |

**`retrieval_miss`, `unknown_identifier`, `syntax_unrecoverable`, `not_read_only` and `timeout` are all zero over 921 questions.** Nothing was refused by the read-only role and nothing hit the statement timeout — the sandbox never had to stop anything, which is a weaker statement than it looks: Spider's questions are not adversarial, so this measures that the limits do not fire on ordinary work, not that they hold under attack. `tests/security/` is what measures the latter.

**Recall, and the claim that kept eroding:**

| | Day 1 (379 q) | Day 2 (744 q) | **Final (921 q)** |
|---|---|---|---|
| R@1 | 0.687 | 0.747 | **0.7445** |
| R@5 | 0.936 | 0.943 | **0.9435** |
| R@10 | 0.982 | 0.984 | **0.9828** |
| R@20 | **1.000** | 0.9983 | **0.9973** |

**R@20 fell at every increase in sample size, and finished at 0.9973.** It read 1.000 over 150 questions and again over 379 — and §2 built an argument on that ceiling. Over 921 it is about one question in 370 where the right table is not in the top 20 at all. The lesson is recorded in §2 in its original wrong form on purpose: **a metric at its maximum has no visible variance**, so two samples agreeing at 1.000 look exactly like two samples that are both too small.

22 gold references could not be resolved to catalog elements and are counted rather than dropped.

Common to this run, per the recording rules: Spider `dev.json`, digest `00636695…c85b121b`, conversion **verified** (§0, `579e312`, 19/20 databases) · **113 questions excluded** from the 1034 in the split — 97 `dialect_error`, 16 `undetermined_limit` · metric **execution accuracy (single DB)**, *not* Spider's Test Suite Accuracy · commit `15dcdf7` · retriever `sentence-transformers/all-MiniLM-L6-v2` · prompt `sql_gen/v1` · seed 0 · Windows 11, CPU-only inference, PostgreSQL 16 in Docker · **455k input / 169k output tokens, 57m07s wall clock**, cumulative across three days.

```powershell
$env:LLM_MODEL_FALLBACKS = ""     # single model, or the number is a blend
python -m evals.run --questions data/splits/spider-official-dev.jsonl --split dev `
    --gold data/splits/spider-dev-gold.jsonl --prefix spider_ `
    --baseline retrieval-only --top-k 30 `
    --run-id spider-full-20260806 --out results/
```

**Reproducing it needs a checkout of `15dcdf7`**, for the reason in §1.3 — the configuration fingerprint includes the commit, and the working tree is well past it. Each of days 2 and 3 was run from a detached `git worktree` at that commit so that all 921 questions were answered by one version of the code.

**What this run does not measure.** The `with-validation` baseline, which is the one that would show whether the self-correction loop recovers any of the 13 `execution_failed` cases — `retrieval-only` runs no validator, so `validation_attempts` is 0 on all 921 artifacts by construction rather than because nothing needed correcting. And **nothing here goes through the MCP servers**: this measures the direct answering path, the same one the HTTP API uses. The servers are proven to work by `tests/contract/`, and proven to answer *as well* by nothing. See [ROADMAP](../project/ROADMAP.md) §3.

### 1.3 The fingerprint refused the resume, twice, and was right both times

Neither day 2 nor day 3 could simply re-run the command. `RunManifest.config_fingerprint` includes the **commit**, the manifest records `15dcdf7`, and the working tree had reached `f74f9e3` by day 2 and `81ea97f` by day 3 — so `RunStore` refused to open the directory:

```
recorded  15dcdf7 -> e651ff75ca239eec
current   f74f9e3 -> a99c100e0254a905
```

**This is not a false positive.** Of the four commits in between, two are documentation, but `d2c146e` fixed defects in the resumption code itself and `ec4b23f` modified `src/answering/answerer.py` — both on the path the harness runs through. A run whose first half was answered by one version and second half by another is exactly the result the guard exists to prevent, and "the change probably didn't matter" is a claim about behaviour that nobody had measured.

**The resolution was to obey it, not to override it.** A detached `git worktree` at `15dcdf7`, with `--out` and the split files pointed back at the main checkout, so the fingerprint matched and the remaining questions were answered by the same code as the first 379. Day 3 did the same from a further four commits away.

**A worktree is not automatically enough, and day 3 is where that surfaced.** The virtual environment installs this project in editable mode, which puts an absolute path to the *main* checkout's `src/` on `sys.path`. Run the harness from a worktree with that interpreter and `git rev-parse` reports the worktree's commit — so the fingerprint matches — while the code actually imported comes from the tree you were trying not to use. The guard would have passed on a run it exists to refuse. `PYTHONPATH=src` with the worktree as the working directory is what makes the two agree, because `PYTHONPATH` is consulted before site-packages. Day 3 verified it explicitly before starting, by printing the resolved module path alongside the computed fingerprint:

```
evals loaded from : ...\wt-15dcdf7\src\evals
current_commit    : 15dcdf7
recorded fp       : e651ff75ca239eec
current  fp       : e651ff75ca239eec
MATCH
```

**This is a gap in the guard, not in the procedure.** The fingerprint is derived from the repository the process is *standing in*, and nothing checks that it is the repository the process is *importing from*. A digest over the loaded modules — the change already logged below — would close both this and the docs-commit problem at once, because it would be computed from the code rather than from the directory.

**The structural problem this exposes is real and unfixed.** The free tier forces multi-day runs; multi-day runs mean either development stops for the duration or the fingerprint invalidates the next day's resume. Every day of work makes the next resume harder, and the guard cannot distinguish a documentation commit from a change to the answering path because a commit hash carries no such information. A fingerprint over the *code the harness actually loads* — a digest of `src/`, or of the modules on the answering path — would let docs commits pass and refuse the ones that matter. That is a change to what the fingerprint means and it is logged in [TASKS.md](../project/TASKS.md) rather than made mid-run, for the same reason as the defect below.

**Known defect in this run's manifest, found while filling in the row above.** `retriever_model_version` is recorded as the **empty string**, because `--retriever` defaults to empty and the command did not pass it. The retriever is `sentence-transformers/all-MiniLM-L6-v2` — read back from `agent_meta.schema_elements`, which is where it should have come from in the first place — so the row is correct, and the *manifest* is not.

That matters beyond bookkeeping: `retriever_model_version` is **in the configuration fingerprint**, the guard that refuses to resume a run whose configuration changed. Left to its default it is the same empty string for every retriever, so the one check standing between a baseline run and a fine-tuned one resuming into each other is inert unless an operator remembers a flag. It is the same shape as the two defects above it in the CHANGELOG: the check exists, its input is optional, nothing supplies it, and it passes silently.

**It was deliberately not fixed during the run.** Deriving it from the catalog changes the fingerprint, which would have orphaned every question already answered — 732 of them by day 3, two full days of free-tier budget. That is the exact cost recorded in ADR-037's tradeoffs: *the commit is in the fingerprint, so a multi-day run cannot absorb a fix*. This was the first multi-day run and the constraint arrived immediately, then arrived again on each subsequent day.

**The run is now complete, so the block is gone.** Both changes — deriving `retriever_model_version` from the catalog, and fingerprinting the loaded modules rather than the commit — are unblocked as of 2026-08-08 and tracked in [TASKS.md](../project/TASKS.md). Until they land, **the retriever field on this row comes from the database, not the manifest.** The row is correct; the manifest is not, and it says so.

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
| 2026-08-07 | `15dcdf7` | Spider `dev.json` (744, 15 DBs) | `all-MiniLM-L6-v2` (baseline) | 0.747 | 0.943 | 0.984 | 0.998 | 16 unresolved references. Superseded by the row below |
| **2026-08-08** | `15dcdf7` | **Spider `dev.json` (921, all 20 DBs)** | `all-MiniLM-L6-v2` (baseline) | **0.7445** | **0.9435** | **0.9828** | **0.9973** | 22 unresolved references. **This is the number the fine-tune must beat**, and the first measured over the whole split |

**R@20 fell at every increase in sample size: 1.000, 1.000, 0.9983, 0.9973.** 150 questions over 3 databases, 379 over 9, 744 over 15, 921 over 20. The previous version of this section drew the obvious conclusion from the first two, and it is kept here as written because it is instructive:

> *"A ceiling that survives a 2.5× increase in sample and a 3× increase in databases is a property of the corpus, not an artefact of a small sample."*

**The reasoning was sound and the conclusion was wrong**, which is a distinction worth being able to make. Two agreeing samples are evidence; they are not proof of a property that a third sample can falsify, and the third and fourth both did. The failure mode is specific: a metric at its maximum has **no visible variance**, so repeated agreement is exactly what a small-sample artefact looks like too. A ceiling can only be confirmed by the coverage that would break it — and here every increase in coverage moved it down, which is the signature of a value that was never a ceiling.

**Recall@20 = 0.9973 is still why `k=30` is enough, and it still bounds what Stage 5 can buy on Spider — with the bound stated as a number rather than as "cannot".** About one question in 370 has no correct element in the top 20, so a fine-tune has **0.27 points** to recover there and **5.65 at R@5**. A retriever that already finds nearly every needed element by rank 20 can mostly only be improved into finding them *sooner*, which matters for prompt cost and for schemas too large to show 30 elements of. That is the argument for BIRD, and [R-01](../project/RISKS.md) predicted exactly this shape of null result.

**The gap that does matter is R@1 = 0.7445 against R@10 = 0.9828.** Ranking, not coverage, is where this retriever is weak on Spider. **R@1 is where the ~24 points of headroom are**, and it is the metric Stage 5 should be judged on.

**R@1 rose with sample size and then stopped: 0.605 → 0.687 → 0.747 → 0.7445.** The first three readings all moved up, which invited reading the trend as "each earlier sample was pessimistic." The full split settled fractionally *below* day 2. The honest description is that R@1 converged somewhere around 0.74 and the earlier climb was sampling noise resolving, not a trend — which is the same mistake as the R@20 ceiling in a different direction, and the reason ablation A2 must be judged against this row and no earlier one.

## 3. Invalid-query rate

> **Pre-correction only. Self-correction is Stage 4, and the gap between the two columns is what the retry loop will be worth.**

| Date | Commit | Split | Config | Invalid (pre) | Invalid (post) | Mean attempts | Notes |
|---|---|---|---|---|---|---|---|
| 2026-08-02 | `38f6457` | Spider `dev.json` (150) | `retrieval-only`, k=30 | **31 / 150 = 20.7%** | — | 1.0 | Before the `<think>` strip. 27 of the 31 were one model's reasoning submitted as SQL |
| 2026-08-02 | `12cd3d5` | Spider `dev.json` (150) | `retrieval-only`, k=30 | **4 / 150 = 2.7%** | — | 1.0 | After. No validation tier in this baseline — these reached the database and were refused |
| 2026-08-06 | `15dcdf7` | Spider `dev.json` (367 scored, 9 DBs) | `retrieval-only`, k=30 | **12 / 367 = 3.3%** | — | 1.0 | Same code path, wider corpus. Superseded by the row below |
| **2026-08-08** | `15dcdf7` | **Spider `dev.json` (921 scored, all 20 DBs)** | `retrieval-only`, k=30 | **13 / 921 = 1.4%** | — | 1.0 | **The baseline Stage 4 must improve on.** Whole split, single model |

**20.7% → 2.7% is a client-side parsing fix, not a model or prompt change.** Worth separating, because an invalid-query rate is normally read as a statement about the model.

**3.3% → 1.4% is a sampling correction in the flattering direction, and it is stated as such.** The absolute count barely moved — 12 invalid queries at 367 scored, 13 at 921 — so almost all of the improvement is denominator. The 9 databases reached first happened to contain nearly every query PostgreSQL would reject, and the other 11 contributed one more between them. **The rate is genuinely 1.4% over the whole split**, and it would be dishonest to present that as the retry loop's problem getting smaller; nothing changed except how much of the corpus was measured.

**Mean attempts is 1.0 on all 921 artifacts because this baseline runs no validator at all.** `retrieval-only` has no validation tier, so `validation_attempts` is 0 by construction — not because nothing needed correcting. The 13 failures here reached the database and were refused by PostgreSQL. **The post-correction column stays empty until a `with-validation` run over the same split exists**, and until then no claim about self-correction on this corpus has any evidence behind it.

## 4. Multi-step task success

> **TBD — Stage 4.** Grading method must be recorded per row — rubric-automatic vs human changes what the number means.

| Date | Commit | Split | Tasks | Success | Grading | Notes |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | *No runs yet* |

## 5. Latency

> **One row, and it is not the Stage 6 measurement.** Targets in [../operations/PERFORMANCE.md](../operations/PERFORMANCE.md).

| Date | Commit | Component | p50 | p95 | p99 | Hardware | Notes |
|---|---|---|---|---|---|---|---|
| 2026-08-08 | `15dcdf7` | Whole answer path, per question (n=921) | **3.09 s** | **7.62 s** | **14.97 s** | Windows 11, CPU-only inference, PostgreSQL 16 in Docker | Free-tier provider over the public internet. Min 0.50 s, max 71.4 s, mean 3.72 s |

**This is a measurement of a free tier, not of this system.** The dominant term is a remote provider's queue, and it is shared, rate-limited and outside this repository's control. The 71-second maximum and the gap between p95 and p99 are what provider throttling looks like from the client side; they say nothing about retrieval, validation or execution, all of which are milliseconds by comparison (§1.1's `execute` stage runs 10–26 ms).

**So it does not satisfy Stage 6 and must not be quoted as a latency budget.** A real row needs the components separated, a fixed local model or a paid tier with predictable queueing, and a stated concurrency. It is recorded because 921 samples of end-to-end latency under the conditions this project actually runs in is worth more than an empty table — and because it sets the honest expectation for anyone running the demo: **a question takes about three seconds, and sometimes it takes a minute.**

## 6. Cost

| Date | Commit | LLM | Effort | Input tok/q | Output tok/q | USD/q | Exec. acc. | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-08-08 | `15dcdf7` | `openai/gpt-oss-120b` | default | **494** | **183** | **$0.00** | **79.9%** | Whole split, n=921. 455k in / 169k out total, free tier |

**$0.00 is a real number here and it is the point of the constraint in [PROJECT.md](../../PROJECT.md).** The entire 921-question benchmark cost nothing but three days of daily quota. That is what makes the result reproducible by someone with no budget, which was the requirement the provider-agnostic port ([ADR-014](../architecture/DECISIONS.md#adr-014--provider-agnostic-llm-behind-an-llmclient-port)) exists to satisfy.

**It is also why the cost column cannot yet do its job.** This table exists so an accuracy gain can be read against what it cost — an improvement at 4× the tokens is a different result from a free one. With one free row there is nothing to compare against, so the useful figure for now is the **token** count, not the dollar count: 677 tokens per question end to end is the budget any future prompt change is spending against. `input_tokens` here is the retrieved schema plus the question; the 30-element retrieval budget (§1.1) is most of it.

## 7. Ablations

> **TBD — Stage 5.** Design in [TRAINING.md](TRAINING.md) §8.

| ID | Comparison | Result | Verdict |
|---|---|---|---|
| A1 | Baseline vs fine-tuned @ equal k | TBD | — |
| A2 | Fine-tuned @ k=5 vs baseline @ k=20 | TBD | **Near-unwinnable on Spider by construction** — baseline R@20 is 0.9973, so the target leaves 0.27 points of headroom. Run it on BIRD or expect a null result |
| A3 | In-batch vs mined hard negatives | TBD | — |
| A4 | Serialization field contribution | TBD | — |
| A5 | Recall@k → execution accuracy | TBD | — |

---

## Regression log

Regressions get their own entries. A change that made something worse is a finding, and hiding it makes every other number here less trustworthy.

| Date | Commit | Metric | From | To | Cause | Resolution |
|---|---|---|---|---|---|---|
| 2026-08-08 | `15dcdf7` | Execution accuracy | 81.4% (744 q) | **79.9%** (921 q) | **Sample, not code.** Identical commit, identical model. The final 177 questions included `dog_kennels` at 62.5% | None needed. The partial figure is superseded and must not be quoted |
| 2026-08-08 | `15dcdf7` | Recall@20 | 1.000 (379 q) | **0.9973** (921 q) | **Sample, not code.** Fell at every widening: 1.000 → 1.000 → 0.9983 → 0.9973 | §2 keeps the superseded reasoning verbatim — a metric at its maximum has no visible variance |
| 2026-08-08 | `15dcdf7` | Recall@1 | 0.747 (744 q) | **0.7445** (921 q) | **Sample, not code.** Rose across three samples then settled fractionally lower; the climb was noise resolving, not a trend | Stage 5 is judged against this row and no earlier one |

**All three are the same event and none of them is a regression in the usual sense**: no code changed, one commit answered all 921 questions. They are recorded here anyway, because the numbers on this page moved *down* between two dated rows, and a reader who finds that later without an entry is entitled to assume something broke. **A log that only records code regressions cannot explain the drops that measurement itself produces** — and those are the ones most likely to be quietly replaced with the flattering earlier figure.

The general lesson is the one §1.1 opens with: **a partial run is a biased sample of its own corpus, and the direction of the bias is unknowable until it finishes.** Two of these three moved against the project's interest. That is the expected behaviour of an honest measurement, not a problem to be resolved.
