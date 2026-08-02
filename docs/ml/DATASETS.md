# Datasets

> **Status: Spider is acquired, converted and verified** — §1 and §3.1 carry measured numbers from a real run. BIRD is not; its counts and digest stay TBD, because they are properties of a download and inventing them would make this file lie about what the project has been run against.

---

## 1. Spider 1.0

**What it is.** A large cross-domain text-to-SQL benchmark: roughly 10k questions across ~200 databases spanning 138 domains. Test databases are disjoint from training databases, so it measures generalization to unseen schemas rather than memorization.

**Why it is here.** It is the standard reference point, and schemas are small and clean, which makes it the right benchmark for getting the core loop correct.

> **Spider 1.0, not Spider 2.0 — and this needs saying now that both exist.** Spider 2.0 is a *different task*, not a newer version of this one: 632 enterprise workflow problems over databases with 1,000–3,000+ columns, where a solution is multiple queries often exceeding 100 lines, and **the expected output is CSV files rather than SQL**.
>
> | Setting | Examples | Databases | Cost |
> |---|---|---|---|
> | Spider 2.0-Snow | 547 | Snowflake | Free of charge (sponsored quota; queries queued) |
> | Spider 2.0-DBT | 68 | DuckDB via dbt | Free, and fully local |
> | Spider 2.0-Lite | 547 | BigQuery 214 · Snowflake 198 · **SQLite 135** | Some cost |
>
> **Cost is not the disqualifier, and an earlier version of this note wrongly said it was.** Two of the three settings are free, and one runs on DuckDB with nothing hosted. The disqualifier is that **Spider 2.0 does not ship the gold SQL this harness needs** — the project states only a small amount of gold SQL is released, and explicitly discourages using it for fine-tuning. Execution accuracy and Recall@k are both computed *from* a reference query here; without one there is nothing to compare against and nothing to extract gold schema elements from.
>
> The practical argument is just as strong: GPT-4o scores **10.1%** on Spider 2.0 against 86.6% on Spider 1.0. Bringing up a pipeline that has never produced a number, on a benchmark where a frontier model scores 10%, means being unable to tell "my retrieval is broken" from "the benchmark is hard."
>
> **Spider 2.0-DBT is a genuine Stage 4 target** — 68 tasks, DuckDB, local, no account. Worth revisiting once the agent layer exists and there is a Spider 1.0 baseline to reason from. It is not a substitute here.
>
> Spider 1.0's leaderboard closed to new submissions in February 2024 and its test set is public. That costs nothing this project needs — the goal is a number in a published range, not a leaderboard row.

**Comparability, stated precisely.** Since November 2020 Spider's *official* metric has been **Test Suite Accuracy**: the query is executed against several randomly generated databases, so a query that coincidentally returns the right rows on one instance is caught. This harness computes **execution accuracy against a single database** ([EVALUATION.md](EVALUATION.md) §1.1). The two are not the same number, and this one is the **more generous** of the pair — it counts false positives that a test suite would reject. Numbers produced here are in a comparable *range* to published work and are not directly comparable to a leaderboard entry, and BENCHMARKS.md rows must say which metric they are.

**Weakness, stated up front.** Spider schemas are *too* clean: few columns, clear names, meaningful comments. Schema linking is not very hard on them, so Spider alone would flatter the retriever and understate what the fine-tune contributes.

| Field | Value |
|---|---|
| Source | Yale LILY group — the Spider **1.0** dataset link on <https://yale-lily.github.io/spider> |
| Format | SQLite databases + question/SQL pairs |
| Layout | The archive nests everything one level down, under `spider_data/`: `spider_data/database/<db_id>/<db_id>.sqlite`, plus `train_spider.json` / `dev.json` / `tables.json` alongside it. Pass `--databases data/spider/spider_data/database` |
| License | CC BY-SA 4.0 |
| Subset used | `dev.json` — **1034 questions over 20 databases** — converted and verified (§3.1). Split assignment covers all **160** databases named by `dev.json` and `train_spider.json`: 104 train, 16 dev, 7 smoke, 33 held-out |
| SHA256 of archive | `00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b` — `spider_data.zip`, 205,800,266 bytes, recorded 2026-08-01 in `data/artifacts.lock.json` |

