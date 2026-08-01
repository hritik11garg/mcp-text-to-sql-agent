# Deployment

> **Status: TBD — Stage 6.** Structure and constraints below are the plan; Dockerfiles, Compose files, and verified commands land with the hardening stage.

Two deployment shapes, with different security postures:

| Shape | Transport | Auth | Audience |
|---|---|---|---|
| **MCP host** (any stdio host) | stdio subprocesses | Host-managed, user privileges | The "point it at your own database" story |
| **HTTP service** | Streamable HTTP + SSE | API key required | Demo / production-style deployment |

The MCP-host shape is the one that makes this project runnable by other people, so it gets the more polished setup path.

**The first shape works today.** The four servers run over stdio and the host configuration is in [../architecture/MCP.md](../architecture/MCP.md) §9 — no Docker image and no HTTP endpoint required, because the host launches them as subprocesses with the environment it is given. The rest of this page is about the second shape, which needs the Streamable HTTP transport and the API layer, neither of which is built.

There is a reason that ordering is not accidental: an HTTP-reachable `execute_sql` is a different risk class from a subprocess a host launched, and it needs authentication before it needs a Dockerfile.

---

## 1. Docker

> **TBD — Stage 6.**

Planned: multi-stage build on `python:3.12-slim` ([ADR-010](../architecture/DECISIONS.md#adr-010--python-312)).

Constraints to design for rather than discover:

- **Image size.** `torch` dominates. The CPU-only wheel index cuts the image by well over a gigabyte, and inference does not need CUDA. GPU is a separate build target used for training.
- **Model weights.** Baking the sentence-transformer into the image makes it large but removes a cold-start download and a runtime network dependency. Mounting it keeps the image small but adds a volume. Decision **TBD**, recorded in DECISIONS.md when made.
- **Non-root user.** Container runs as an unprivileged user; nothing in the runtime path needs root.
- **No build toolchain in the runtime layer.** Every pinned dependency ships a wheel, so the final stage carries no compiler.
- **Read-only root filesystem** where possible, with explicit writable mounts.

## 2. Docker Compose

> **TBD — Stage 6.**

Services planned:

| Service | Purpose |
|---|---|
| `postgres` | Postgres 16 + pgvector, initialized with roles and extensions |
| `migrate` | One-shot Alembic run; the API waits on its completion |
| `api` | FastAPI + agent |
| `mcp-*` | The four MCP servers, when running over HTTP transport |
| `otel-collector` | Optional; traces to a local backend |

Two points that matter:

- **Migrations are a separate one-shot service**, not an entrypoint hook in the API container. Running migrations from N replicas racing each other is a reliable way to corrupt schema state.
- **`depends_on` with `condition: service_healthy`.** Container-started is not database-ready; without a real healthcheck the API starts, fails to connect, and restart-loops.

The read-only role is created by a migration, not by an init script, so the security posture is reproducible from a clean database and testable in CI. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §7–8.

## 3. Environment variables

Full reference: [CONFIG.md](CONFIG.md).

Production-specific requirements:

| Variable | Production value | Reason |
|---|---|---|
| `HOST` | `0.0.0.0` | Container networking |
| `API_KEY` | **Set** | Startup fails without it when not on loopback |
| `LOG_FORMAT` | `json` | Machine-parseable |
| `LOG_RESULT_VALUES` | `false` | Never log result data outside local debugging |
| `OTEL_ENABLED` | `true` | |
| `OTEL_TRACES_SAMPLER_ARG` | `<1.0` | Full sampling is a dev setting |
| `DATABASE_RO_URL` | Read-only role | Verified at startup, not assumed |

Secrets come from the orchestrator's secret store, not from a `.env` file baked into the image.

## 4. Production setup

> **TBD — Stage 6.**

Checklist to be verified, not just written:

- [ ] `DATABASE_RO_URL` role cannot write — proven by the negative tests in [../development/TESTING.md](../development/TESTING.md)
- [ ] `agent_meta` not readable by the read-only role
- [ ] `API_KEY` set; unauthenticated access impossible
- [ ] Rate limits active (request, token, and concurrent-stream)
- [ ] Statement timeout set on the role *and* per transaction
- [ ] Connection pool sized against database capacity, not application concurrency
- [ ] Vectors indexed for the configured `RETRIEVER_MODEL_VERSION`
- [ ] Audit logging on and writable
- [ ] Secrets from a secret store; none in the image or in logs
- [ ] Health and readiness probes wired to the orchestrator

## 5. Scaling

> **TBD — Stage 6**, with load-test numbers.

- **API/agent is stateless** — sessions live in Postgres, so replicas scale horizontally.
- **The database is the shared bottleneck.** Total concurrent queries = replicas × `DB_POOL_MAX_SIZE`. Scaling replicas without accounting for this scales load onto a single database.
- **SSE holds a connection per in-flight request**, so per-replica concurrency is bounded by connections, not CPU.
- **The embedding model loads into each replica's memory.** With CPU inference this is also per-replica CPU cost; a shared embedding service is the alternative if it becomes the constraint.
- **MCP servers over HTTP scale independently.** `execute_sql` is the one to scale carefully — it is the only one that puts real load on the database.

## 6. Monitoring

Detail in [OBSERVABILITY.md](OBSERVABILITY.md). Minimum for a deployment to be considered supportable:

- Request rate, error rate, and p95 latency per endpoint
- Tool-call outcomes per MCP server
- Invalid-query rate — a spike means a regression in generation, retrieval, or validation
- Statement-timeout rate
- LLM token spend
- Connection-pool saturation
- Retry-budget exhaustion rate

## 7. Health checks

| Endpoint | Checks | Orchestrator action on failure |
|---|---|---|
| `/health` | Process alive. No dependencies. | Restart |
| `/ready` | Database reachable, MCP servers connected, embedding model loaded, vectors present for the configured model version | Remove from load balancer |

Keeping them distinct matters: a database outage should stop traffic, not trigger a restart loop that makes recovery slower.

## 8. Backup and recovery

> **TBD — Stage 6.**

- `agent_meta` — sessions are disposable; **`query_audit` is not**. It is the security record.
- Embeddings are regenerable from the target schema, but regeneration is not instant; a backup is cheaper than a rebuild.
- Target data backup is the data owner's responsibility, not this project's — this service only reads it.

## 9. Rollback

> **TBD — Stage 6.** Image tags are immutable; every migration has a working `downgrade()`. The awkward case is a migration that re-embeds under a new `model_version` — rollback is a config change back to the previous version, which is exactly why old vectors are kept until the new set is verified.
