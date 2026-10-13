FROM oven/bun:1 AS web
WORKDIR /src
COPY web/package.json web/bun.lock* ./
RUN bun install --frozen-lockfile
COPY web/ ./
RUN bun run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY --from=web /src/dist ./web/dist
ENV PATH="/app/.venv/bin:$PATH" \
    LIBRARY_PATH=/library \
    DATA_PATH=/data \
    WEB_DIST=/app/web/dist \
    STAY_ON_MAIN_DEVICE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --system --uid 1000 --no-create-home app \
    && mkdir -p /data && chown app /data
USER 1000:1000
EXPOSE 8787
CMD ["uvicorn", "better_search.main:app", "--host", "0.0.0.0", "--port", "8787"]
