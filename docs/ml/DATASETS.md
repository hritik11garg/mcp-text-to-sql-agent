# Datasets

> **Status: TBD — Stage 2** for exact counts and checksums. Sourcing, split policy, and licensing constraints below are decided.

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
| Subset used | **TBD — Stage 2** |
| SHA256 of archive | **TBD** |

## 2. BIRD

**What it is.** Text-to-SQL over larger, dirtier, more realistic databases — 95 databases, 12,751 questions, with external-knowledge requirements and messy values. Published accuracies run well below Spider.

**Why it is here.** BIRD is where schema linking actually gets hard: many columns, cryptic names, inconsistent value formats. It is the benchmark on which the fine-tuned retriever should show its value, and where `profile_table` earns its place (you cannot write a correct `WHERE` clause against a column whose value format you have not seen).

| Field | Value |
|---|---|
| Source | BIRD-bench |
| Format | SQLite databases + question/SQL pairs + evidence annotations |
| License | CC BY-SA 4.0 |
| Subset used | **TBD — Stage 2** |
| SHA256 of archive | **TBD** |

**Note on gold quality.** BIRD contains reference queries that are wrong or ambiguous. These cap achievable accuracy. They are **counted and reported** in the failure taxonomy ([EVALUATION.md](EVALUATION.md) §5), not silently dropped — dropping them inflates the score.

## 3. SQLite → PostgreSQL conversion

Both benchmarks ship SQLite. This project runs PostgreSQL ([ADR-001](../architecture/DECISIONS.md#adr-001--postgresql-as-the-only-datastore)), so a conversion step is required.

> **TBD — Stage 2** for the script and its verification.

Known friction, to be handled explicitly rather than discovered late:

| Issue | Handling |
|---|---|
| SQLite dynamic typing vs Postgres static typing | Infer column types from data; record coercions |
| Identifier quoting and case-folding | Postgres lowercases unquoted identifiers; Spider schemas contain mixed case |
| `AUTOINCREMENT`, SQLite-specific pragmas | Dropped — irrelevant to read-only querying |
| Date/time stored as text | Preserved as text; conversion would change what the gold SQL means |
| Gold SQL dialect differences | Gold queries are transpiled with sqlglot for the reference execution path |

**Conversion must be verified, not assumed.** Each converted database is checked by executing every gold query against both the SQLite original and the Postgres copy and comparing result sets. A conversion that silently changes results would corrupt every number downstream.

## 4. Training pairs (derived)

Built from gold SQL as described in [TRAINING.md](TRAINING.md) §2–3.

- **Positive:** `(question, schema_element)` where the element appears in the gold query.
- **Negative:** in-batch, plus mined hard negatives.

> **TBD:** pair counts before and after each cleaning filter.

## 5. Splits

**Split by database, never by question.** Splitting by question puts the same schema elements in train and eval, so the model memorizes the corpus rather than learning to link. That produces an impressive and meaningless Recall@k.

| Split | Databases | Questions | Purpose |
|---|---|---|---|
| Train | TBD | TBD | Retriever fine-tuning |
| Dev | TBD | TBD | Iteration, prompt tuning, debugging |
| Held-out | TBD | TBD | **Reported numbers only** |
| Smoke | ~5 DBs | ~20 | Per-commit regression check |

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

> **TBD — Stage 2.** Download script, expected paths, disk requirements, and checksum verification. Checksums are not optional: a silently different dataset version makes every recorded benchmark incomparable.