## 2. BIRD

**What it is.** Text-to-SQL over larger, dirtier, more realistic databases — 95 databases, 12,751 questions, with external-knowledge requirements and messy values. Published accuracies run well below Spider.

**Why it is here.** BIRD is where schema linking actually gets hard: many columns, cryptic names, inconsistent value formats. It is the benchmark on which the fine-tuned retriever should show its value, and where `profile_table` earns its place (you cannot write a correct `WHERE` clause against a column whose value format you have not seen).

| Field | Value |
|---|---|
| Source | BIRD-bench — <https://bird-bench.github.io/> |
| Format | SQLite databases + question/SQL pairs + evidence annotations |
| Layout | `dev_databases/<db_id>/<db_id>.sqlite` — note the folder is not called `database`, so pass it to `--databases` explicitly. Each folder also holds `database_description/*.csv`, which is why databases are matched on the **directory** name rather than by globbing `*.sqlite` |
| License | CC BY-SA 4.0 |
| Subset used | **TBD** — recorded once acquired |
| SHA256 of archive | Recorded in `data/artifacts.lock.json` on first acquisition |

**Note on gold quality.** BIRD contains reference queries that are wrong or ambiguous. These cap achievable accuracy. They are **counted and reported** in the failure taxonomy ([EVALUATION.md](EVALUATION.md) §5), not silently dropped — dropping them inflates the score.

## 3. SQLite → PostgreSQL conversion

