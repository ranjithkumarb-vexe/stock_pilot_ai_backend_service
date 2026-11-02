# syntax=docker/dockerfile:1
# StockPilot AI API. Build: docker build -t stockpilot-api .
# STATUS: statically reviewed + the file layout below was reproduced and booted without Docker (docs/RUNBOOK.md section 9);
# `docker build` itself has NOT been run (no Docker on the dev machine) - treat the first CI/Render build as the real test.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
# GET /v1/evals/latest serves the committed eval results (the Audit -> Evals screen); regenerate with `make eval`
COPY evals/results ./evals/results

FROM python:3.12-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
USER app
EXPOSE 8000
# Platforms that inject $PORT (Render: 10000 by default) are honoured; local default 8000.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=2).status == 200 else 1)"]
# ONE process only: the quote store, SSE fan-out, alert monitor and feature scheduler are in-process (ADR-004).
# Do not add --workers and do not scale the service beyond 1 instance without moving them to Redis/Arq.
# Migrations are a release step, not part of start-up: `python -m app.core.migrate` (needs DATABASE_ADMIN_URL + APP_DB_PASSWORD).
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --no-server-header"]
