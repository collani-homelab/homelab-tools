FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /agent
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen
ENV PATH="/agent/.venv/bin:$PATH"
