# Deployment

> **Status: the local shape is built and verified; the production shape is v2.0.** `docker compose up` builds the image, applies migrations, seeds a demo database and serves the API with its UI — run end to end from empty volumes on 2026-08-11. What is **not** here is a deployment anyone else can reach, and the reason is one sentence: **the service has no authentication**, so the only supported audience is a person on the machine it runs on.

Two deployment shapes, with different security postures:

| Shape | Transport | Auth | Audience | State |
|---|---|---|---|---|
| **MCP host** (any stdio host) | stdio subprocesses | Host-managed, user privileges | The "point it at your own database" story | **Works today** |
| **HTTP service, local** | HTTP + SSE on loopback, or a published container port | **None** | One operator, on their own machine | **Works today** |
| **HTTP service, shared** | HTTP + SSE behind a proxy | API key required | Demo / production-style deployment | **v2.0** — blocked on authentication |

The MCP-host shape is the one that makes this project runnable by other people, so it gets the more polished setup path.

**The first shape works today.** The four servers run over stdio and the host configuration is in [../architecture/MCP.md](../architecture/MCP.md) §9 — no Docker image and no HTTP endpoint required, because the host launches them as subprocesses with the environment it is given.

**The second shape works today and is bounded rather than documented.** `python -m api` — or the `api` service in Compose — serves `/health`, `/ready` and `POST /v1/query`. It has **no authentication**, so `APISettings` refuses any `API_HOST` that is not loopback ([ADR-034](../architecture/DECISIONS.md#adr-034--the-api-refuses-to-bind-beyond-loopback-while-it-has-no-authentication)) unless an operator sets `API_ALLOW_NON_LOOPBACK`, which the container image does and which the compose file pairs with publishing to `127.0.0.1` only. That pairing is the whole containment argument for the container shape and it is explained in §2.

**The third shape is not deployable and the process says so rather than the prose.** An HTTP-reachable `execute_sql` is a different risk class from a subprocess a host launched, and it needs authentication before it needs anything else here. [SECURITY.md §13.1](SECURITY.md) rates that gap Critical and it is open.

---

## 1. Docker

`Dockerfile`, five stages, built and run on 2026-08-11.

| Stage | Base | What it produces |
|---|---|---|
| `web` | `node:22-slim` | `npm ci` then `npm run build` — typecheck, bundle, and the no-inline-assets assertion the CSP depends on ([SECURITY.md §13.18](SECURITY.md)) |
| `deps` | `python:3.12-slim` | A virtualenv at `/opt/venv` |
| `runtime` | `python:3.12-slim` | The venv, `src/`, the built bundle, and a non-root user |
| `migrate` | `runtime` | One-shot `alembic upgrade head` |
| `api` | `runtime` | `python -m api` |

**Any OCI-compatible runtime.** The image and Compose file target the standard format, so Podman, Rancher Desktop, colima or Docker Engine all serve. Docker Desktop is free for personal use and requires a paid subscription for larger commercial organisations — convenient on Windows, and deliberately not a requirement, given the constraint in [PROJECT.md](../../PROJECT.md).

What the constraints turned into:

- **Image size — `torch` dominates, and it is installed first, from PyTorch's own CPU index.** `pip install --index-url https://download.pytorch.org/whl/cpu` before the rest of `requirements.txt`, so the resolver never reaches PyPI for a CUDA build. That is well over a gigabyte of CUDA libraries not downloaded. Inference does not need a GPU; training is a separate concern and a separate build.
- **Model weights are mounted, not baked.** A named volume at `/home/appuser/.cache/huggingface`. Baking would remove the ~90 MB first-run download at the cost of putting it in every layer pull. The volume is created **in the image with the right ownership** — a Docker named volume inherits ownership from the image path at first mount, and creating it implicitly leaves it root-owned under a non-root user, which fails as a `PermissionError` inside `huggingface_hub` rather than as anything about permissions.
- **Non-root user.** `appuser`, uid 10001. Nothing in the runtime path needs root.
- **No build toolchain in the runtime layer.** Every pinned dependency ships a wheel, so the final stage carries no compiler.
- **`pip` retries are raised** (`PIP_RETRIES=10 PIP_TIMEOUT=120`). The torch wheel is large enough that a default-timeout read failure is a routine build failure rather than an exceptional one.
- **Read-only root filesystem** is *not* set. Recorded as open rather than claimed.

`.dockerignore` keeps `data/`, `results/`, `.git/`, `web/node_modules/` and every `.env*` except the example out of the build context — the first two are gigabytes, and the last is the one that would bake a credential into a layer.

## 2. Docker Compose

Four services. `docker compose up` runs all of them in order and ends with a served API.

| Service | Purpose | Gate |
|---|---|---|
| `postgres` | Postgres 16 + pgvector | `pg_isready` healthcheck |
| `migrate` | One-shot Alembic run — schema, extension, HNSW index, read-only role | waits for `postgres` healthy |
| `seed` | One-shot: creates the demo schema, loads it, grants `SELECT` to the read-only role, indexes the catalog | waits for `migrate` **completed successfully** |
| `api` | FastAPI, serving the built UI | waits for both one-shots completed successfully |

Points that matter:

- **Migrations are a separate one-shot service**, not an entrypoint hook in the API container. Running migrations from N replicas racing each other is a reliable way to corrupt schema state.
- **`depends_on` with `condition: service_healthy` for the database, and `service_completed_successfully` for the one-shots.** Container-started is not database-ready, and a migration that is still running is a schema the application must not touch.
- **The published port is `127.0.0.1:${API_PORT:-8000}:8000`, and that binding is a security control, not a preference.** Inside the namespace the API binds `0.0.0.0` with `API_ALLOW_NON_LOOPBACK=true`, because `-p` forwards to the container's **bridge** interface and a process on the container's loopback is reachable by nobody ([ADR-049](../architecture/DECISIONS.md#adr-049--binding-beyond-loopback-is-an-operators-assertion-not-a-detection), [SECURITY.md §13.17](SECURITY.md)). **The two halves are one decision:** binding wide inside the namespace is only safe because the host side of the mapping is loopback. Change `ports` to `8000:8000` and the unauthenticated service is on every interface the host has.
- **`env_file: .env` is inherited wholesale rather than enumerated.** A list of `LLM_*` variables in the compose file is a second copy of `core.settings` that goes stale silently, and the symptom is a container running on defaults while the host runs on the configured values — which presents as a code difference. Only what must *differ* inside the namespace is set explicitly: the two database URLs, the bind, and the demo dataset names.
- **`DATASET` and `DB_TARGET_SCHEMA` are literals, not `${VAR:-demo}`.** Compose interpolates `${VAR:-default}` from `.env` — the same file passed as `env_file` — so a `.env` carrying `DB_TARGET_SCHEMA=public` wins and the default never fires. The failure it produced was `relation "event" does not exist` from a container that had seeded the data correctly into a schema it was not reading.

The read-only role is created by a migration, not by an init script, so the security posture is reproducible from a clean database and testable in CI. See [../architecture/DATABASE.md](../architecture/DATABASE.md) §7–8.

### 2.1 The demo dataset

`seed` runs `python -m demo.seed`, which builds an **original** three-table schema — venues, artists, events; 432 rows from a fixed seed — in the `demo` PostgreSQL schema, grants `USAGE` and `SELECT` on it to `sql_agent_ro`, and indexes the catalog so retrieval has vectors to search.

It exists because the clean-checkout path needed data and the only data this repository knew how to load was Spider: a 100 MB download under CC BY-SA, which is fine for a benchmark and wrong for a first run. The demo set is generated rather than vendored, so it ships under the repository's own MIT licence, and it is deterministic, so the README's example output is reproducible.

**No number anywhere in this repository comes from it.** Every measurement is Spider ([BENCHMARKS.md](../ml/BENCHMARKS.md)); the demo dataset is a thing to ask questions of, not a thing to score against.

`create_schema()` sets `search_path` under a `try/finally` that resets it. That is not tidiness: the connection is shared, and a leaked `search_path` made the *indexer* fail two steps later with `vector type not found in the database` — an error naming neither the schema nor the setting that caused it. A regression test pins the reset.

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
| `OTEL_ENABLED` | `true` | **Reads nothing today.** No settings class defines it and nothing imports `opentelemetry`; the row is what production will need, not what production will get |
| `OTEL_TRACES_SAMPLER_ARG` | `<1.0` | Full sampling is a dev setting — same caveat as the row above |
| `DATABASE_RO_URL` | Read-only role | **Verified at startup, not assumed** — `assert_read_only` refuses to open a connection whose role can write ([ADR-033](../architecture/DECISIONS.md#adr-033--the-read-only-role-is-proved-at-startup-by-asking-rather-than-by-writing)) |

**`API_HOST=0.0.0.0` is a startup error, including inside a container.** That looks inconvenient and is deliberate: this service has no authentication, so binding all interfaces makes an endpoint that runs model-generated SQL against the target database reachable by anything that can route to it. Publishing the port (`-p 8000:8000`, or a Kubernetes Service) forwards to loopback inside the network namespace and works unchanged — the difference is that exposing the service becomes something a deployment declares rather than something a default does.

**`API_KEY` does not exist yet.** Earlier revisions of this page listed it as required in production. It is v2.0 work; until it lands, "front it with something that authenticates" is the whole of the answer, and the loopback refusal is what stops that being optional.

Secrets come from the orchestrator's secret store, not from a `.env` file baked into the image.

## 4. Production setup

> **TBD — v2.0.**

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

> **TBD — v2.0**, with load-test numbers.

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

> **TBD — v2.0.**

- `agent_meta` — sessions are disposable; **`query_audit` is not**. It is the security record.
- Embeddings are regenerable from the target schema, but regeneration is not instant; a backup is cheaper than a rebuild.
- Target data backup is the data owner's responsibility, not this project's — this service only reads it.

## 9. Rollback

> **TBD — v2.0.** Image tags are immutable; every migration has a working `downgrade()`. The awkward case is a migration that re-embeds under a new `model_version` — rollback is a config change back to the previous version, which is exactly why old vectors are kept until the new set is verified.
