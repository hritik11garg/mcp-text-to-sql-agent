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

**Before changing the password, check what is answering on the port.** A native PostgreSQL installed on the host takes 5432 first, and the compose container then binds a different port or fails silently — so correct credentials get rejected by an entirely different server that happens to be listening. `docker compose ps` shows the mapping the container actually got. Moving the container's published port and updating both URLs is the fix; changing the password to match the wrong server is not.

### `invalid connection option "postgresql+psycopg"` or a DSN in an error message

`DATABASE_URL` is a **SQLAlchemy** URL because alembic requires the `+driver` form. psycopg cannot parse it. Every psycopg call site converts it through `core.dsn.libpq_dsn()`, so this means a new call site passed the configured value straight to `psycopg.connect` — fix the call site, do not add a second variable ([ADR-028](../architecture/DECISIONS.md#adr-028--one-connection-string-form-per-consumer-converted-at-the-driver)).

**If you saw the connection string itself in the output, the password in it is compromised — rotate it.** psycopg quotes the DSN in parse errors; `core.dsn.redact_dsn()` masks it, and anything that got past that is a bug worth reporting. See [SECURITY.md](SECURITY.md) §14.2.10.

### `permission denied for table X` — from `execute_sql`

**This is usually correct behaviour, not a bug.** The read-only role is meant to be unable to reach some things.

Ask which table:
- **A table in `agent_meta`** → working as designed. Generated SQL must not read the audit log or session state. See [SECURITY.md](SECURITY.md) §5.
- **A table in the target schema** → a real grant gap. A table added after the role was created needs `ALTER DEFAULT PRIVILEGES` to have been set (it is, in the migration) or a manual grant.

**Never fix this by granting broader privileges to get past it.** That is how a read-only role stops being read-only.

### `canceling statement due to statement timeout`

Also usually correct — the query was too expensive. The agent should respond by narrowing it, not retrying verbatim ([PROMPTS.md](../ml/PROMPTS.md) §3.3).

If legitimate queries are timing out: check whether the target data is far larger than the timeout assumes, whether an index is missing, and whether `MAX_ESTIMATED_COST` should have rejected it before execution.

### `DATABASE_RO_URL can write` at startup — the process refuses to start

**Working as intended, and worth reading rather than working around.**

`assert_read_only` asked PostgreSQL what the role could do and found a write privilege, a `CREATE`, or a grant-bypassing role attribute. The message names what it found:

```
DATABASE_RO_URL can write. The role it connects as holds INSERT, UPDATE,
DELETE or TRUNCATE on: public.orders, public.customers. ...
```

or, for the most common case:

```
DATABASE_RO_URL connects as a role with: SUPERUSER (bypasses every grant ...)
```

Almost always, `DATABASE_RO_URL` is pointing at the **owner** role. The check that used to exist only compared the two connection strings for inequality, so `…@localhost/db` and `…@127.0.0.1/db` passed it while being the same superuser. Point it at the login role migration 002 creates (`sql_agent_login`), or revoke the grants.

There is no setting to skip this. See [SECURITY.md](SECURITY.md) §13.2 and [ADR-033](../architecture/DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing).

### `DATABASE_RO_URL can create objects in: public`

The role holds `CREATE` on a schema. On PostgreSQL before 15, `PUBLIC` gets `CREATE` on `public` by default; migration 002 revokes it, but a database created another way will not have had that done.

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM sql_agent_ro;
```

CREATE is a write: a role that can add a table can add a trigger.

### Writes succeed from the read-only role

**Stop and treat this as a security incident, not a bug.**

This should now be unreachable — `assert_read_only` refuses to open the connection at startup, so a process that is running has already proved it. If you see it anyway, the interesting question is *how the process started*, not what the grants are.

Verify:
```sql
SELECT grantee, privilege_type FROM information_schema.role_table_grants
WHERE grantee = 'sql_agent_ro';
```
The negative tests in [../development/TESTING.md](../development/TESTING.md) prove the *migration* produces a correct role. They build that role in a testcontainer and never look at the one your `.env` names — which is exactly why the startup assertion had to exist as well.

---

## pgvector

### `type "vector" does not exist`

Extension not created. `CREATE EXTENSION IF NOT EXISTS vector;` — and it belongs in the first migration, not in a manual step, or it will be missing on the next clean database.

### `expected N dimensions, not M`

The configured embedding model's dimension does not match the column type. `vector(384)` is fixed by DDL; a model with a different dimension is a **migration**, not a config toggle. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §6.

### Retrieval returns irrelevant results

Check in this order:

1. **`RETRIEVER_MODEL` vs indexed vectors.** The single most likely cause. Query embeddings from one model against corpus embeddings from another land in different vector spaces — and this **fails silently**, returning plausible-looking garbage rather than an error.
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

### A host does not list the tools

1. Config file JSON is valid (a trailing comma silently breaks the whole file).
2. **Absolute path to the virtualenv's interpreter.** A host does not inherit an activated environment, so a bare `python` resolves to whatever is first on the system `PATH` and dies on the first import.
3. **`PYTHONPATH` points at `src/`.** The packages live there, not at the repo root.
4. **Both `DATABASE_URL` and `DATABASE_RO_URL` are set**, and they are different roles.
5. **The catalog is indexed for the configured `DATASET`.** A server with an empty catalog refuses to start rather than answering "no such table" to a schema that plainly has one — the error names the dataset.
6. The server runs standalone: launch it manually and check it starts without traceback.
7. The host fully restarted, not just the window closed — desktop apps commonly cache the server list.
8. Check the host's own MCP logs — startup failures go to stderr, which most hosts surface in a log pane rather than in the conversation.

**Fastest way to isolate whether it is the server or the host:** run the server under the open-source MCP Inspector (`npx @modelcontextprotocol/inspector ...`, [MCP.md](../architecture/MCP.md) §9.2). If the tools list there, the server is fine and the problem is the host config.

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

`LLM_API_KEY` unset in the process's environment. A key in `.env` does not reach a process that never loaded `.env`. (There is no provider-specific key variable — one adapter serves every OpenAI-compatible endpoint, so there is one key setting.)

### `rate_limit_error` / HTTP 429 during eval runs

**Not concurrency — the harness is sequential.** A 150-question run at ~1000 tokens each is enough to hit a free tier's *per-minute* or *daily* cap on its own. The SDK retries with backoff, then `LLM_MODEL_FALLBACKS` advances to the next model.

That advance is the thing to check rather than the 429 itself. Read `answered_by` in the run summary: if more than one model appears, `single_model` is `false` and **the accuracy figure is a weighted average of two systems**. Measured on one run: 96% for one model and 59% for the other, reported as 75.3% — a number neither earned. Re-run with a single model, or record the row as a blend and mark it.

### `LLMUnavailableError ... HTTP 413`

`LLM_MAX_TOKENS` is above the endpoint's completion cap. It is a *request* rejection — the model never ran — and the default of 16000 exceeds at least one free tier's limit. Measured on Groq with `openai/gpt-oss-120b`: 6000 accepted, 8192 refused. Set `LLM_MAX_TOKENS=4096`.

Not to be confused with an oversized prompt, which this almost never is: the `full-schema` baseline sends the most schema of any configuration and a Spider database is 10–67 catalog elements.

### Every generated query fails to execute, and the SQL starts with `<think>`

The model is putting its reasoning in the `content` field and the answer after `</think>`, and an older build submitted the whole monologue as a query. The generator strips a leading block. If this reappears, the tag is a different one — check the raw `generated_sql` in a per-question artifact, which is stored precisely for this.

Worth knowing *which* model did it: the configured model may not, while a fallback does, so the symptom appears only once a rate limit moves the chain. That is how it went unnoticed through an entire run.

### Half the questions come back `unanswerable`

The model is refusing rather than guessing, which is correct behaviour and points at **retrieval, not the prompt**. `RETRIEVAL_TOP_K` defaults to 10; a Spider database holds 10–67 catalog elements in total, so `k=10` shows a partial schema and one missing column is enough for an honest refusal.

Check Recall@k in the summary. Measured on Spider dev: `k=10` gave 42.7% execution accuracy with 75 of 150 unanswerable; `k=30` gave 72.7% with none, and Recall@20 is 1.0. Raise `--top-k` before touching the prompt.

### Costs are far higher than expected

In order of likelihood:

1. **Prompt caching not working.** Check `usage.cache_read_input_tokens` — if it is zero across repeated requests, something volatile is in the cached prefix. This is silent; there is no error. See [PROMPTS.md](../ml/PROMPTS.md) §4.
2. **Retry loops.** Check `sql_generation_attempts` — many attempts per question means each question costs several generations.
3. **Full schema in the prompt.** Retrieval failing open dumps the entire schema.
4. **Decomposition over-triggering.** A single-step question split into five sub-queries costs five times as much.

### The model writes SQL for the wrong dialect

Date functions are the usual tell (`strftime` instead of `date_trunc`). The dialect must be stated explicitly in the generation prompt — an unstated dialect tends to produce SQLite-flavoured SQL, especially when the benchmark data originated as SQLite.

---

## Benchmark loading

### `no digest is recorded for 'spider'`

Expected on a first acquisition. Nothing is checked against the archive you have, so the loader will not extract it silently. Re-run with `--trust-on-first-use` and **commit `data/artifacts.lock.json`** — until it is committed, nothing checks that later runs use the same data.

### `'spider' does not match the recorded digest`

The archive is not the one every recorded number came from. Two causes, and they need different responses:

- **The benchmark was re-released.** Decide which archive the project is measuring against and re-run the affected benchmarks. Then update the lockfile in a commit that says so.
- **The download is not what it claims to be.** Do not extract it.

The one response that is always wrong is editing the lockfile to make the check pass. `record()` will not overwrite an existing entry for exactly this reason, so `--trust-on-first-use` cannot be used to launder a second archive.

### `archive member '...' escapes the destination`

Working as intended, and worth reading before assuming a bug. `ZipFile.extractall` would have written that file. If the archive came from the official source and the digest matched, report it upstream; if the digest did not match, that is the more interesting finding.

### `archive member '...' cannot be represented on this filesystem`

Not a refusal — a **skip**, listed in the extraction report. The member has a name Windows cannot store (a colon, `<>"|?*`, a trailing dot or space, a reserved device name); Spider genuinely ships one. The archive still extracts. If the skipped member is something the loader needs, run the acquisition on Linux or in WSL, where the name is representable.

A skipped `.sqlite` file is the one case that refuses the whole archive instead, because a corpus that quietly lost a database invalidates every number computed from it ([ADR-023](../architecture/DECISIONS.md#adr-023--an-unrepresentable-archive-name-is-skipped-and-recorded-an-escaping-one-refuses-the-archive)).

### `table 'X' contains characters that cannot be used safely`

A source identifier holding a double quote, a backslash, a control character or a non-ASCII byte — or one over 63 bytes. The database is refused rather than converted with the name rewritten, because a rewrite that collides with another name merges two tables and every question about either is then scored against the wrong data ([ADR-019](../architecture/DECISIONS.md#adr-019--benchmark-identifiers-are-folded-to-lower-case-and-ambiguity-is-refused)).

Note that `%`, parentheses, spaces and `$` are **fine** — they are quoted by `sql.Identifier` like anything else. An earlier, narrower rule refused them and cost two Spider databases for nothing.

Use `--keep-going` to convert the rest of the corpus and record which databases were skipped.

### `<db>.<table>.<column> was planned as bigint but holds ''`

A value that does not fit its planned type. Type inference is exact — it asks SQLite with `typeof()` over the whole column — so this should be unreachable, and reaching it means the plan and the data disagree for a reason worth finding rather than working around. Do **not** force the column to `text`: that changes what gold queries comparing it to a number will do ([ADR-024](../architecture/DECISIONS.md#adr-024--column-types-are-inferred-from-sqlites-own-typeof-over-the-whole-column)).

### `schema 'X' already exists`

A previous load left it there. `--replace` drops and reloads it. The loader will not load *into* an existing schema, because the result would be a mix of two databases returning plausible, wrong answers.

### `verify` exits 3

At least one database did not reproduce every gold result. The report names them and the failing queries. Read the outcome before the query:

- `mismatch` — the data moved. This is the one that matters.
- `gold_error` — the reference query fails on its own SQLite database. A benchmark defect; excluded from the denominator and not your problem.
- `transpile_error` — sqlglot could not render the query for PostgreSQL. A parser gap, not a conversion defect.
- `dialect_error` — the gold query asks PostgreSQL for something it does not offer (`42883` undefined operator, `42803` grouping, `42804` type mismatch). It would fail against a perfect conversion too. Excluded from the denominator, and **must be reported alongside any accuracy number computed from the split** — 97 of 1034 on Spider dev.
- `postgres_error` — a missing table, column or schema (`42P01`, `42703`, `3F000`). The names are what the conversion chose, so this one does blame the conversion; check the conversion report for that database.
- `ambiguous_order` — identical rows in a different order, from a gold query with no total order. Counted as agreement, and counted separately so it is visible.
- `undetermined_limit` — the gold `ORDER BY` ties across its `LIMIT`, so the question has several equally correct answers. Excluded from the denominator. **Not the same as `ambiguous_order`:** there the two engines return the same rows, here they return different ones, which is why one is counted as agreement and the other is excluded.

Do not "fix" this by relaxing the comparison. The comparator is the one the eval will score with, so a change here changes every number. The one relaxation that exists — `ambiguous_order` — lives in the verifier and deliberately not in `evals.comparison`, because there the two queries being compared are different ([ADR-027](../architecture/DECISIONS.md#adr-027--an-undetermined-result-order-is-not-a-mismatch--in-verification-only)).

### `permission denied for schema spider_x` — from the read-only role

The conversion did not grant, or `DB_READONLY_ROLE` names a role that does not exist. Migration 002 grants on `public` only; converted schemas are granted at conversion time. Re-run the conversion for that database rather than granting by hand — a hand-issued grant is not reproducible and tends to be wider than the loader's.

### `LLM_MODEL is required` from a command that uses no model

Should no longer happen: the loader composes only the settings groups it uses. If it reappears, something has started calling `Settings.load()` — the fix is to take the groups needed, not to set a fake `LLM_MODEL`, because a fake value in the environment outlives the command that needed it.

---

## Training

### CUDA out of memory

Reduce batch size — but note that with `MultipleNegativesRankingLoss`, **batch size is the number of negatives per example**, so changing it changes the task difficulty. A comparison across different batch sizes is not an ablation, it is two different experiments. Prefer gradient accumulation, and record the effective batch size either way. See [../ml/TRAINING.md](../ml/TRAINING.md) §6.

### Recall@k is suspiciously high

Check the split. If databases straddle train and eval, the model has memorized the corpus rather than learned to link, and the number is meaningless. Split by **database**, never by question — [../ml/DATASETS.md](../ml/DATASETS.md) §5.

### Fine-tuned model performs worse than baseline

A real possibility, and a publishable result ([ADR-006](../architecture/DECISIONS.md#adr-006--fine-tune-the-schema-linker-rather-than-retrieve-more-candidates)). Before concluding it, rule out:

1. `RETRIEVER_MODEL` mismatch — corpus not re-embedded with the new model. The recorded `model_version` comes from the embedder, so pointing `RETRIEVER_MODEL` at a checkpoint is what separates the vector spaces.
2. Overfitting on a small pair set — check the training curve.
3. Learning rate too high, degrading the pretrained representation.
4. Evaluating on the wrong split.

If all four are ruled out and it is still worse, record it in [../ml/BENCHMARKS.md](../ml/BENCHMARKS.md) and write up why in TRAINING.md.

---

## HTTP API

### `API_HOST='0.0.0.0' would serve this API beyond this machine`

Working as intended. The API has **no authentication**, so it refuses to bind anything but loopback ([ADR-034](../architecture/DECISIONS.md#adr-034--the-api-refuses-to-bind-beyond-loopback-while-it-has-no-authentication)).

If you are in a container: leave `API_HOST=127.0.0.1` and publish the port from the runtime — `-p 8000:8000`, or a Kubernetes Service. That forwards to loopback inside the network namespace and works unchanged.

If you are trying to reach it from another machine: put something in front that authenticates. There is nothing in the application to turn on.

All four spellings of loopback are accepted, so if you needed `::1` or `localhost` for an unrelated reason, use it.

### `API_CORS_ORIGINS must not contain '*'`

Name the origins. A wildcard on an API that reaches a database makes every page on the internet a client, and the usual mitigation — disabling credentials — is one refactor away from being undone.

### `/ready` returns 503 with `{"startup": "down"}`

The process is serving but its lifespan has not finished. Normal for the first moment after start; persistent means startup is blocked on a dependency, and the reason is on stderr.

### `/ready` returns 503 and says only `"down"`

By design — the reason is never in the response, because both probes are unauthenticated and a driver message carries the DSN, the internal hostname and the role name. **The cause is in the process log**, with the full traceback, correlated by the `request_id` in the response body and the `X-Request-Id` header.

### `/ready` says `up` but queries fail

The verdict is cached for 5 seconds. Also, `/ready` probes the two connections and nothing else — the catalog and retriever are loaded at startup and held, so a stale catalog after a migration to the target database will not show up here. Restart the process; the catalog is a point-in-time snapshot by design.

### The `X-Request-Id` I sent is not the one in the response

It failed the allowlist (`[A-Za-z0-9._:-]`, 1–128 chars) and was replaced rather than rejected. A newline in that header writes a second log line, so a value that cannot be repeated safely is not repeated. Trailing whitespace and newlines are the usual cause.

### `ModuleNotFoundError: No module named 'httpx2'` running the tests

`starlette.testclient` requires `httpx2`; it refuses `httpx` 0.x. `pip install -r requirements-dev.txt`.

## Container runtime

> Any Docker-compatible runtime works — Podman, Rancher Desktop, colima, or Docker Engine. `testcontainers` talks to whichever socket is present. Docker Desktop is convenient on Windows and is not required; see [DEPLOYMENT.md](DEPLOYMENT.md) §1.

### `exec format error` / `no matching manifest`

Architecture mismatch (arm64 image on amd64 or vice versa). Build for the local platform or use `--platform`.

### API container restart-loops on startup

Usually the database is not ready. `depends_on` alone waits for container start, not database readiness — it needs `condition: service_healthy` with a real healthcheck. See [DEPLOYMENT.md](DEPLOYMENT.md) §2.

### Migrations run several times concurrently

Migrations are being invoked from the API entrypoint with more than one replica. They belong in a separate one-shot service that the API depends on.
