#!/usr/bin/env bash
# Start a disposable local Postgres in Docker (never the live database) and print its addresses.
# Usage: scripts/local_db.sh        Stop it with: docker rm -f catalyst-db
set -euo pipefail

NAME=catalyst-db
PORT="${CATALYST_LOCAL_DB_PORT:-5432}"

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=catalyst \
    -p "127.0.0.1:${PORT}:5432" postgres:16 >/dev/null
fi

for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U postgres -q && break
  sleep 1
done
docker exec "$NAME" psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='catalyst_test'" | grep -q 1 \
  || docker exec "$NAME" createdb -U postgres catalyst_test

echo "export CATALYST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PORT}/catalyst"
echo "export CATALYST_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PORT}/catalyst_test"
