FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
COPY apps/agents ./apps/agents
COPY packages ./packages
COPY infra ./infra

RUN uv sync --frozen --package ecom-agent-api

CMD ["sh", "-c", "uv run uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
