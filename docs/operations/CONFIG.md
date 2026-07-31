# Configuration

> **Status: TBD — Stage 1** for the authoritative list. The variables below are the design intent; this page becomes canonical once `settings.py` exists.

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
| `DATABASE_URL` | SecretStr | — | **Required.** App role — owns `agent_meta`, runs migrations |
| `DATABASE_RO_URL` | SecretStr | — | **Required.** Read-only role used by `execute_sql` |
| `DB_POOL_MIN_SIZE` | int | `2` | |
| `DB_POOL_MAX_SIZE` | int | `10` | **This is what bounds concurrent load on the database** |
| `DB_CONNECT_TIMEOUT_MS` | int | `5000` | |
| `DB_TARGET_SCHEMA` | str | `public` | Schema questions are asked about |

**Two URLs, and they must be different roles.** If `DATABASE_RO_URL` resolves to a role with write privileges, every containment guarantee in [SECURITY.md](SECURITY.md) is void. Startup validation asserts the read-only role cannot write — a real check against the database, not a naming convention.

## 3. Execution limits

| Variable | Type | Default | Notes |
|---|---|---|---|
| `MAX_ROWS_DEFAULT` | int | `500` | Applied when the caller does not specify |
| `MAX_ROWS_CEILING` | int | `5000` | Hard cap; client requests are clamped to it |
| `STATEMENT_TIMEOUT_MS` | int | `30000` | Also set on the role in the database |
| `STATEMENT_TIMEOUT_CEILING_MS` | int | `60000` | Hard cap |
| `MAX_ESTIMATED_COST` | float | `1000000` | Bail out on `EXPLAIN` cost before executing |
| `WORK_MEM` | str | `32MB` | Per-connection sort memory |

`MAX_ESTIMATED_COST` is the cheap defence against expensive queries: the planner has already estimated cost during validation, so an obviously catastrophic query can be rejected without spending the execution budget on it. Calibrating the threshold is empirical — see [../architecture/DATABASE.md](../architecture/DATABASE.md) §9.

## 4. Agent

The agent depends on the `LLMClient` protocol, never on a vendor SDK ([ADR-014](../architecture/DECISIONS.md#adr-014--provider-agnostic-llm-behind-an-llmclient-port)). These variables select and configure the adapter.

| Variable | Type | Default | Notes |
|---|---|---|---|
| `LLM_PROVIDER` | enum | `openai_compatible` | `openai_compatible` / `anthropic` / `fake` |
| `LLM_BASE_URL` | str | — | Required for `openai_compatible`. **Operator-only — never client-controlled** (SSRF; see [SECURITY.md](SECURITY.md) §14.1) |
| `LLM_MODEL` | str | — | **Required.** Provider-specific model id |
| `LLM_API_KEY` | SecretStr | — | Required unless the endpoint is local (Ollama / LM Studio) |
| `LLM_MAX_TOKENS` | int | `16000` | |
| `LLM_TEMPERATURE` | float | `0.0` | Omitted for providers that reject it |
| `LLM_TIMEOUT_MS` | int | `60000` | |
| `LLM_SUPPORTS_TOOL_CALLING` | bool | `auto` | `auto` probes at startup; falls back to prompt-based structured output |
| `LLM_EFFORT` | enum | — | Anthropic adapter only; ignored elsewhere |
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

Nearly all of the 120b's output is internal reasoning. Two consequences: budget roughly **2× per question** against a daily cap, and **never set `LLM_MAX_TOKENS` low** — a reasoning model whose budget is spent thinking returns an *empty string with no error*. The generator detects that case and names it, rather than reporting "empty response".

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

**`SCHEMA_SAMPLE_VALUES` defaults to off and should stay off unless every column in the schema has been audited.** Sampling copies real rows into `schema_elements.serialized`, and that text is quoted into prompts sent to a third-party model — a path the read-only role does not protect, because the data is read legitimately and then transmitted. Unlike `profile_table`, catalog samples are *persisted* and re-sent on every retrieval hit, and they do not appear in the audit log. Full analysis in [SECURITY.md](SECURITY.md) §14.2.1. For sensitive data the supported configuration is local inference.

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

## 6. API

| Variable | Type | Default | Notes |
|---|---|---|---|
| `HOST` | str | `127.0.0.1` | **Localhost by default — deliberate** |
| `PORT` | int | `8000` | |
| `API_KEY` | SecretStr | — | Required when `HOST` is not loopback |
| `CORS_ORIGINS` | list[str] | `[]` | Empty = no cross-origin access |
| `MAX_QUESTION_LENGTH` | int | `2000` | |
| `RATE_LIMIT_PER_MINUTE` | int | `30` | Per client |
| `MAX_CONCURRENT_STREAMS` | int | `10` | Global |
| `SSE_KEEPALIVE_MS` | int | `15000` | Prevents proxy idle-timeouts |

Binding to `0.0.0.0` without `API_KEY` set is a **startup error**, not a warning. An unauthenticated endpoint that runs LLM-generated SQL and bills tokens should not be reachable by accident.

## 7. Observability

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

## 8. Feature flags

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

> **TBD — Stage 1.** Committed at the repo root with every variable above, placeholder values, and inline comments. Never contains a real secret.
