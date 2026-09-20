# syntax=docker/dockerfile:1
#
# Build from the repo root: docker build -f agents/researcher/Dockerfile .
#
# ---- builder: resolve + install dependencies into an isolated venv ----
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

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

# ---- runtime: git is the one extra runtime dependency this agent needs ----
# (agents/researcher/repository.py shells out to `git clone`/`fetch`/`checkout`
# for every commit it analyzes) — the only one of the three agents that does.
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="code-review-swarm-researcher" \
      org.opencontainers.image.description="commit.detected -> repo clone, AST graph, blast radius -> findings.ready" \
      org.opencontainers.image.source="https://github.com/"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 swarm \
    && useradd --uid 10001 --gid swarm --no-create-home --shell /usr/sbin/nologin swarm

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REPO_CACHE_DIR=/var/lib/swarm/repo-cache \
    RESEARCHER_METRICS_PORT=9102

WORKDIR /app
RUN mkdir -p "${REPO_CACHE_DIR}" && chown swarm:swarm "${REPO_CACHE_DIR}"
USER swarm

EXPOSE 9102

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${RESEARCHER_METRICS_PORT}/metrics', timeout=3)" || exit 1

CMD ["python", "-m", "agents.researcher.main"]
