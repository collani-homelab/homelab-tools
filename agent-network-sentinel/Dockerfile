FROM python:3.14.0-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
WORKDIR /agent
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen
ENV PATH="/agent/.venv/bin:$PATH"
