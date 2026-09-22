#!/usr/bin/env bash
# Wrapper for running docker-compose with the Instaclustr override.
# Loads instaclustr.env for both compose variable substitution (--env-file)
# and container injection (env_file: directive in the override file).
#
# Usage:
#   ./run-instaclustr.sh up -d
#   ./run-instaclustr.sh up -d --force-recreate investigation-ui
#   ./run-instaclustr.sh logs -f investigation-ui
#   ./run-instaclustr.sh down
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/instaclustr.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy instaclustr.env.template to instaclustr.env and fill in your credentials." >&2
  exit 1
fi

docker-compose \
  --env-file "$ENV_FILE" \
  -f "$SCRIPT_DIR/docker-compose.yml" \
  -f "$SCRIPT_DIR/docker-compose.instaclustr.yml" \
  "$@"
