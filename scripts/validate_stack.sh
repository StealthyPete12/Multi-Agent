#!/usr/bin/env bash
# Brings up the Phase 0 infrastructure stack and verifies every service is
# healthy and reachable. Exits non-zero on any failure.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f .env ]]; then
  echo "No .env found, using .env.example defaults for this run."
  cp .env.example .env
fi

echo "==> docker compose up -d"
docker compose up -d

SERVICES=(rabbitmq postgres redis)
MAX_WAIT_SECONDS=90

echo "==> waiting for healthchecks (up to ${MAX_WAIT_SECONDS}s)"
for service in "${SERVICES[@]}"; do
  container="$(docker compose ps -q "$service")"
  if [[ -z "$container" ]]; then
    echo "FAIL: no container found for service '$service'"
    exit 1
  fi

  waited=0
  while true; do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")"
    if [[ "$status" == "healthy" ]]; then
      echo "OK: $service is healthy"
      break
    fi
    if (( waited >= MAX_WAIT_SECONDS )); then
      echo "FAIL: $service did not become healthy within ${MAX_WAIT_SECONDS}s (status: $status)"
      docker compose logs "$service" | tail -n 50
      exit 1
    fi
    sleep 3
    waited=$((waited + 3))
  done
done

echo "==> checking RabbitMQ management UI"
RABBITMQ_MANAGEMENT_PORT="${RABBITMQ_MANAGEMENT_PORT:-15672}"
if curl -fsS -o /dev/null "http://localhost:${RABBITMQ_MANAGEMENT_PORT}"; then
  echo "OK: RabbitMQ management UI reachable at http://localhost:${RABBITMQ_MANAGEMENT_PORT}"
else
  echo "FAIL: RabbitMQ management UI not reachable"
  exit 1
fi

echo "==> checking Postgres schema"
POSTGRES_USER="${POSTGRES_USER:-swarm}"
POSTGRES_DB="${POSTGRES_DB:-code_review_swarm}"
EXPECTED_TABLES=(modules imports commits findings reports processed_events audit_log)
for table in "${EXPECTED_TABLES[@]}"; do
  if docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
      "SELECT to_regclass('public.${table}') IS NOT NULL;" | grep -q t; then
    echo "OK: table '$table' exists"
  else
    echo "FAIL: table '$table' missing"
    exit 1
  fi
done

echo "==> all checks passed"
