# The API and the worker (one image; the worker overrides the command). CPU-only PyTorch on
# Linux comes from pyproject's pytorch-cpu index. The recorded parses and model replies are
# included so a demo can run on them with no model key.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
# Shared libraries the OCR and image code load at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app /app
COPY evals/baselines ./evals/baselines
COPY tests/fixtures/recorded ./recordings
COPY tests/fixtures/synthetic ./demo/synthetic
COPY tests/fixtures/coa ./demo/coa
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    RECORDINGS_DIR=/app/recordings \
    EVALS_DIR=/app/evals/baselines \
    HF_HOME=/home/app/.cache/huggingface
USER app
EXPOSE 8000
# Only the front proxy can reach this port, so its forwarded client address is trusted.
CMD ["uvicorn", "docforge.api.main:create_default_app", "--factory", "--host", "0.0.0.0", \
     "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