Both benchmarks ship SQLite. This project runs PostgreSQL ([ADR-001](../architecture/DECISIONS.md#adr-001--postgresql-as-the-only-datastore)), so a conversion step is required.

```
python -m benchmark.load convert --databases data/spider/database --prefix spider_ --keep-going
```

**One PostgreSQL schema per benchmark database.** Spider ships ~200 databases and a question is only meaningful against its own, so `concert_singer` becomes the schema `spider_concert_singer`. The prefix is not cosmetic: Spider and BIRD both ship a database called `movie`, and loading the second over the first would silently replace it.

**Planned first, executed second.** `plan_database` reads the source and decides every target name and type without touching PostgreSQL; `convert_database` executes a plan. The decisions worth arguing about are all in the half that needs no server, which is also the half that is unit-tested.

| Issue | Handling |
|---|---|
| SQLite dynamic typing vs PostgreSQL static typing | Inferred from the **data**, not the declaration, and **exactly** — SQLite is asked with `group_concat(DISTINCT typeof(col))` rather than sampled ([ADR-024](../architecture/DECISIONS.md#adr-024--column-types-are-inferred-from-sqlites-own-typeof-over-the-whole-column)). Widening only: all-`int` → `bigint`, `int`+`float` → `double precision`, anything else → `text`. Every coercion the declaration did not imply is named in the conversion report |
| A foreign key whose two sides hold different types | Both sides take the **numeric** type, if every value converts losslessly ([ADR-025](../architecture/DECISIONS.md#adr-025--a-foreign-key-joining-two-types-is-unified-toward-the-numeric-side)). SQLite joins `'1'` to `1` by applying numeric affinity to the text side; PostgreSQL answers `operator does not exist`. Measured on Spider: **35 of 769 foreign keys, across 21 of 166 databases**. Never unified toward text — `'01' = 1` is true under affinity and `'01' = '1'` is not, so that direction would silently change which rows join. A column that cannot convert keeps its type, the constraint is dropped, and both are reported |
| Identifier quoting and case-folding | Folded to lower case so unquoted gold SQL resolves; unrepresentable names refuse the database rather than being rewritten ([ADR-019](../architecture/DECISIONS.md#adr-019--benchmark-identifiers-are-folded-to-lower-case-and-ambiguity-is-refused)). "Unrepresentable" means a double quote, a backslash, a control character or a non-ASCII byte — and nothing else. A narrower rule refused `%_Change_2007` and `Official_ratings_(millions)`, which are perfectly safe inside `sql.Identifier` |
| `AUTOINCREMENT`, SQLite-specific pragmas | Dropped — irrelevant to read-only querying |
| Views and virtual tables | Not converted. A view is a stored query; a virtual table's backing module can read the filesystem |
| Date/time stored as text | Preserved as text; conversion would change what the gold SQL means |
| A value that does not fit the planned type | Raises, naming database, table, column and the value. Under exact inference this should be unreachable; if it is ever reached, the message is the diagnosis rather than an `invalid literal for int()` naming nothing |
| Primary and foreign keys | Added **after** the data loads, foreign keys as `NOT VALID`. They are metadata for schema retrieval and join reasoning, not integrity enforcement — benchmark data is routinely inconsistent, and a constraint the data cannot satisfy is skipped and recorded rather than failing the database |
| Text that is not valid UTF-8 | Decoded with replacement characters and **counted** in the report. Refusing the database for one bad byte would cost most of BIRD; doing it silently would change values a gold `WHERE` clause filters on |
| Gold SQL dialect differences | Gold queries are transpiled with sqlglot for the reference execution path |

**What the conversion deliberately does not do:** rewrite data so that gold queries pass. A column holding a mix of numbers and text becomes `text`, and a gold query comparing it to a number then fails on PostgreSQL where it succeeded on SQLite. That failure is real and §3.1 is what finds it. Coercing the column and dropping the rows that do not fit would make the query pass and the answer wrong.

**Grants.** Migration 002 grants the read-only role privileges on `public` only, so a converted schema is invisible to it until granted explicitly. The loader issues `USAGE` on the schema and `SELECT` on its tables — and nothing else, ever. Asserted by integration tests that check the role can read a converted schema and still cannot write to it or create in it.

### 3.1 Verifying the conversion

**Conversion must be verified, not assumed.** Every gold query is executed against both the SQLite original and the PostgreSQL copy, and the results are compared.

```
python -m benchmark.load verify --databases data/spider/database \
    --questions data/spider/dev.json --benchmark spider --prefix spider_
```

The comparator is `evals.comparison.compare` — the eval harness's own, not a stricter one written for this purpose. The question is not whether the two databases are identical (they are not; one is SQLite) but whether the eval will score a correct answer as correct on the converted copy, and only the thing that will do the scoring can answer that. See [ADR-022](../architecture/DECISIONS.md#adr-022--the-conversion-is-verified-by-the-eval-harnesss-own-comparator).

| Outcome | Means | In the denominator? |
|---|---|---|
| `match` | The converted copy reproduced the reference result | Yes |
| `ambiguous_order` | Identical rows, different order, from a gold query that never determined one — `ORDER BY age` with tied ages. A property of the benchmark query, not the conversion ([ADR-027](../architecture/DECISIONS.md#adr-027--an-undetermined-result-order-is-not-a-mismatch--in-verification-only)) | Yes, as agreement |
| `mismatch` | The data moved. One is enough to fail the database | Yes |
| `gold_error` | The reference query fails on its **own** SQLite database. A benchmark defect | No |
| `transpile_error` | sqlglot could not render the query for PostgreSQL. Distinct from a mismatch on purpose | No |
| `dialect_error` | The gold query asks for something PostgreSQL does not offer — `42883`, `42803`, `42804`. It would fail identically against a perfect conversion ([ADR-026](../architecture/DECISIONS.md#adr-026--gold-sql-is-repaired-for-sqlites-quoted-literal-rule-and-dialect-gaps-are-not-conversion-faults)) | No |
| `postgres_error` | A missing table, column or schema — `42P01`, `42703`, `3F000`. The names are what the conversion chose, so the conversion is why | Yes, as failure |

The last two used to be one bucket, and that bucket was named after the component under test. It absorbed 213 questions that had nothing to do with the conversion.

A database is `verified` only if **every** comparable query agreed. Not most of them: one disagreement is a class of data that moved, and which questions it affects is unknown until someone looks at it. The command exits **3** when any database fails, distinct from exit 1 for a tool failure, so a CI step cannot pass while reporting that the data is wrong.

**Measured — Spider `dev.json`, 2026-08-02.** 20 databases, 1034 questions, all 20 converted:

| | Questions | |
|---|---|---|
| `match` | 896 | 86.7% |
| `ambiguous_order` | 16 | 1.5% |
| `mismatch` | 25 | 2.4% |
| `dialect_error` | 97 | 9.4% — 56 `GROUP BY`, 41 type-affinity comparisons |
| `postgres_error` | 0 | |
| **Conversion fidelity** | **912 / 937** | **97.3%** of comparable questions |

**10 of 20 databases verify completely.** The other 10 hold the 25 mismatches — 22 classified `no_column_bijection`, 3 `shape_mismatch` — which are open and not yet diagnosed. They are stated here rather than in a footnote because a fidelity number without its failures is a marketing number.

**The 97 dialect errors leave the denominator, and that must be reported with any accuracy figure computed from this split.** They are questions whose gold SQL has no PostgreSQL expression, so they cannot be scored later either — but an exclusion that is not reported is indistinguishable from cheating. Same rule §5 of [EVALUATION.md](EVALUATION.md) applies to gold errors.

## 4. Training pairs (derived)

Built from gold SQL as described in [TRAINING.md](TRAINING.md) §2–3.

- **Positive:** `(question, schema_element)` where the element appears in the gold query.
- **Negative:** in-batch, plus mined hard negatives.

> **TBD:** pair counts before and after each cleaning filter.

## 5. Splits

**Split by database, never by question.** Splitting by question puts the same schema elements in train and eval, so the model memorizes the corpus rather than learning to link. That produces an impressive and meaningless Recall@k.

```
python -m benchmark.load splits --questions data/spider/dev.json --benchmark spider --dataset spider
```

Writes one JSONL file per split plus `spider-assignment.json`, under `data/splits/`.

**Only the assignment is committed.** It is a map of database name to split — metadata, a few kilobytes. The per-split `.jsonl` files hold the questions and gold SQL themselves, which is the benchmark, which is CC BY-SA, and §7 says benchmark data is not vendored. They regenerate exactly from the assignment plus the archive the lockfile pins, so committing them would redistribute 2.5 MB of someone else's licensed data to save one command.

**Assignment is a hash of the database name, not a seeded shuffle.** A shuffle is reproducible only while the input list is unchanged; add one database and every later one can move to a different split, silently training on what used to be held out. Hashing each name independently makes membership a property of the name alone, so adding databases never moves the ones already assigned. See [ADR-021](../architecture/DECISIONS.md#adr-021--splits-are-a-hash-of-the-database-name-not-a-seeded-shuffle).

Spider's 160 databases assign as **104 train / 16 dev / 7 smoke / 33 held-out** — smoke being a sub-band of dev, so the dev band is 23 of 160 rather than 16.

> **Open, and it affects what any number here may be compared to.** This split cuts across Spider's *own* train/dev boundary: `spider-dev` is a hash-selected slice of both files, not Spider's `dev.json`. Published Spider numbers are computed on Spider's dev set, so **a score from this split is not comparable to them** — only to other scores from this split. The alternative is to adopt Spider's dev set as held-out and carve an internal dev from their train, which buys comparability and gives up the property [ADR-021](../architecture/DECISIONS.md#adr-021--splits-are-a-hash-of-the-database-name-not-a-seeded-shuffle) was written for. ADR-021's own *Revisit* clause anticipates exactly this. Undecided; every BENCHMARKS.md row must state which split it used until it is.

| Split | Share | Purpose |
|---|---|---|
| Train | 70% | Retriever fine-tuning |
| Dev | 12.5% | Iteration, prompt tuning, debugging |
| Held-out | 15% | **Reported numbers only** |
| Smoke | 2.5% (~5 DBs at Spider's size) | Per-commit regression check |

> Shares, not counts. Proportions are approximate at small corpus sizes, which is the cheaper mistake: a 68/12/20 split is a fine split, and a held-out set that quietly absorbed three training databases is not a split at all. **TBD:** the realised database and question counts, once an archive is acquired.

Smoke is a **sub-band of dev** — never carved from held-out, which would mean the per-commit check touches the set reserved for reported numbers, and never from train, whose schemas the retriever has been fitted to.

Split assignments are committed as a file, not regenerated — a split that changes between runs makes runs incomparable.

**Held-out discipline.** Touched once per reported result. If it gets used for iteration, that is recorded here and a fresh held-out split is carved from unused databases.

## 6. Custom evaluation set

> **TBD — Stage 4.**

Spider and BIRD are single-query benchmarks. The multi-step decomposition claim needs compound questions ("compare Q3 vs Q4 growth by region and flag anomalies"), which neither provides. A small hand-authored set (~30–50 tasks) over the same databases will be needed, with rubrics rather than gold SQL, since multiple query decompositions can be correct.

Hand-authoring the eval set for a capability you also built is a real bias risk. Mitigation: write the tasks before implementing decomposition, and record that they were written first.

## 7. Licensing

| Dataset | License | Constraint |
|---|---|---|
| Spider | CC BY-SA 4.0 | Attribution + share-alike on derivatives |
| BIRD | CC BY-SA 4.0 | Attribution + share-alike on derivatives |

**Consequences for this repo:**

- Benchmark data is **not vendored**. A download script fetches it; the data stays out of version control.
- Derived artifacts (training pairs, converted Postgres dumps) are derivative works under share-alike. They are not committed, and if published they carry the same license.
- The repo's MIT license covers **code only**. Stated explicitly in [../../LICENSE](../../LICENSE).
- Fine-tuned checkpoints trained on this data inherit share-alike obligations. Anything published carries the attribution and license notice.

## 8. Acquisition

```
python -m benchmark.load acquire spider --archive ~/Downloads/spider.zip --trust-on-first-use
```

**Sources are an allowlist in source code.** `benchmark/sources.py` names each artifact, where a human goes to get it, and its license page. There is deliberately **no `--url` flag**: the thing being downloaded, extracted and parsed cannot be redirected by an argument an operator can be talked into. Neither benchmark currently has a stable direct download — Spider is served through Google Drive, BIRD through a project page — so both are fetched by hand and adopted with `--archive`. The integrity check is identical either way, because it happens on the bytes on disk rather than on the transport.

**Checksums are not optional.** A silently different dataset version makes every recorded benchmark incomparable, and the failure is invisible. `data/artifacts.lock.json` holds the SHA-256 of every archive this project has been run against, is committed, and is checked before extraction. The first acquisition records what it saw — explicitly, behind `--trust-on-first-use`, with a warning — and `record()` will not overwrite an existing entry, so the flag cannot launder a second, different archive. See [ADR-020](../architecture/DECISIONS.md#adr-020--benchmark-archives-are-pinned-by-a-committed-lockfile-recorded-on-first-use).

**Extraction is hostile-input handling, not a utility call.** `ZipFile.extractall` will write a member named `../../../.ssh/authorized_keys`; CVE-2007-4559 is the same bug in `tarfile`, unfixed for fifteen years. Every member is validated before a single byte is written:

| Refused | Because |
|---|---|
| Absolute paths, drive letters, `..` in any component | Writes outside the destination |
| Backslash separators | A path separator on Windows and an ordinary character in a zip name, so a POSIX-only check misses `..\..\x` |
| Symlinks and non-regular files | The second half of a two-step traversal: plant a link, then write "through" it with a member whose own path looks safe |
| A member that expands past its budget | Decompression bomb. Enforced against bytes **written**, never against the size the archive declares |
| More members than `BENCHMARK_MAX_ARCHIVE_MEMBERS` | Inode exhaustion |

A rejected archive leaves nothing behind: validation covers the whole archive first, so a partial extraction can never be picked up by a later run as though it had succeeded. Asserted in `tests/security/test_benchmark_acquisition.py`, which builds each of these archives and checks the file is genuinely not on disk afterwards.

**A name this filesystem cannot store is a different fact, and gets a different verdict.** Spider ships `receipts (3:11:18, 5:53 PM)_original.csv`; a colon on Windows is a drive or stream separator. Refusing the archive for it fails the entire benchmark over a CSV the loader never reads, and the same archive is fine on Linux — so an unrepresentable member is **skipped and listed in the extraction report**, while an escaping member still refuses the whole archive. The exception is a database file (`.sqlite`, `.sqlite3`, `.db`), which refuses, because skipping one would silently change which databases exist. See [ADR-023](../architecture/DECISIONS.md#adr-023--an-unrepresentable-archive-name-is-skipped-and-recorded-an-escaping-one-refuses-the-archive) — including the ordering bug that briefly made a traversal attempt look like a portability problem.

**Disk.** Spider is a few GB extracted; BIRD's train pack is substantially larger and is not needed to produce a dev or held-out number. Only the extracted tree is kept — nothing is vendored, per §7.
