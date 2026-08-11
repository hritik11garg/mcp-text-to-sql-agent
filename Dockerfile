# Multi-stage build for the Text-to-SQL Analytics Agent.
#
# Three things drive the shape of this file.
#
# **torch is the whole image.** Installed from PyPI it pulls the CUDA build --
# several gigabytes of wheels for a device this image does not have. It is
# therefore installed first, from PyTorch's CPU index, so that the pinned
# version in requirements.txt is already satisfied when pip reads that file.
# Getting this wrong does not fail the build; it produces an image ~10x larger
# that works, which is why the ordering carries this comment.
#
# **Migrations are a separate target, not an entrypoint step.** Several replicas
# racing the same migration is a reliable way to corrupt schema state, so
# `migrate` is a one-shot service in docker-compose.yml and the API image has no
# way to run alembic at boot. DEPLOYMENT.md section 2.
#
# **The UI is built here, not committed.** `web/dist` is build output; shipping
# it in the repository would make the served page and the source drift.

# --- web build -------------------------------------------------------------
FROM node:22-slim AS web

WORKDIR /web
# Lockfile first, so a source edit does not re-resolve the dependency tree.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
# `npm run build` is `tsc --noEmit && vite build` -- the typecheck is part of
# the build on purpose, so a type error fails the image rather than shipping.
RUN npm run build


# --- python dependencies ---------------------------------------------------
FROM python:3.12-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # The CPU torch wheel is a few hundred megabytes from a single host. pip's
    # defaults (5 retries, no read timeout of its own) surface a slow link as a
    # ReadTimeoutError partway through, which reads like a broken Dockerfile
    # and is not one. Raised so a marginal connection retries instead.
    PIP_RETRIES=10 \
    PIP_TIMEOUT=120

# A virtualenv rather than the system interpreter, so the runtime stages copy
# one directory and inherit nothing else from this one.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./

# CPU-only torch first. See the header: this ordering is the difference between
# a ~1 GB image and a ~4 GB one, and both of them work.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        "$(grep -E '^torch==' requirements.txt)" \
 && pip install -r requirements.txt


# --- runtime base ----------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

# Non-root. This process executes SQL a language model wrote; the container
# user is one of the few boundaries that costs nothing to put in place.
RUN useradd --create-home --uid 10001 appuser

# The model cache directory is created *here*, owned by appuser, and that is
# load-bearing rather than tidy. Docker initialises an empty named volume from
# the image's contents at that path -- ownership included -- but creates the
# path root-owned if the image has nothing there. Mounting the cache volume
# then gives a non-root process a directory it cannot write, and the failure
# lands deep inside huggingface_hub as a bare PermissionError on first download.
RUN mkdir -p /home/appuser/.cache/huggingface \
 && chown -R appuser:appuser /home/appuser/.cache

WORKDIR /app

COPY --from=deps /opt/venv /opt/venv


# --- migrations ------------------------------------------------------------
FROM runtime AS migrate

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY src/ ./src/

USER appuser
# Overridden by compose, which passes the same thing explicitly so that reading
# the compose file tells you what runs.
CMD ["alembic", "upgrade", "head"]


# --- api -------------------------------------------------------------------
FROM runtime AS api

COPY src/ ./src/
COPY --from=web /web/dist/ ./web/dist/

# The model cache lives somewhere the non-root user can write. Without this,
# sentence-transformers tries to write to a home directory it does not own and
# the first retrieval fails at a confusing depth.
ENV HF_HOME=/home/appuser/.cache/huggingface \
    API_STATIC_DIR=/app/web/dist

USER appuser
EXPOSE 8000

# `python -m api` reads the bind address from settings rather than argv, so
# there is no flag here that could contradict the configuration. Binding beyond
# loopback requires API_ALLOW_NON_LOOPBACK -- set in compose, with the reasoning
# in ADR-049.
CMD ["python", "-m", "api"]
