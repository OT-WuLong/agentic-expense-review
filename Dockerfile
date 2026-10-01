FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1 UV_LINK_MODE=copy UV_HTTP_TIMEOUT=120 \
    PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY scripts/seed_database.py scripts/index_hybrid.py scripts/
COPY data/fixtures/ data/fixtures/
COPY --from=frontend /build/dist/ frontend/dist/
RUN useradd --system --uid 10001 --create-home approval \
    && mkdir -p /app/tmp/uploads /app/data/indexes \
    && chown -R approval:approval /app/tmp /app/data/indexes
USER approval
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
