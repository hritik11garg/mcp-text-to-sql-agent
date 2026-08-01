# Datasets

> **Status: the loader is built (§3, §5, §8). Counts and digests stay TBD until an archive is actually acquired** — they are properties of a download, and inventing them would make this file lie about what the project has been run against.

---

## 1. Spider

**What it is.** A large cross-domain text-to-SQL benchmark: roughly 10k questions across ~200 databases spanning 138 domains. Test databases are disjoint from training databases, so it measures generalization to unseen schemas rather than memorization.

**Why it is here.** It is the standard reference point — a number on Spider is comparable to published work. Schemas are small and clean, which makes it the right benchmark for getting the core loop correct.

**Weakness, stated up front.** Spider schemas are *too* clean: few columns, clear names, meaningful comments. Schema linking is not very hard on them, so Spider alone would flatter the retriever and understate what the fine-tune contributes.

| Field | Value |
|---|---|
| Source | Yale LILY group |
| Format | SQLite databases + question/SQL pairs |
| License | CC BY-SA 4.0 |
| Subset used | **TBD** — recorded once acquired |
| SHA256 of archive | Recorded in `data/artifacts.lock.json` on first acquisition |

## 2. BIRD

**What it is.** Text-to-SQL over larger, dirtier, more realistic databases — 95 databases, 12,751 questions, with external-knowledge requirements and messy values. Published accuracies run well below Spider.

**Why it is here.** BIRD is where schema linking actually gets hard: many columns, cryptic names, inconsistent value formats. It is the benchmark on which the fine-tuned retriever should show its value, and where `profile_table` earns its place (you cannot write a correct `WHERE` clause against a column whose value format you have not seen).

| Field | Value |
|---|---|
| Source | BIRD-bench |
| Format | SQLite databases + question/SQL pairs + evidence annotations |
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
| SQLite dynamic typing vs PostgreSQL static typing | Inferred from the **data**, not the declaration. Widening only: all-`int` → `bigint`, `int`+`float` → `double precision`, anything else → `text`. Every coercion the declaration did not imply is named in the conversion report |
| Identifier quoting and case-folding | Folded to lower case so unquoted gold SQL resolves; unrepresentable names refuse the database rather than being rewritten ([ADR-019](../architecture/DECISIONS.md#adr-019--benchmark-identifiers-are-folded-to-lower-case-and-ambiguity-is-refused)) |
| `AUTOINCREMENT`, SQLite-specific pragmas | Dropped — irrelevant to read-only querying |
| Views and virtual tables | Not converted. A view is a stored query; a virtual table's backing module can read the filesystem |
| Date/time stored as text | Preserved as text; conversion would change what the gold SQL means |
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

| Outcome | Means |
|---|---|
| `match` | The converted copy reproduced the reference result |
| `mismatch` | The data moved. One is enough to fail the database |
| `gold_error` | The reference query fails on its **own** SQLite database. A benchmark defect; excluded from the denominator |
| `transpile_error` | sqlglot could not render the query for PostgreSQL. Distinct from a mismatch on purpose |
| `postgres_error` | The query ran on SQLite and errored on PostgreSQL — usually a genuine type difference the conversion produced |

A database is `verified` only if **every** comparable query agreed. Not most of them: one disagreement is a class of data that moved, and which questions it affects is unknown until someone looks at it. The command exits **3** when any database fails, distinct from exit 1 for a tool failure, so a CI step cannot pass while reporting that the data is wrong.

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

Writes one JSONL file per split plus `spider-assignment.json`, all under `data/splits/` and all committed — the one thing under `data/` that is.

**Assignment is a hash of the database name, not a seeded shuffle.** A shuffle is reproducible only while the input list is unchanged; add one database and every later one can move to a different split, silently training on what used to be held out. Hashing each name independently makes membership a property of the name alone, so adding databases never moves the ones already assigned. See [ADR-021](../architecture/DECISIONS.md#adr-021--splits-are-a-hash-of-the-database-name-not-a-seeded-shuffle).

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

**Disk.** Spider is a few GB extracted; BIRD's train pack is substantially larger and is not needed to produce a dev or held-out number. Only the extracted tree is kept — nothing is vendored, per §7.
