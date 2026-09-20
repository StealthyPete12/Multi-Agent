# syntax=docker/dockerfile:1
#
# Build from the repo root: docker build -f agents/watcher/Dockerfile .
# (the watcher imports shared/, which lives outside this directory).
#
# ---- builder: resolve + install dependencies into an isolated venv ----
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

# build-essential only exists in this stage — needed in case any dependency
# (e.g. a future non-manylinux wheel) requires compiling from source. The
# runtime stage below never sees a compiler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY shared ./shared
COPY agents ./agents
COPY tools ./tools

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# ---- runtime: minimal image, no compiler, no source outside site-packages ----
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="code-review-swarm-watcher" \
      org.opencontainers.image.description="GitHub webhook receiver -> commit.detected" \
      org.opencontainers.image.source="https://github.com/"

RUN groupadd --gid 10001 swarm \
    && useradd --uid 10001 --gid swarm --no-create-home --shell /usr/sbin/nologin swarm

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATCHER_PORT=8001

WORKDIR /app
USER swarm

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${WATCHER_PORT}/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "agents.watcher.main:app", "--host", "0.0.0.0", "--port", "8001"]
