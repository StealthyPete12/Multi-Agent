# syntax=docker/dockerfile:1
#
# Build from the repo root: docker build -f agents/reviewer/Dockerfile .
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

# ---- runtime: no git needed here — the reviewer never checks out a repo ----
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="code-review-swarm-reviewer" \
      org.opencontainers.image.description="findings.ready -> risk score + narrative -> review.completed" \
      org.opencontainers.image.source="https://github.com/"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 swarm \
    && useradd --uid 10001 --gid swarm --no-create-home --shell /usr/sbin/nologin swarm

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REVIEWER_METRICS_PORT=9103

WORKDIR /app
USER swarm

EXPOSE 9103

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${REVIEWER_METRICS_PORT}/metrics', timeout=3)" || exit 1

CMD ["python", "-m", "agents.reviewer.main"]
