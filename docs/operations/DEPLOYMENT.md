# Deployment

> **Status: TBD — Stage 6.** Structure and constraints below are the plan; Dockerfiles, Compose files, and verified commands land with the hardening stage.

Two deployment shapes, with different security postures:

| Shape | Transport | Auth | Audience |
|---|---|---|---|
| **MCP host** (any stdio host) | stdio subprocesses | Host-managed, user privileges | The "point it at your own database" story |
| **HTTP service** | Streamable HTTP + SSE | API key required | Demo / production-style deployment |

The MCP-host shape is the one that makes this project runnable by other people, so it gets the more polished setup path.

**The first shape works today.** The four servers run over stdio and the host configuration is in [../architecture/MCP.md](../architecture/MCP.md) §9 — no Docker image and no HTTP endpoint required, because the host launches them as subprocesses with the environment it is given.

**The second shape is not deployable yet, and the process enforces that rather than documenting it.** The HTTP layer exists — `python -m api` serves `/health`, `/ready` and `POST /v1/query` — but it has **no authentication**, so `APISettings` refuses any `API_HOST` that is not loopback ([ADR-034](../architecture/DECISIONS.md#adr-034--the-api-refuses-to-bind-beyond-loopback-while-it-has-no-authentication)). A container can still publish the port, which is the supported path below; what it cannot do is bind `0.0.0.0` inside the container and pretend that is the same thing.

There is a reason that ordering is not accidental: an HTTP-reachable `execute_sql` is a different risk class from a subprocess a host launched, and it needs authentication before it needs a Dockerfile.

---

## 1. Docker

> **TBD — Stage 6.**

Planned: multi-stage build on `python:3.12-slim` ([ADR-010](../architecture/DECISIONS.md#adr-010--python-312)).

**Any OCI-compatible runtime.** The images and Compose file target the standard format, so Podman, Rancher Desktop, colima or Docker Engine all serve. Docker Desktop is free for personal use and requires a paid subscription for larger commercial organisations — convenient on Windows, and deliberately not a requirement, given the constraint in [PROJECT.md](../../PROJECT.md).

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
| `API_HOST` | `127.0.0.1` | **Not `0.0.0.0`** — see below. The container runtime publishes the port |
| `API_PORT` | `8000` | Whatever the runtime maps to |
| `API_DOCS_ENABLED` | `false` (the default) | OpenAPI is a complete map of the attack surface, served unauthenticated |
| `API_CORS_ORIGINS` | Named origins, or empty | `*` is refused at startup |
| `API_MAX_BODY_BYTES` | `65536` (the default) | Enforced before parsing. Raise only for a measured need |
| `API_MAX_CONCURRENT_REQUESTS` | Size against the **provider**, not the CPU | Every in-flight question holds a database connection and an outstanding LLM call. The free tier's requests-per-minute binds first |
| `API_POOL_MAX_SIZE` | **Must exceed** `API_MAX_CONCURRENT_REQUESTS` | Startup refuses otherwise. Sized equal, saturated traffic starves `/ready` of a connection and the replica is pulled for being busy |
| `DB_TARGET_SCHEMA` | The schema holding the target tables | **Must match `DATASET`'s schema.** Disagreeing returns a plausible answer from the wrong tables |
| `LOG_FORMAT` | `json` | Machine-parseable |
| `LOG_RESULT_VALUES` | `false` | Never log result data outside local debugging |
| `OTEL_ENABLED` | `true` | |
| `OTEL_TRACES_SAMPLER_ARG` | `<1.0` | Full sampling is a dev setting |
| `DATABASE_RO_URL` | Read-only role | **Verified at startup, not assumed** — `assert_read_only` refuses to open a connection whose role can write ([ADR-033](../architecture/DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing)) |

**`API_HOST=0.0.0.0` is a startup error, including inside a container.** That looks inconvenient and is deliberate: this service has no authentication, so binding all interfaces makes an endpoint that runs model-generated SQL against the target database reachable by anything that can route to it. Publishing the port (`-p 8000:8000`, or a Kubernetes Service) forwards to loopback inside the network namespace and works unchanged — the difference is that exposing the service becomes something a deployment declares rather than something a default does.

**`API_KEY` does not exist yet.** Earlier revisions of this page listed it as required in production. It is Stage 6 work; until it lands, "front it with something that authenticates" is the whole of the answer, and the loopback refusal is what stops that being optional.

Secrets come from the orchestrator's secret store, not from a `.env` file baked into the image.

## 4. Production setup

> **TBD — Stage 6.**

Checklist to be verified, not just written:

- [x] **`DATABASE_RO_URL` role cannot write** — no longer a checklist item an operator ticks. `assert_read_only` refuses to open the connection otherwise, so a deployment that gets this wrong does not start. The negative tests in [../development/TESTING.md](../development/TESTING.md) prove the *migration* produces a correct role; this proves the *deployment* connects as one, and the difference is the whole of [SECURITY.md](SECURITY.md) §13.2
- [ ] `agent_meta` not readable by the read-only role
- [ ] **Authentication in front of the service** — there is none in the application. Until `API_KEY` exists this means a proxy, a gateway, or a network boundary that does the work
- [ ] **A per-client rate limit at that same proxy** — the application's in-flight cap is process-wide, so one caller can consume the whole allowance. The per-client half needs an identity to key on, which is the same thing the row above is missing
- [ ] **`replicas × API_POOL_MAX_SIZE` sized against the database**, not the application. Scaling replicas without accounting for it scales load onto one PostgreSQL
- [~] **Rate limits active** — the global in-flight cap exists and covers streams and plain requests together (`API_MAX_CONCURRENT_REQUESTS`, `429` rather than a queue). Request-rate and token limits do not, and the per-client half of the in-flight cap needs the authentication two rows above
- [ ] **Response buffering disabled on every proxy in the path** — see §5.1. A buffering proxy turns an event stream into a slow non-streaming reply, and it fails silently: the answer still arrives, just all at once and possibly after the client gave up
- [ ] Statement timeout set on the role *and* per transaction
- [ ] Connection pool sized against database capacity, not application concurrency
- [ ] Vectors indexed for the configured `RETRIEVER_MODEL` (the recorded `model_version` comes from the embedder — there is deliberately no separate version variable, see [CONFIG.md](CONFIG.md) §5)
- [ ] Audit logging on and writable
- [ ] Secrets from a secret store; none in the image or in logs
- [ ] Health and readiness probes wired to the orchestrator

## 5. Scaling

> **TBD — Stage 6**, with load-test numbers.

- **API/agent is stateless** — sessions live in Postgres, so replicas scale horizontally.
- **The database is the shared bottleneck.** Total concurrent queries = replicas × `DB_POOL_MAX_SIZE`. Scaling replicas without accounting for this scales load onto a single database.
- **SSE holds a connection per in-flight request**, so per-replica concurrency is bounded by connections, not CPU — and a stream holds its slot for the *whole* answer, not just the moment of work. `API_MAX_CONCURRENT_REQUESTS` was sized against a provider's requests-per-minute for the non-streaming shape; it binds sooner once callers stream.
- **The embedding model loads into each replica's memory, at startup.** With CPU inference this is also per-replica CPU cost; a shared embedding service is the alternative if it becomes the constraint. **Startup takes roughly twenty seconds because of it** — deliberately, since that cost was previously paid by whoever sent the first request ([ADR-040](../architecture/DECISIONS.md#adr-040--startup-opens-the-model-because-naming-it-is-not-loading-it)). Size readiness probe `initialDelaySeconds` and any deployment timeout above it, or an orchestrator will kill replicas that are loading correctly.
- **MCP servers over HTTP scale independently.** `execute_sql` is the one to scale carefully — it is the only one that puts real load on the database.

### 5.1 Proxy buffering, and why it fails quietly

**Every reverse proxy in front of this service must be told not to buffer the response.** nginx, and several others, buffer by default — which for `text/event-stream` means holding every event until the response ends. The answer still arrives and is still correct, so nothing errors; the stream simply stops being a stream, and a client that was showing progress shows nothing for the whole answer.

The application sends `X-Accel-Buffering: no` and `Cache-Control: no-cache` on every streaming response. The first is honoured by nginx and by proxies that copied its convention, and it is **not a general standard** — anything else in the path needs configuring directly:

| Proxy | What to set |
|---|---|
| nginx | `proxy_buffering off;` on the location, or rely on the `X-Accel-Buffering` header this service already sends |
| Apache `mod_proxy` | `SetEnv proxy-sendchunked` and no `mod_deflate` on `text/event-stream` |
| HAProxy | No response buffering by default; check `option http-buffer-response` is **not** set |
| CloudFront / most CDNs | Do not put a CDN in front of this path |

**Compression is the other one.** A gzip layer that buffers to compress defeats streaming just as completely, and `text/event-stream` should be excluded from it.

Idle timeouts matter too: the service sends a `: keepalive` comment every `API_STREAM_KEEPALIVE_SECONDS` (default 15) so an idle-timeout of 60 s is safe. Anything shorter than the keepalive interval will close working streams.

### 5.2 Serving the demo UI

`API_STATIC_DIR` points the API at a built bundle and it serves the page itself. Three deployment consequences.

**The build is a separate step with a separate toolchain.** `npm ci && npm run build` in `web/` needs Node; the API image does not. Either build in a first stage and copy `web/dist` into the runtime image, or serve the page from a CDN and leave `API_STATIC_DIR` empty — but see the next point before choosing the second.

**Serving it from the API is what keeps CORS closed.** Page and API on one origin means `API_CORS_ORIGINS` stays empty, which matters because there is no authentication: every entry in that list is an origin allowed to drive this endpoint from a visitor's browser. **Hosting the UI elsewhere requires opening CORS**, and that is a materially weaker position, not a deployment preference. [SECURITY.md](SECURITY.md) §13.15.

**A wrong path is a failed start, not a 404.** If `API_STATIC_DIR` names something missing, something that is not a directory, or a directory with no `index.html`, the process raises `ConfigurationError` before binding. The last case is what an interrupted build leaves behind — and serving it would answer every request with a 404 from a process reporting itself healthy, which is the failure an orchestrator cannot see.

**Cache behaviour is already correct and worth not overriding.** Bundles under `/assets/` are content-hashed and safe to cache hard; `index.html` is served `no-cache` because it is the document that points at the current hashes. A proxy configured to cache the root document is how a browser ends up asking for a bundle a redeploy replaced.

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

Both are **built**. Contract in [../architecture/API.md](../architecture/API.md).

| Endpoint | Checks today | Orchestrator action on failure |
|---|---|---|
| `/health` | Process alive. **No dependencies** — asserted by a test that fails if it issues a query | Restart |
| `/ready` | `database`, `database_readonly` — a `SELECT 1` on each held connection, run on a worker thread | Remove from load balancer |

Keeping them distinct matters: a database outage should stop traffic, not trigger a restart loop that makes recovery slower. That is not a stylistic preference — a `/health` that touched the database would fail every replica's liveness probe at the same moment, and the orchestrator would answer a self-resolving incident by restarting the fleet, adding a cold start, a connection storm and a catalog reload per replica.

**`/ready` does not borrow from the request pool**, and that is why `API_POOL_MAX_SIZE` must exceed `API_MAX_CONCURRENT_REQUESTS`. It probes the two connections held since startup. If it ever moves to the pool, a saturated service would fail its own readiness probe — the orchestrator would remove the replica for being busy and move its traffic to replicas that are also busy, which is the shape that turns a spike into an outage.

Three properties an orchestrator config should know about:

- **`/ready` reports `up`/`down` per dependency and never a reason.** Both probes are unauthenticated — a kubelet cannot hold a credential — and a driver message carries the DSN, the internal hostname and the role name. The cause is in the process log, keyed by `request_id`.
- **The verdict is cached for 5 seconds**, so probe frequency does not become database load. A 10-second probe period sees at most one real check per probe.
- **Before startup completes, `/ready` answers `503` with `{"startup": "down"}`.** An unconfigured readiness checker must not answer yes.

The catalog, the retriever and the MCP servers are **not** probed. All are loaded or resolved at startup and held in memory, so a probe would assert that the process still has its own attributes — a check that cannot fail reports nothing. Earlier revisions of this page listed them; that was design intent rather than behaviour.

## 8. Backup and recovery

> **TBD — Stage 6.**

- `agent_meta` — sessions are disposable; **`query_audit` is not**. It is the security record.
- Embeddings are regenerable from the target schema, but regeneration is not instant; a backup is cheaper than a rebuild.
- Target data backup is the data owner's responsibility, not this project's — this service only reads it.

## 9. Rollback

> **TBD — Stage 6.** Image tags are immutable; every migration has a working `downgrade()`. The awkward case is a migration that re-embeds under a new `model_version` — rollback is a config change back to the previous version, which is exactly why old vectors are kept until the new set is verified.
