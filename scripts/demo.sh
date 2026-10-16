#!/usr/bin/env bash
# One command for the walking skeleton: start a local database, build the tables, then fetch one
# SEC filing, store a copy, and write one row. Needs Docker, uv, and SEC_CONTACT_EMAIL.
# Usage: scripts/demo.sh [CIK]     (default CIK 875320, Vertex Pharmaceuticals)
set -euo pipefail
cd "$(dirname "$0")/.."

CIK="${1:-875320}"
if [[ -z "${CATALYST_DATABASE_URL:-}" ]]; then
  db_env="$(scripts/local_db.sh)"  # a failure here stops the script; inside eval it would not
  eval "$db_env"
fi
uv sync --locked --quiet
uv run alembic upgrade head
uv run catalyst sec-press-release --cik "$CIK"
