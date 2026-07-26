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

## 5. Retrieval

| Variable | Type | Default | Notes |
|---|---|---|---|
| `RETRIEVER_MODEL_VERSION` | str | `baseline-v1` | Filters `schema_elements`; **vectors from different models are not comparable** |
| `RETRIEVER_MODEL_PATH` | str | — | Local checkpoint path for the fine-tuned model |
| `RETRIEVAL_TOP_K` | int | `10` | |
| `RETRIEVAL_TOP_K_CEILING` | int | `50` | |
| `HNSW_EF_SEARCH` | int | `64` | Recall/latency knob; swept in Stage 6 |
| `EMBEDDING_DEVICE` | enum | `auto` | `auto` / `cpu` / `cuda` |

**A `RETRIEVER_MODEL_VERSION` mismatched against the indexed vectors silently degrades retrieval** rather than erroring — the query embedding lands in a different vector space than the corpus. Startup validation checks that vectors exist for the configured version.

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
