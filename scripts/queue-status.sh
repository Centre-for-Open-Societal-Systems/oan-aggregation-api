#!/usr/bin/env bash
# Is the aggregation fan-out queue healthy, and what is in it?
#
#   ./queue-status.sh            pretty-printed
#   run-local.bat queue          the same, from Windows
#
# The endpoint needs a token, so this borrows one from the mint sidecar on
# :8099 rather than asking you to paste one. Nothing here reads a topic
# directly — the answer comes from the aggregation rows, so it is the same with
# the broker on or off, and says which of the two is serving requests.
set -uo pipefail

AGG="${AGG_URL:-http://localhost:8110}"
MINT="${MINT_URL:-http://localhost:8099}"
PY="${AGG_VENV:-$HOME/agg-venv}/bin/python"

if [ ! -x "$PY" ]; then
  echo "No virtualenv at $PY — see RUN-LOCAL.md" >&2
  exit 1
fi

TOKEN="$("$PY" - <<PYEOF 2>/dev/null
import httpx
try:
    print(httpx.post("$MINT/mint", timeout=60).json()["variables"]["access_token"])
except Exception as exc:
    raise SystemExit("mint sidecar on $MINT is not answering (%s)" % exc)
PYEOF
)"

if [ -z "$TOKEN" ]; then
  echo "Could not get a token from $MINT. Start the mint sidecar:" >&2
  echo "  cd postman && ./mint-service.sh" >&2
  exit 1
fi

curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" \
  "$AGG/consent/v1/aggregation/queue" | "$PY" -m json.tool
