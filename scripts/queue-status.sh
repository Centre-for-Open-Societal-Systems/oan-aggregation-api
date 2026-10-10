#!/usr/bin/env bash
# Is the aggregation fan-out queue healthy, and what is in it?
#
#   TOKEN=<bearer token> scripts/queue-status.sh
#   scripts/queue-status.sh      # token from the environment postman/agg-prep.py wrote
#
# Nothing here reads a topic directly: the answer comes from the aggregation
# rows, so it is the same with the broker on or off, and says which of the two
# is serving requests.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AGG="${AGG_URL:-http://localhost:8110}"
ENV_FILE="$HERE/../postman/OpenG2P-Aggregator.local.postman_environment.json"
PY="${PYTHON:-python3}"

if [ -z "${TOKEN:-}" ] && [ -f "$ENV_FILE" ]; then
  TOKEN="$("$PY" -c 'import json,sys; print(next(v["value"] for v in json.load(open(sys.argv[1]))["values"] if v["key"] == "access_token"))' "$ENV_FILE")"
fi
if [ -z "${TOKEN:-}" ]; then
  echo "No token: set TOKEN, or run postman/agg-prep.py first." >&2
  exit 1
fi

curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" \
  "$AGG/aggregation/v1/queue" | "$PY" -m json.tool
