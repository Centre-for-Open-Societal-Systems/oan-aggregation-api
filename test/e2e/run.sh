#!/usr/bin/env bash
# Self-contained end-to-end run: throwaway Postgres + CM + Aggregation Layer +
# fakes, then e2e.py. Nothing in Docker is touched; everything is torn down.
#
#   CM_SRC=/path/to/consent-management bash test/e2e/run.sh
#
# Needs: PostgreSQL binaries (initdb, pg_ctl) and a venv with the CM's
# dependencies (VENV, default ~/agg-venv).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AGG_SRC="$(cd "$HERE/../.." && pwd)"
CM_SRC="${CM_SRC:?set CM_SRC to the consent-management checkout}"
VENV="${VENV:-$HOME/agg-venv}"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin | tail -1)}"
WORK="$(mktemp -d /tmp/agg-e2e.XXXX)"
PGPORT=55432
PY="$VENV/bin/python"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  "$PG_BIN/pg_ctl" -D "$WORK/pg" stop -m fast >/dev/null 2>&1 || true
  [ "${KEEP:-0}" = 1 ] || rm -rf "$WORK"
}
trap cleanup EXIT

echo "work dir: $WORK"
"$PG_BIN/initdb" -D "$WORK/pg" -U postgres --auth=trust >/dev/null
"$PG_BIN/pg_ctl" -D "$WORK/pg" -o "-p $PGPORT -k $WORK -c listen_addresses=127.0.0.1" \
  -l "$WORK/pg.log" start >/dev/null
for db in consent_manager_db aggregation_layer_db; do
  "$PG_BIN/createdb" -h 127.0.0.1 -p $PGPORT -U postgres $db
done

# Keys: partners X/Y sign consent objects; the aggregation layer signs its hops.
mkdir -p "$WORK/keys"
"$PY" - "$WORK/keys" <<'PY'
import sys, pathlib
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric import ed25519
d = pathlib.Path(sys.argv[1])
for ref in ("PARTNER_X", "PARTNER_Y", "PARTNER_AGGREGATION_LAYER"):
    k = ed25519.Ed25519PrivateKey.generate()
    (d / f"{ref}.pem").write_bytes(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
    (d / f"{ref}.pub.pem").write_bytes(k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo))
PY

SECRET="e2e-hmac-secret"
common_db() { echo "$1_db_hostname=127.0.0.1 $1_db_port=$PGPORT $1_db_username=postgres $1_db_password=x"; }

start() {  # name port pythonpath env... -- module
  local name=$1 port=$2 pp=$3; shift 3
  ( cd "$WORK" && env PYTHONPATH="$pp" "$@" >"$WORK/$name.log" 2>&1 ) &
  PIDS+=($!)
}

# ── Consent Manager (this branch) ──
CM_ENV="$(common_db consent_manager) consent_manager_auth_enabled=false
  consent_manager_crypto_backend=partner-mgmt consent_manager_partner_mgmt_api_url=http://127.0.0.1:18090
  consent_manager_subject_consent_required=true consent_manager_aggregator_enabled=false
  consent_manager_otp_debug_enabled=true consent_manager_partner_cache_ttl_sec=1
  consent_manager_aggregation_layer_events_url=http://127.0.0.1:18100/aggregation/v1/cm-events
  consent_manager_aggregation_layer_events_hmac_secret=$SECRET"
( cd "$WORK" && env PYTHONPATH="$CM_SRC/backend/src" $CM_ENV "$PY" -m openg2p_consent_manager.main migrate >"$WORK/cm-migrate.log" 2>&1 )
start cm 18000 "$CM_SRC/backend/src" $CM_ENV "$PY" -m uvicorn openg2p_consent_manager.main:app --port 18000

# ── Aggregation Layer ──
REG='{"farmer":{"url":"http://127.0.0.1:18090","audience":"aggregation-layer-farmer","controller_id":"farmer-registry","reg_type":"ns:FarmerRecord","reg_record_type":"ns:Farmer","receiver_id":"farmer-registry","id_type":"functional_id"}}'
AGG_ENV="$(common_db aggregation_layer) aggregation_layer_auth_enabled=false
  aggregation_layer_cm_base_url=http://127.0.0.1:18000
  aggregation_layer_cm_events_hmac_secret=$SECRET
  aggregation_layer_otp_debug_enabled=true"
( cd "$WORK" && env PYTHONPATH="$AGG_SRC/backend/src" $AGG_ENV "$PY" -m openg2p_aggregation_layer.main migrate >"$WORK/agg-migrate.log" 2>&1 )
start agg 18100 "$AGG_SRC/backend/src" $AGG_ENV \
  aggregation_layer_signing_private_key_pem="$(cat "$WORK/keys/PARTNER_AGGREGATION_LAYER.pem")" \
  aggregation_layer_aggregator_registries="$REG" \
  "$PY" -m uvicorn openg2p_aggregation_layer.main:app --port 18100

# ── PM keys + registry + partner callback ──
start fakes 18090 "$HERE" E2E_KEYS="$WORK/keys" E2E_CM=http://127.0.0.1:18000 \
  "$PY" -m uvicorn fakes:app --port 18090

for url in http://127.0.0.1:18000/ping http://127.0.0.1:18100/ping http://127.0.0.1:18090/received; do
  for _ in $(seq 60); do curl -sf "$url" >/dev/null && break; sleep 0.5; done
  curl -sf "$url" >/dev/null || { echo "not up: $url"; tail -30 "$WORK"/*.log; exit 1; }
done

set +e
E2E_KEYS="$WORK/keys" "$PY" "$HERE/e2e.py"
rc=$?
if [ $rc -ne 0 ]; then
  echo "--- cm.log ---";  grep -iE 'error|traceback|warn' "$WORK/cm.log"  | grep -v json_logging | tail -15
  echo "--- agg.log ---"; grep -iE 'error|traceback|warn' "$WORK/agg.log" | grep -v json_logging | tail -15
fi
exit $rc
