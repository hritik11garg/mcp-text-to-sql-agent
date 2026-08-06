# Configuration

> **Status: canonical for everything marked *implemented*.** `src/core/settings.py` exists and every section headed *implemented* below tracks it exactly, including ranges and defaults. Sections headed *planned* are still design intent and carry the stage that fills them.

All configuration comes from environment variables, loaded and validated by `pydantic-settings`. Invalid configuration fails at **startup**, not on the first request — a service that boots with a bad row limit and discovers it under load has no useful failure mode.

---

## 1. Principles

1. **No configuration in code.** Anything that differs between dev and production is an environment variable.
2. **Typed and validated at startup.** Ranges, enums, and required-ness are enforced by the settings model.
3. **Secrets are `SecretStr`** so accidental `repr()` or log interpolation does not leak them.
4. **Every limit has a server-side maximum.** A client can request a smaller row limit; it cannot request a larger one than the server allows. Client-supplied values are clamped, never trusted.
5. **`.env` is gitignored.** `.env.example` carries placeholders only and is committed.

## 2. Database

| Variable | Type | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | SecretStr | — | **Required.** App role — owns `agent_meta`, runs migrations. Written in SQLAlchemy's `postgresql+psycopg://` form |
| `DATABASE_RO_URL` | SecretStr | — | **Required.** Read-only role used by `execute_sql`. Same form |
| `DB_POOL_MIN_SIZE` | int | `2` | |
| `DB_POOL_MAX_SIZE` | int | `10` | **This is what bounds concurrent load on the database** |
| `DB_CONNECT_TIMEOUT_MS` | int | `5000` | |
| `DB_TARGET_SCHEMA` | str | `public` | Schema questions are asked about |
| `DB_READONLY_ROLE` | str | `sql_agent_ro` | The group role migration 002 creates. Used by the benchmark loader to grant `SELECT` on the schemas it creates |

**`DB_TARGET_SCHEMA` is one schema, and the benchmark loader creates many.** Each benchmark database becomes its own schema (`spider_concert_singer`), so evaluating a question means pointing the servers at its database — `DB_TARGET_SCHEMA=spider_concert_singer`. Wiring that per question is Stage 2's remaining work; today it is set per run.

