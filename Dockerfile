# AllPath in a container. See "Run with Docker" in README.md.
#   docker compose --profile demo up     keyless demo on http://localhost:8790
#   docker compose --profile app up      your own install on http://localhost:8791
FROM ghcr.io/astral-sh/uv:0.9-python3.11-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY allpath_trade ./allpath_trade
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.11-slim-bookworm

# Runs as an ordinary user. /data is the working directory: the app keeps
# .env, the SQLite database, strategies/ and memory/ in whatever directory
# it is started from, so that is where the volume goes.
RUN useradd --create-home --uid 10001 allpath \
    && mkdir /data \
    && chown allpath:allpath /data
COPY --from=build /app/.venv /app/.venv
COPY IDENTITY.md /app/IDENTITY.md
COPY docker/entrypoint.sh /usr/local/bin/allpath-entrypoint
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
USER allpath
WORKDIR /data
VOLUME /data
EXPOSE 8790 8791
ENTRYPOINT ["allpath-entrypoint"]
CMD ["serve"]
