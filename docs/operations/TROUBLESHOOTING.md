# Troubleshooting

> **Living document.** Entries are added when a problem is actually hit, not predicted in advance. The pre-seeded entries below are the failures that are near-certain given this stack — Windows + torch + pgvector + MCP + a 5–6 week build.

Format: **symptom → cause → fix**. Symptom first, because that is what you have when you arrive here.

---

## Environment

### `pip install` tries to build a package from source

**Symptom:** compilation errors, `Microsoft Visual C++ 14.0 or greater is required`.

**Cause:** running on a Python version without a wheel for that package. Every pinned dependency has a cp312 win_amd64 wheel; another interpreter is being used.

**Fix:**
```powershell
python --version            # must be 3.12.x
where.exe python            # confirm it resolves inside .venv
```
`python` on PATH may be a different install than the venv. Activate the venv, or call `.\.venv\Scripts\python.exe` explicitly. See [ADR-010](../architecture/DECISIONS.md#adr-010--python-312).

### `py -3.12` not found

`py -0p` lists installed interpreters. If 3.12 is absent, install it — do not silently fall back to another version. `.python-version` pins 3.12 for a reason.

### torch downloads several gigabytes

**Cause:** the default index serves CUDA-enabled wheels.

**Fix** (CPU-only — fine for inference; fine-tuning will be slow):
```powershell
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```
Install torch **first**, or the CUDA wheel is pulled in as a transitive dependency before the override applies.

### `OSError: [WinError 126]` importing torch

Missing Visual C++ redistributable. Install the Microsoft VC++ Redistributable for x64 and restart the shell.

---

## PostgreSQL

### `psycopg.OperationalError: connection refused`

Postgres is not running or not on the expected port. `docker compose ps`, then `docker compose logs postgres`. Note that "container started" is not "database ready" — see the healthcheck point in [DEPLOYMENT.md](DEPLOYMENT.md) §2.

### `password authentication failed`

Two URLs are configured (`DATABASE_URL` and `DATABASE_RO_URL`) and it is easy to fix one and leave the other. Check both. See [CONFIG.md](CONFIG.md) §2.

### `permission denied for table X` — from `execute_sql`

**This is usually correct behaviour, not a bug.** The read-only role is meant to be unable to reach some things.

Ask which table:
- **A table in `agent_meta`** → working as designed. Generated SQL must not read the audit log or session state. See [SECURITY.md](SECURITY.md) §5.
- **A table in the target schema** → a real grant gap. A table added after the role was created needs `ALTER DEFAULT PRIVILEGES` to have been set (it is, in the migration) or a manual grant.

**Never fix this by granting broader privileges to get past it.** That is how a read-only role stops being read-only.

### `canceling statement due to statement timeout`

Also usually correct — the query was too expensive. The agent should respond by narrowing it, not retrying verbatim ([PROMPTS.md](../ml/PROMPTS.md) §3.3).

If legitimate queries are timing out: check whether the target data is far larger than the timeout assumes, whether an index is missing, and whether `MAX_ESTIMATED_COST` should have rejected it before execution.

### Writes succeed from the read-only role

**Stop and treat this as a security incident, not a bug.**

The role is misconfigured, or `DATABASE_RO_URL` points at the wrong role. Verify:
```sql
SELECT grantee, privilege_type FROM information_schema.role_table_grants
WHERE grantee = 'sql_agent_ro';
```
The negative tests in [../development/TESTING.md](../development/TESTING.md) exist to catch this in CI. If they were green and this happened in production, the tests are testing the wrong database.

---

## pgvector

### `type "vector" does not exist`

Extension not created. `CREATE EXTENSION IF NOT EXISTS vector;` — and it belongs in the first migration, not in a manual step, or it will be missing on the next clean database.

### `expected N dimensions, not M`

The configured embedding model's dimension does not match the column type. `vector(384)` is fixed by DDL; a model with a different dimension is a **migration**, not a config toggle. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §6.

### Retrieval returns irrelevant results

Check in this order:

1. **`RETRIEVER_MODEL_VERSION` vs indexed vectors.** The single most likely cause. Query embeddings from one model against corpus embeddings from another land in different vector spaces — and this **fails silently**, returning plausible-looking garbage rather than an error.
   ```sql
   SELECT model_version, count(*) FROM agent_meta.schema_elements GROUP BY 1;
   ```
2. Vectors exist at all for the configured dataset.
3. `HNSW_EF_SEARCH` not set too low.
4. The serialization actually contains type and comment text, not bare column names.

### Vector search is slow

HNSW index missing or not used. `EXPLAIN` the retrieval query — a sequential scan over embeddings means the index is absent or the pre-filter defeated it. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §9.

---

## MCP

### Claude Desktop does not list the tools

1. Config file JSON is valid (a trailing comma silently breaks the whole file).
2. **Absolute path to the virtualenv's interpreter.** A host does not inherit an activated environment, so a bare `python` resolves to whatever is first on the system `PATH` and dies on the first import.
3. **`PYTHONPATH` points at `src/`.** The packages live there, not at the repo root.
4. **Both `DATABASE_URL` and `DATABASE_RO_URL` are set**, and they are different roles.
5. **The catalog is indexed for the configured `DATASET`.** A server with an empty catalog refuses to start rather than answering "no such table" to a schema that plainly has one — the error names the dataset.
6. The server runs standalone: launch it manually and check it starts without traceback.
7. Claude Desktop fully restarted, not just the window closed.
8. Check Claude Desktop's own MCP logs — startup failures go to stderr, which most hosts surface in a log pane rather than in the conversation.

Copy-pasteable config: [../architecture/MCP.md](../architecture/MCP.md) §9.

### Server starts then immediately exits

**Historically: something wrote to stdout.** Under stdio transport, stdout *is* the JSON-RPC channel, and a stray `print()`, a library banner or a progress bar corrupts the stream.

**This is now guarded.** Each server calls `claim_stdout()` at startup, which hands the real stream to the transport and repoints `sys.stdout` at stderr — so a stray write becomes a log line rather than a dead session. Logging is forced onto stderr with `force=True`, so an earlier `basicConfig` by a library cannot redirect it.

**So if a server still exits immediately, look elsewhere first:** a startup failure. Bad `DATABASE_URL`, unreachable database (fails after `DB_CONNECT_TIMEOUT_MS` rather than hanging), or an empty catalog. All three write a named error to **stderr** and exit non-zero.

The residual stdout case is anything that writes to file descriptor 1 *before* `claim_stdout()` runs — an import-time banner in a dependency. Reproduce by launching the server manually and checking whether stdout carries anything that is not JSON-RPC.

### `tools/call` times out

The host's own call timeout must exceed `STATEMENT_TIMEOUT_CEILING_MS` (60 s by default). Otherwise the MCP call gives up before the database does, and a normal query timeout is misreported as an MCP fault.

> **`MCP_CALL_TIMEOUT_MS` does not exist.** It is documented in [CONFIG.md](CONFIG.md) §9 as planned, for the HTTP transport that is not built. Under stdio the timeout is the host's, not this project's, so there is nothing here to validate at startup — set it in the host's configuration.

### The agent never calls a tool it should

The tool description is the model's only selection signal. If a tool is not being chosen, the description does not say *when* to call it. See [MCP.md](../architecture/MCP.md) §3 — descriptions are prompts and are versioned as such.

---

## LLM

### `authentication_error`

`ANTHROPIC_API_KEY` unset in the process's environment. A key in `.env` does not reach a process that never loaded `.env`.

### `rate_limit_error` during eval runs

The eval harness parallelizes; sequential dev use did not. Reduce harness concurrency — the SDK retries with backoff automatically, so persistent 429s mean sustained over-limit, not transient contention.

### Costs are far higher than expected

In order of likelihood:

1. **Prompt caching not working.** Check `usage.cache_read_input_tokens` — if it is zero across repeated requests, something volatile is in the cached prefix. This is silent; there is no error. See [PROMPTS.md](../ml/PROMPTS.md) §4.
2. **Retry loops.** Check `sql_generation_attempts` — many attempts per question means each question costs several generations.
3. **Full schema in the prompt.** Retrieval failing open dumps the entire schema.
4. **Decomposition over-triggering.** A single-step question split into five sub-queries costs five times as much.

### The model writes SQL for the wrong dialect

Date functions are the usual tell (`strftime` instead of `date_trunc`). The dialect must be stated explicitly in the generation prompt — an unstated dialect tends to produce SQLite-flavoured SQL, especially when the benchmark data originated as SQLite.

---

## Training

### CUDA out of memory

Reduce batch size — but note that with `MultipleNegativesRankingLoss`, **batch size is the number of negatives per example**, so changing it changes the task difficulty. A comparison across different batch sizes is not an ablation, it is two different experiments. Prefer gradient accumulation, and record the effective batch size either way. See [../ml/TRAINING.md](../ml/TRAINING.md) §6.

### Recall@k is suspiciously high

Check the split. If databases straddle train and eval, the model has memorized the corpus rather than learned to link, and the number is meaningless. Split by **database**, never by question — [../ml/DATASETS.md](../ml/DATASETS.md) §5.

### Fine-tuned model performs worse than baseline

A real possibility, and a publishable result ([ADR-006](../architecture/DECISIONS.md#adr-006--fine-tune-the-schema-linker-rather-than-retrieve-more-candidates)). Before concluding it, rule out:

1. `RETRIEVER_MODEL_VERSION` mismatch — corpus not re-embedded with the new model.
2. Overfitting on a small pair set — check the training curve.
3. Learning rate too high, degrading the pretrained representation.
4. Evaluating on the wrong split.

If all four are ruled out and it is still worse, record it in [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) and write up why in TRAINING.md.

---

## Docker

### `exec format error` / `no matching manifest`

Architecture mismatch (arm64 image on amd64 or vice versa). Build for the local platform or use `--platform`.

### API container restart-loops on startup

Usually the database is not ready. `depends_on` alone waits for container start, not database readiness — it needs `condition: service_healthy` with a real healthcheck. See [DEPLOYMENT.md](DEPLOYMENT.md) §2.

### Migrations run several times concurrently

Migrations are being invoked from the API entrypoint with more than one replica. They belong in a separate one-shot service that the API depends on.