**`DB_READONLY_ROLE` exists because migration 002 grants on `public` only.** A schema created afterwards is invisible to the read-only role until granted, so the loader has to name the role. It is validated as an identifier before it reaches a `GRANT` — quoting answers "how is this written", not "may this be named" ([ADR-017](../architecture/DECISIONS.md#adr-017--servers-claim-stdout-and-validate-arguments-themselves)).

**Two URLs, and they must be different roles.** If `DATABASE_RO_URL` resolves to a role with write privileges, every containment guarantee in [SECURITY.md](SECURITY.md) is void. Startup validation asserts the read-only role cannot write — a real check against the database, not a naming convention.

**Both are SQLAlchemy URLs, and psycopg cannot parse one.** The `+psycopg` suffix is required by alembic and rejected by `psycopg.connect`. There is deliberately no second variable holding the other form — two ways to say one thing can disagree, and the one that is wrong is found by whichever tool runs second. `core.dsn.libpq_dsn()` converts at every psycopg call site instead ([ADR-028](../architecture/DECISIONS.md#adr-028--one-connection-string-form-per-consumer-converted-at-the-driver)). A new call site that skips it fails at startup with `invalid connection option`.

**These values reach error messages, so they are redacted where errors are built, not where they are logged.** psycopg quotes the whole connection string in its parse errors; `SecretStr` does nothing about that, because the string has already been handed to the driver. `core.dsn.redact_dsn()` masks the password in anything derived from a driver exception — see [SECURITY.md](SECURITY.md) §14.2.10, which exists because this leaked a live password to a terminal before it was fixed.

## 3. Execution limits

| Variable | Type | Default | Notes |
|---|---|---|---|
| `MAX_ROWS_DEFAULT` | int | `500` | Applied when the caller does not specify |
| `MAX_ROWS_CEILING` | int | `5000` | Hard cap; client requests are clamped to it |
| `STATEMENT_TIMEOUT_MS` | int | `30000` | Also set on the role in the database |
| `STATEMENT_TIMEOUT_CEILING_MS` | int | `60000` | Hard cap |
| `MAX_ESTIMATED_COST` | float | `1000000` | Bail out on `EXPLAIN` cost before executing |
| `WORK_MEM` | str | `32MB` | Per-connection sort memory. **Not an environment variable** — set as a role attribute by migration 002, so it cannot be raised by a caller |

`MAX_ESTIMATED_COST` is the cheap defence against expensive queries: the planner has already estimated cost during validation, so an obviously catastrophic query can be rejected without spending the execution budget on it.

**Calibrate it for your database. The shipped default is sized for benchmark data and will refuse ordinary work on a real one.** Planner cost units are not seconds, do not convert to seconds, and do not transfer between machines, datasets, or PostgreSQL versions — so there is no default that is correct everywhere, and `1000000` is correct for tables of thousands of rows. Against millions, a single sequential scan can approach it and an ordinary analytical join exceeds it, so legitimate questions come back as `cost_exceeded` and the operator sees a system that refuses to answer.

The procedure, which takes about five minutes:

1. Write the slowest query you consider acceptable to run interactively.
2. `EXPLAIN (FORMAT JSON) <query>` and read `Total Cost` from the top plan node.
3. Set the ceiling above it, with headroom for a plan that changes when statistics do.
4. Re-check after any significant change in data volume — the ceiling is absolute while the plans it judges are not.

Setting it *too high* is not free either: the ceiling is what stops a query from occupying a connection until `STATEMENT_TIMEOUT_CEILING_MS` fires. Both bounds are real, which is the same shape as `LLM_MAX_TOKENS` in §4 — a limit with a failure mode on each side, where only one of them was documented.

A cost ceiling is a blunt instrument by design. Routing an expensive query to a background job instead of refusing it is [FUTURE.md](../project/FUTURE.md) § *Two-tier execution*; the estimate this setting compares against is already the signal that would do the routing.

## 4. Agent

The agent depends on the `LLMClient` protocol, never on a vendor SDK ([ADR-014](../architecture/DECISIONS.md#adr-014--provider-agnostic-llm-behind-an-llmclient-port)). These variables select and configure the adapter.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `LLM_PROVIDER` | enum | `openai_compatible` | `openai_compatible` / `fake`. `anthropic` is accepted by the enum and **raises at startup** — the adapter is unbuilt and its SDK is not installed |
| `LLM_BASE_URL` | str | — | Required for `openai_compatible`. **Operator-only — never client-controlled** (SSRF; see [SECURITY.md](SECURITY.md) §14.1) |
| `LLM_MODEL` | str | — | **Required.** Provider-specific model id |
| `LLM_API_KEY` | SecretStr | — | Required unless the endpoint is local (Ollama / LM Studio) |
| `LLM_MAX_TOKENS` | int | `16000` | Above some free tiers' completion cap — see below. `4096` works on Groq |
| `LLM_TEMPERATURE` | float | `0.0` | Omitted for providers that reject it |
| `LLM_TIMEOUT_MS` | int | `60000` | |
| `LLM_ALLOWED_HOSTS` | list[str] | `[]` | Optional host allowlist for `LLM_BASE_URL`. Empty means "any host that passes the IP checks". Comma-separated |
| `LLM_MODEL_FALLBACKS` | list[str] | `[]` | Models tried in order on a 429. Same provider, same key — see §4.2. Comma-separated |
| `LLM_SUPPORTS_TOOL_CALLING` | bool | `auto` | *Planned.* Today the capability is a property of the `LLMClient` adapter, not a setting |
| `LLM_EFFORT` | enum | — | *Planned.* Anthropic adapter only, and that adapter is not built |
| `MAX_TOOL_CALLS_PER_REQUEST` | int | `20` | **Hard stop on agent loops** |
| `MAX_SQL_RETRIES` | int | `3` | Self-correction budget per query |
| `MAX_DECOMPOSITION_STEPS` | int | `5` | Sub-questions per compound question |
| `AGENT_TIMEOUT_MS` | int | `120000` | Wall clock for one request |

**`MAX_TOOL_CALLS_PER_REQUEST` is not optional.** A self-correcting agent that fails to converge retries until something stops it. That something must be a counter.

`LLM_PROVIDER` and `LLM_MODEL` are configurable specifically so the eval harness can sweep them. **Accuracy is reported per provider/model**, never as a single number — free-tier model quality varies enormously, and a benchmark row without the model that produced it is meaningless.

### Working `openai_compatible` configurations

| Provider | `LLM_BASE_URL` | Key needed | Notes |
|---|---|---|---|
| Groq | `https://api.groq.com/openai/v1` | free tier | Very fast; good first choice |
| OpenRouter | `https://openrouter.ai/api/v1` | free tier | Several `:free` models |
| Cerebras | `https://api.cerebras.ai/v1` | free tier | |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | free tier | OpenAI-compat endpoint |
| **Ollama (local)** | `http://localhost:11434/v1` | **none** | Fully offline — no data leaves the machine |
| LM Studio (local) | `http://localhost:1234/v1` | none | Offline |

Verify current free-tier terms yourself before use — they change, and §14.2 of [SECURITY.md](SECURITY.md) explains why the terms matter here specifically.

### Worked example: Groq (or any OpenAI-compatible provider)

One adapter covers every provider that exposes `/chat/completions`; they differ by `base_url` and nothing else (ADR-014).

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=<a model id from the provider's console>
LLM_API_KEY=<your key>
```

Other providers, same four lines: OpenRouter `https://openrouter.ai/api/v1`, Cerebras `https://api.cerebras.ai/v1`, Gemini `https://generativelanguage.googleapis.com/v1beta/openai`, local Ollama `http://localhost:11434/v1`, LM Studio `http://localhost:1234/v1`.

Add a fallback chain so a spent daily cap does not end a run:

```dotenv
LLM_MODEL_FALLBACKS=qwen/qwen3.6-27b,llama-3.3-70b-versatile,llama-3.1-8b-instant
```

Comma-separated, in preference order, **same provider and same key** — this is a model chain, not a provider chain. Switching provider stays a `LLM_BASE_URL` change, deliberately: a second endpoint means a second credential and a second SSRF check, which is not configuration a running process should improvise.

**Verify before you depend on it:**

```powershell
python -m generation.check
python -m generation.check --model llama-3.1-8b-instant
```

One round trip; reports which model answered, latency, tokens, and cached tokens. It exists so a bad key, a retired model id, or a spent cap surfaces *here* rather than halfway through a benchmark run, where it looks like an agent bug.

**Model ids change.** Providers rename and retire them, so take the id from the provider's own model list rather than from any document — including this one. A wrong id fails on the first request with a provider error, not at startup.

**Reasoning models cost far more output tokens than they appear to.** Measured on Groq with the `sql_gen` prompt at k=10:

| Model | Input | Output | Total per question |
|---|---|---|---|
| `openai/gpt-oss-120b` | 515 | 464 | **979** |
| `llama-3.1-8b-instant` | 474 | 88 | **562** |

Nearly all of the 120b's output is internal reasoning. Budget roughly **2× per question** against a daily cap.

**`LLM_MAX_TOKENS` is bounded from both sides, and the default of 16000 is above the ceiling on at least one free tier.** Set it too low and a reasoning model spends its whole budget thinking, returning an *empty string with no error* — the generator detects that case and names it. Set it above what the endpoint allows and the request is refused with **HTTP 413** before the model sees it; measured on Groq with `openai/gpt-oss-120b`, 6000 is accepted and 8192 is not. `4096` is a working value for that combination and is what `.env.example` now suggests.

Neither failure is self-evident from the symptom, which is why the adapter maps HTTP statuses to what they mean rather than reporting "the model provider could not be reached" — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

**Several open-weight models emit their reasoning in the `content` field**, as `<think>…</think>` followed by the answer. The generator strips a leading block; a model that emits one *and* is truncated mid-thought produces no answer at all, which is the low-`LLM_MAX_TOKENS` case above wearing a different costume.

**Free tiers have daily token caps**, and hitting one on a benchmark run is normal rather than exceptional. Two consequences worth planning for: the eval harness must be resumable, and [BENCHMARKS.md](../ml/BENCHMARKS.md) records accuracy **per provider and model**, never as a single number — a run split across two models is two rows, not an average.

**The key is read once, from the environment, at startup.** It is never logged, never included in an error message, and never populated from a request. `LLM_BASE_URL` is SSRF-checked before use — see [SECURITY.md](SECURITY.md) §14.1 — which is why a local `http://` endpoint is permitted but a private-range one is not.

## 5. Retrieval

### Catalog and embedder — implemented

| Variable | Type | Default | Notes |
|---|---|---|---|
| `DATASET` | str | `default` | Namespace for catalog rows, so several schemas can share one database |
| `EMBEDDER_PROVIDER` | enum | `sentence_transformer` | `sentence_transformer` / `hashing` |
| `RETRIEVER_MODEL` | str | `sentence-transformers/all-MiniLM-L6-v2` | Hub id **or** a local checkpoint path. 384 dimensions, matching the vector column |
| `RETRIEVER_LOCAL_FILES_ONLY` | bool | `false` | Refuse to download. Set `true` in CI and air-gapped deployments so an implicit fetch fails loudly |

**There is no `RETRIEVER_MODEL_VERSION` setting, deliberately.** The version recorded on every row comes from the embedder itself — for `sentence_transformer` it *is* `RETRIEVER_MODEL`. A separately configured label could disagree with the model that actually produced the vectors, and that disagreement is silent: retrieval keeps returning k results, they are just meaningless. Pointing `RETRIEVER_MODEL` at a fine-tuned checkpoint therefore keeps the two vector spaces separable automatically.

`hashing` is a dependency-free stand-in for tests and offline work. It has no semantic understanding, and its version string (`hashing-trigram-384`) says so — a catalog indexed with it cannot be searched with a real model by accident.

**A model mismatched against the indexed vectors silently degrades retrieval** rather than erroring, because the query embedding lands in a different vector space than the corpus. `assert_catalog_ready` refuses startup when the configured model has no vectors indexed.

### Schema sampling — implemented, and off by default

| Variable | Type | Default | Notes |
|---|---|---|---|
| `SCHEMA_SAMPLE_VALUES` | bool | **`false`** | **Leave off** — see below |
| `SCHEMA_SAMPLE_COUNT` | int | `3` | Distinct values kept per column |
| `SCHEMA_SAMPLE_MAX_CHARS` | int | `40` | Truncation applied in SQL, not after fetching |
| `SCHEMA_SAMPLE_SCAN_LIMIT` | int | `1000` | Rows examined per column; bounds indexing cost |
| `SCHEMA_EXTRA_SENSITIVE_COLUMNS` | list[str] | `[]` | Added to the built-in denylist, never replacing it |

**`SCHEMA_SAMPLE_VALUES` defaults to off and should stay off unless every column in the schema has been audited.** Sampling copies real rows into `schema_elements.serialized` — a persistent copy of production data in a second table, which the read-only role does not protect because the data is read legitimately. Those values do **not** reach the model: the prompt is built from column name, type and comment, never from the serialized string ([SECURITY.md](SECURITY.md) §14.2.5, pinned by test). What sampling buys is retrieval quality; what it costs is a persisted copy that does not appear in the audit log. Full analysis in [SECURITY.md](SECURITY.md) §14.2.1. For sensitive data the supported configuration is local inference.

### Retrieval tuning — implemented

| Variable | Type | Default | Notes |
|---|---|---|---|
| `RETRIEVAL_TOP_K` | int | `10` | Elements per search when the caller does not ask for a count. Range 1–50 |
| `HNSW_EF_SEARCH` | int | `40` | Recall/latency knob, range 1–1000; swept in Stage 6. Raised to `k` automatically when a caller asks for more than this |

**There is no `RETRIEVAL_TOP_K_CEILING` setting.** The hard ceiling is `MAX_K = 50` in `schema.retrieval`, because it has to match the published `schema_search` tool schema in [../architecture/MCP.md](../architecture/MCP.md) §3.1 — a ceiling an operator can raise past the contract is not a ceiling. Every caller-supplied `k` is clamped there regardless of configuration, so `RETRIEVAL_TOP_K` is bounded twice: once at the configuration edge for operator typos, once at request time for hostile callers.

**`hnsw.iterative_scan` is not configurable and is deliberately always on.** Turning it off makes searches faster and silently returns fewer results than asked for — a correctness setting wearing a performance setting's clothes. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §5.1 for the measurements.

### Retrieval tuning — planned (Stage 6)

| Variable | Type | Default | Notes |
|---|---|---|---|
| `EMBEDDING_DEVICE` | enum | `auto` | `auto` / `cpu` / `cuda` |

## 5a. Table profiling — implemented

Profiling is the one component whose **output is row data by design** — it exists to be shown to a model so it can write a correct `WHERE` clause. Read [SECURITY.md](SECURITY.md) §14.2.6 before changing any default here.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `PROFILE_ALLOW_VALUE_SAMPLING` | bool | **`false`** | Whether raw cells may be returned at all. **The one that matters** |
| `PROFILE_SAMPLE_ROWS` | int | `5` | Rows returned when sampling is allowed. Range 0–20, matching the tool schema. Ignored when it is not |
| `PROFILE_MIN_VALUE_FREQUENCY` | int | `5` | A value must occur this often before it may be reported. **Floor of 2 in the type** |
| `PROFILE_TOP_K` | int | `5` | Frequent values per column, range 0–20. `0` disables them |
| `PROFILE_SCAN_LIMIT` | int | `5000` | Rows read per column. Bounds cost, and bounds disclosure with it |
| `PROFILE_MAX_COLUMNS` | int | `30` | Columns per call, range 1–200. A wide table is truncated and told so, not refused |
| `PROFILE_MAX_VALUE_CHARS` | int | `40` | Applied in SQL (`left(col::text, n)`), so a wide cell is never held in full |
| `PROFILE_TIMEOUT_MS` | int | `10000` | Deliberately shorter than `STATEMENT_TIMEOUT_MS` |

**`PROFILE_MIN_VALUE_FREQUENCY` is the control doing the real work, and it is the one to raise on sensitive data.** Frequent values are on by default and raw sampling is off, which sounds inconsistent until you look at what each returns: a value occurring 500 times is a category label, a value occurring once is a record. The threshold is the standard small-cell rule from statistical disclosure control, and it is the only gate that catches a secret sitting in an innocuously-named column — the case the name-based denylist is openly admitted to miss.

**Raising it costs disambiguation quality, not correctness.** A column whose values all fall below the threshold reports *why* they were withheld, so the agent picks a different strategy rather than concluding the column is empty.

**`PROFILE_ALLOW_VALUE_SAMPLING` should stay off unless every column being profiled has been audited by name.** Turning it on does not disable the denylist or the frequency threshold — those still apply. What it adds is verbatim cells, truncated and capped, for the cases where the *format* of a value matters and a summary cannot convey it.

**There is no setting that widens which tables may be profiled.** The catalog is the allowlist, and a name not in it is rejected before any statement is composed. That is a containment boundary, not a convenience — see [SECURITY.md](SECURITY.md) §14.2.6, control 1.

## 5b. Benchmark loader — implemented

Read only by `python -m benchmark.load`. Nothing here is reachable from a request; every one of them is a limit on how much damage a hostile archive or a pathological source database can do before the loader gives up.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `BENCHMARK_DATA_DIR` | Path | `data` | Where archives are extracted. Gitignored except the lockfile and the splits |
| `BENCHMARK_MAX_ARCHIVE_BYTES` | int | 8 GiB | Total **decompressed** bytes from one archive |
| `BENCHMARK_MAX_ARCHIVE_MEMBERS` | int | `200000` | Member count ceiling |
| `BENCHMARK_MAX_MEMBER_BYTES` | int | 4 GiB | Per-member ceiling |
| `BENCHMARK_COPY_BATCH_ROWS` | int | `5000` | Bounds loader memory during `COPY` |
| `BENCHMARK_VERIFY_TIMEOUT_MS` | int | `30000` | `statement_timeout` for each gold query during verification |

**The byte caps are enforced against bytes written, never against the sizes the archive declares.** A zip's directory is attacker-controlled: a 42-byte bomb declares whatever it likes. Raising them to accommodate a large benchmark is fine; the point is that the limit is a real one.

**There is no setting for how many rows type inference reads, because it does not read rows.** SQLite is asked directly, with `group_concat(DISTINCT typeof(col))`, which answers exactly and over the whole column in one scan per table. The previous sampling cap was a real defect rather than a tuning knob: Spider's `wta_1.rankings` has 510,437 rows and exactly one empty-string `player_id` past row 1.5 million, so a 200,000-row sample inferred `bigint` and the load died on that single value.

**There is no variable that points the loader at a URL.** Sources are an allowlist in `benchmark/sources.py`; see [DATASETS.md](../ml/DATASETS.md) §8.

## 6. API

The HTTP layer exists as of v0.1: `create_app()`, `/health`, `/ready`, the error envelope, request correlation, and **`POST /v1/query` non-streaming**. SSE and sessions do not — see [API.md](../architecture/API.md) for which parts of that contract are served today.

### Implemented

| Variable | Type | Default | Notes |
|---|---|---|---|
| `API_HOST` | str | `127.0.0.1` | **Loopback, enforced at startup** — see below |
| `API_PORT` | int | `8000` | |
| `API_DOCS_ENABLED` | bool | **`false`** | Serves `/docs`, `/redoc` **and** `/openapi.json` |
| `API_CORS_ORIGINS` | csv | `[]` | Empty = no cross-origin access. `*` is refused at startup |
| `API_MAX_BODY_BYTES` | int | `65536` | Request body cap, enforced **before** parsing. 1 KiB–10 MiB |
| `API_MAX_QUESTION_CHARS` | int | `2000` | Longest question. Bounds prompt cost, not memory |
| `API_MAX_CONCURRENT_REQUESTS` | int | `4` | In-flight questions across all callers. Over it: `429`, immediately |
| `API_POOL_MIN_SIZE` | int | `1` | Read-only pool floor |
| `API_POOL_MAX_SIZE` | int | `8` | Read-only pool ceiling. **Must exceed `API_MAX_CONCURRENT_REQUESTS`** |

**Binding beyond loopback is a startup error, not a warning.** This page has said so since Stage 0, conditioned on `API_KEY`; the honest form today is stricter, because `API_KEY` does not exist yet. There is no authentication of any kind, so no non-loopback bind address is safe, and the process refuses to start on one. All four spellings of loopback are accepted (`127.0.0.1`, `127.0.0.2`, `::1`, `localhost`) so that nobody has to work around the control to get a legitimate configuration running.

To deploy: publish the port from your container runtime (`-p 8000:8000`) or put a reverse proxy in front that authenticates. Both leave the decision to expose the service with whoever is deploying it, rather than with a default.

`API_DOCS_ENABLED` governs all three documentation routes together, `openapi.json` included. Clearing only `docs_url` hides the rendered page while leaving the machine-readable route map reachable, which helps nobody except somebody enumerating the service.

**The pool must be larger than the request cap, and startup refuses it otherwise.** Sized equal, saturated traffic holds every connection and leaves none for the readiness probe — so `/ready` fails *because* the service is busy, the orchestrator pulls the replica out of rotation, and its traffic moves to the replicas that are also busy. Load-induced failure that removes capacity is the shape that turns a spike into an outage, and it is two numbers apart.

**`DB_TARGET_SCHEMA` and `DATASET` must describe the same schema.** Generated SQL names bare tables, and `EXPLAIN` resolves them through the *session's* search path. Set them apart and the query validates against one schema and executes against another — which is the only failure in this section that returns a **plausible answer from the wrong tables** rather than an error. Left at `public` on a benchmark deployment, every question instead fails identically with "relation does not exist", for correct and hallucinated SQL alike, so the invalid-query rate becomes an artifact of wiring. The eval harness scopes this per question; a serving process has one dataset and scopes once, on every read-only session it opens.

**The body cap and the question cap are not redundant.** The first bounds what is *read* and is enforced before parsing, because parsing is where an unauthenticated caller gets to decide how much this process allocates. The second bounds what reaches the model, where the cost is tokens. A caller can exhaust either without touching the other.

**Over-limit requests are refused, not queued.** A queue converts an overload into latency that every caller waits out, including the ones who arrived first; a `429` is a fact the caller can act on. The default of 4 comes from the provider's requests-per-minute rather than from anything about this process.

### Planned

| Variable | Type | Default | Notes |
|---|---|---|---|
| `API_KEY` | SecretStr | — | Lands with authentication; until then, loopback is the control |
| `MAX_QUESTION_LENGTH` | int | `2000` | With `POST /v1/query` |
| `MAX_REQUEST_BYTES` | int | `65536` | Required **before** the first endpoint that accepts a body |
| `RATE_LIMIT_PER_MINUTE` | int | `30` | Per client |
| `MAX_CONCURRENT_STREAMS` | int | `10` | Global |
| `MAX_IN_FLIGHT_PER_CLIENT` | int | `2` | The `429` in API.md; nothing emits it yet |
| `SSE_KEEPALIVE_MS` | int | `15000` | Prevents proxy idle-timeouts |

## 7. Observability — planned (Stage 6)

> None of these exist yet. Logging today is `logging.basicConfig` at the server entrypoints, forced onto stderr.
>
> They are **not** in `.env.example` as assignments, only named in a comment there — see §10. Until Stage 6, setting any of them has no effect.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `OTEL_ENABLED` | bool | `false` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | str | — | Required when enabled |
| `OTEL_SERVICE_NAME` | str | `text-to-sql-agent` | |
| `OTEL_TRACES_SAMPLER_ARG` | float | `1.0` | Sample everything in dev; lower in production |
| `LOG_LEVEL` | enum | `INFO` | |
| `LOG_FORMAT` | enum | `json` | `json` / `console` |
| `LOG_SQL` | bool | `true` | Logs generated SQL |
| `LOG_RESULT_VALUES` | bool | **`false`** | **Leave off** — see below |

**`LOG_RESULT_VALUES` defaults to off and should stay off.** Logging result values copies the data the read-only role is protecting into a second store with different retention and access controls. Useful for local debugging; a data-exposure vector anywhere else.

## 8. Feature flags — planned (Stage 4)

> **None of these exist yet, and one of them reads like a safety control.** `EXPLAIN_ONLY_MODE` is described below as "generate + validate, never execute" — setting it today does nothing, and `execute_sql` will execute. The control that actually stops execution is the read-only role plus not running the `execute_sql` server at all. Flagged here rather than left to be discovered.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `ENABLE_DECOMPOSITION` | bool | `true` | Multi-step planning (Stage 4) |
| `ENABLE_SELF_CORRECTION` | bool | `true` | Error-feedback retry loop |
| `ENABLE_PROFILE_TABLE` | bool | `true` | Disambiguation via column stats |
| `ENABLE_PROMPT_CACHING` | bool | `true` | |
| `EXPLAIN_ONLY_MODE` | bool | `false` | Generate + validate, never execute |

Flags exist for **ablations**, not accumulated indecision. Each maps to a row in the [../ml/EVALUATION.md](../ml/EVALUATION.md) baseline table — flipping one off must produce a measurable, explainable difference. A flag that cannot be justified that way gets deleted.

`EXPLAIN_ONLY_MODE` doubles as the degraded mode when `execute_sql` is unavailable.

## 9. MCP

### Implemented

The four servers are launched over **stdio** by an MCP host and read no `MCP_*` variables at all. They read the database, dataset and profiling settings above, exactly as any other component does. Host configuration — the `claude_desktop_config.json` block and the four mistakes that produce confusing failures — is in [../architecture/MCP.md](../architecture/MCP.md) §9.

They do **not** need an LLM key: they are called *by* a model and never call one, so `LLM_PROVIDER=fake` is a valid configuration for running them under a host.

### Planned — HTTP transport

> None of these exist yet. They land with the Streamable HTTP transport, which lands with the API layer — a network-reachable `execute_sql` needs authentication before it needs configuration.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `MCP_TRANSPORT` | enum | `stdio` | `stdio` / `http` |
| `MCP_SCHEMA_SEARCH_URL` | str | — | Required when transport is `http` |
| `MCP_VALIDATE_SQL_URL` | str | — | |
| `MCP_EXECUTE_SQL_URL` | str | — | |
| `MCP_PROFILE_TABLE_URL` | str | — | |
| `MCP_CONNECT_TIMEOUT_MS` | int | `5000` | |
| `MCP_CALL_TIMEOUT_MS` | int | `60000` | Must exceed `STATEMENT_TIMEOUT_CEILING_MS` |

If `MCP_CALL_TIMEOUT_MS` is below the statement-timeout ceiling, the MCP call gives up before the database does — producing a confusing timeout that looks like an MCP fault. Validated at startup.

## 10. `.env.example`

> **Implemented.** Committed at the repo root with placeholder values and inline comments. Never contains a real secret.

**Its coverage of this document is asserted, not reviewed.** `tests/unit/test_settings.py` enumerates every field on every settings class and fails if one is absent from either `.env.example` or this file. A commented-out `# NAME=value` counts as documented — several settings are shown that way precisely because their default should not be edited casually. What is refused is silence.

The test exists because this drifted: **18 of 50 settings had reached the code without reaching the template**, including `RETRIEVAL_TOP_K`, which is worth 30 points of execution accuracy, and `PROFILE_ALLOW_VALUE_SAMPLING` and `LLM_ALLOWED_HOSTS`, which are security controls. Safe defaults are what made the gap invisible — nothing broke, so nothing complained. A control an operator cannot discover is a control they cannot reason about, which is a weaker property than being correctly configured by accident.

**And the reverse is asserted too, because the reverse is worse.** A second test refuses any `NAME=` in `.env.example` that no settings class reads. A missing setting is invisible; a **dead** one is worse than invisible, because an operator sets it, gets no error, and concludes it took effect.

That had also happened. `LOG_LEVEL`, `LOG_FORMAT` and `LOG_RESULT_VALUES` sat in the template uncommented, with values, while nothing in the codebase read any of them — and §7 of this document correctly called them planned. The two files disagreed in the dangerous direction: the one operators actually edit was the one implying the controls worked.

`LOG_RESULT_VALUES=false` is why that is a security finding rather than untidiness. It carried a comment describing what it protects, so a reader would conclude result logging was off **by policy**. It is off because the feature does not exist — a different fact, and one that stops being true the moment somebody adds one.

Planned settings are therefore named in `.env.example` **in prose only**, never as an assignment. There is nothing to uncomment. Five variables legitimately appear in the template while no settings class reads them — four `POSTGRES_*` consumed by `docker-compose.yml` and `SQL_AGENT_RO_PASSWORD` read by migration 002 via `os.environ`; they sit in an explicit allowlist that names each consumer, and a second test fails if an allowlist entry stops appearing in the template, so the list cannot quietly outlive its reason.
