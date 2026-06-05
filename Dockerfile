# Astral's official image bundles `uv` on top of python:3.12-slim.
# See https://docs.astral.sh/uv/guides/integration/docker/
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# `copy` is required because the bind mount target can be on a different
# filesystem from the cache (typical in Docker BuildKit).
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    POE_CONFIG_PATH=/app/poe_control_config.json \
    PATH="/app/.venv/bin:$PATH"

# 1) Install the locked dependencies first, without the project itself,
#    so this layer caches across source changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Add the project sources and install the project itself (cheap).
# LICENSE and README.md are required at build time: pyproject.toml declares
# `license = { file = "LICENSE" }` and `readme = "README.md"`, so hatchling
# fails the wheel build if they are absent.
COPY LICENSE README.md ./
COPY config.py poe_control_mqtt.py poe_status_to_mqtt.py mqtt_test.py web_ui.py ./
COPY templates ./templates
COPY static ./static
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

CMD ["python", "poe_control_mqtt.py"]
