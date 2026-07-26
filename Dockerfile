FROM node:22-alpine AS frontend
WORKDIR /build
RUN corepack enable
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json frontend/package.json
RUN pnpm install --frozen-lockfile
COPY frontend frontend
RUN pnpm --dir frontend build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 basis-hawk \
    && useradd --uid 10001 --gid basis-hawk --create-home basis-hawk
COPY requirements.lock pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir -r requirements.lock
COPY alembic.ini ./
COPY alembic alembic
COPY src src
COPY --from=frontend /build/frontend/dist frontend/dist
RUN pip install --no-cache-dir -e . --no-deps \
    && chown -R basis-hawk:basis-hawk /app
USER basis-hawk
EXPOSE 8000
CMD ["basis-hawk", "serve"]
