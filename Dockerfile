# Lazy Sleeper backend — the API + draft companion (`lazy serve`) and the CLI, for the
# dev-apps VM (LS-75). Build context = the repo; the DB is whatever DATABASE_URL points at
# (Supabase for draft night). Migrations are explicit, never run on start:
#   docker compose run --rm app alembic upgrade head
FROM python:3.12-slim-bookworm

# uv from its official image — pinned so the lockfile resolves the same way as in CI.
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cached until the lockfile changes), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY lazy_sleeper ./lazy_sleeper
COPY alembic.ini ./
COPY alembic ./alembic
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# data/ = snapshot cache, draft poll logs, board exports — a volume in compose so it outlives
# the container. Owned by the runtime user so a named volume inherits writable ownership.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/snapshots /app/data/logs /app/data/boards \
    && chown -R app:app /app/data
USER app
VOLUME ["/app/data"]

EXPOSE 8000
# slim has no curl; the API's /health is the liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# The printed "LAN" URLs are the container's own address — reach the API at the host's IP.
CMD ["lazy", "serve", "--host", "0.0.0.0", "--port", "8000"]
