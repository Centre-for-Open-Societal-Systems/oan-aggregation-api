#!/usr/bin/env bash
#
# uninstall-agri-composite.sh
# ---------------------------
# Cleanly uninstall an Agri Stack Composite Helm release and everything it
# created, including what survives `helm uninstall`: the console's database and
# role in commons-postgresql, and the console's application rows in IAM.
#
# What it does, in order:
#   1. Stop in-flight Jobs                  (so `helm uninstall --wait` does not block)
#   2. helm uninstall <release>             (API, console UI, Services, HPAs,
#                                            VirtualServices, use-case ConfigMap)
#   3. Delete leftover Jobs + their Pods    (sanity, console DB init, Keycloak
#                                            client init, IAM registration), then
#                                            re-check for hook Jobs that appear late
#   4. Sweep leftover Secrets / ConfigMaps  (label app.kubernetes.io/instance; this
#                                            includes the console's DB password and
#                                            Keycloak client Secrets, kept by Helm)
#   5. Drop the console database + role     (via `kubectl exec` into commons-postgresql-0)
#   6. Delete the console's IAM rows        (staff_portal_applications + roles /
#                                            permissions for mnemonic agri-composite)
#   7. Optionally delete the signing-key Secret (--drop-signing-secret; you created it)
#
# NOT removed (deliberately):
#   - the console's Keycloak client (staff realm): harmless, and a reinstall
#     updates it in place (as the registry uninstall leaves its clients);
#   - the composite's partner, key and policies in Partner Management, and the
#     partners' use-case policies there (test fixtures, reused by a reinstall;
#     remove them there);
#   - the department PMs' entries (key, scope policies) for the composite.
#
# Refuses to start while a Helm operation on the release is still running
# (status pending-install / pending-upgrade / pending-rollback, or a Rancher
# helm-operation-* pod in cattle-system targeting it). Wait, or pass --force.
# After the job cleanup it re-checks for late hook Jobs/Pods SWEEP_CHECKS times
# (env, default 3), SWEEP_INTERVAL seconds apart (env, default 10).
#
# Requires: kubectl (cluster admin), helm, jq, bash 4+.
#
# USAGE:
#   ./uninstall-agri-composite.sh \
#       --namespace <ns> \
#       [--release <name>]            (default: agri-composite)
#       [--console-db <name>]         (default: agri_composite)
#       [--console-db-user <name>]    (default: agri_composite_user)
#       [--console-mnemonic <name>]   (default: agri-composite; IAM application / Keycloak client)
#       [--iam-db <name>]             (default: iam)
#       [--postgres-release <name>]   (default: commons-postgresql)
#       [--postgres-namespace <ns>]   (default: same as --namespace)
#       [--keep-db]                   (do NOT drop the console database/role)
#       [--keep-iam]                  (do NOT delete the console's IAM rows)
#       [--drop-signing-secret]       (also delete the signing Secret; kept by default)
#       [--signing-secret <name>]     (default: agri-composite-signing)
#       [--force]                     (run even while a Helm operation is in progress)
#       [--dry-run]                   (print actions, change nothing)
#       [--yes]                       (skip interactive confirmation)
#
# EXAMPLES:
#   ./uninstall-agri-composite.sh --namespace agrix --dry-run
#   ./uninstall-agri-composite.sh --namespace agrix
#   ./uninstall-agri-composite.sh --namespace agrix --yes --drop-signing-secret

set -euo pipefail

RELEASE="agri-composite"
NAMESPACE=""
CONSOLE_DB="agri_composite"
CONSOLE_DB_USER="agri_composite_user"
CONSOLE_MNEMONIC="agri-composite"
IAM_DB="iam"
POSTGRES_RELEASE="commons-postgresql"
POSTGRES_NAMESPACE=""
SIGNING_SECRET="agri-composite-signing"
KEEP_DB=false
KEEP_IAM=false
DROP_SIGNING_SECRET=false
FORCE=false
DRY_RUN=false
ASSUME_YES=false
SWEEP_CHECKS="${SWEEP_CHECKS:-3}"
SWEEP_INTERVAL="${SWEEP_INTERVAL:-10}"

usage() { sed -n '2,59p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)             RELEASE="$2";             shift 2 ;;
    --namespace|-n)        NAMESPACE="$2";           shift 2 ;;
    --console-db)          CONSOLE_DB="$2";          shift 2 ;;
    --console-db-user)     CONSOLE_DB_USER="$2";     shift 2 ;;
    --console-mnemonic)    CONSOLE_MNEMONIC="$2";    shift 2 ;;
    --iam-db)              IAM_DB="$2";              shift 2 ;;
    --postgres-release)    POSTGRES_RELEASE="$2";    shift 2 ;;
    --postgres-namespace)  POSTGRES_NAMESPACE="$2";  shift 2 ;;
    --signing-secret)      SIGNING_SECRET="$2";      shift 2 ;;
    --keep-db)             KEEP_DB=true;             shift ;;
    --keep-iam)            KEEP_IAM=true;            shift ;;
    --drop-signing-secret) DROP_SIGNING_SECRET=true; shift ;;
    --force)               FORCE=true;               shift ;;
    --dry-run)             DRY_RUN=true;             shift ;;
    --yes|-y)              ASSUME_YES=true;          shift ;;
    -h|--help)             usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

[[ -z "$NAMESPACE" ]] && { echo "ERROR: --namespace is required"; exit 1; }
POSTGRES_NAMESPACE="${POSTGRES_NAMESPACE:-$NAMESPACE}"
PG_POD="${POSTGRES_RELEASE}-0"
[[ "$SWEEP_CHECKS" =~ ^[0-9]+$ && "$SWEEP_INTERVAL" =~ ^[0-9]+$ ]] \
  || { echo "ERROR: SWEEP_CHECKS and SWEEP_INTERVAL must be non-negative integers"; exit 1; }

_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
_green() { printf "\033[32m%s\033[0m\n" "$*"; }
_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
_blue()  { printf "\033[34m%s\033[0m\n" "$*"; }

run() {
  # Print + execute, or only print with --dry-run. Never aborts: cleanup is idempotent.
  echo "  \$ $*"
  if [[ "$DRY_RUN" == false ]]; then
    eval "$@" || _yellow "  (command returned non-zero — continuing)"
  fi
}

kexec_psql() {
  # SQL as the postgres superuser inside the Postgres pod (password from the pod's env).
  local db="$1" sql="$2"
  echo "  \$ psql -U postgres -d $db -c \"$sql\""
  if [[ "$DRY_RUN" == false ]]; then
    kubectl exec -n "$POSTGRES_NAMESPACE" "$PG_POD" -c postgresql -- \
      bash -c "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -U postgres -d \"$db\" -v ON_ERROR_STOP=0 -c \"$sql\"" \
      || _yellow "  (psql returned non-zero — continuing)"
  fi
}

kexec_psql_capture() {
  # Read-only query; prints the trimmed result, empty on any error.
  local db="$1" sql="$2"
  kubectl exec -n "$POSTGRES_NAMESPACE" "$PG_POD" -c postgresql -- \
    bash -c "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -U postgres -d \"$db\" -tAc \"$sql\"" 2>/dev/null \
    | tr -d '[:space:]' || true
}

# ---------- in-progress Helm operation guard ----------
# Uninstalling while `helm install/upgrade` still runs its hooks races it: the
# next hook Job appears after the cleanup and is left behind. Checks Helm's own
# status and Rancher's helm-operation-* pods. Returns 0 when one is running.
helm_operation_in_progress() {
  local busy=1 status ops
  status=$(helm -n "$NAMESPACE" status "$RELEASE" -o json 2>/dev/null \
    | jq -r '.info.status // empty' 2>/dev/null || true)
  case "$status" in
    pending-install|pending-upgrade|pending-rollback)
      _red "  Helm release '$RELEASE' is in status '$status' — an operation on it is still running"
      busy=0 ;;
  esac
  ops=$(kubectl -n cattle-system get pod -o json 2>/dev/null \
    | jq -r --arg ns "$NAMESPACE" --arg rel "$RELEASE" '
        .items[]
        | select(.metadata.name | startswith("helm-operation-"))
        | select(.status.phase == "Running")
        | ([(.spec.containers // [])[] | (.command // []) + (.args // [])] | add // []) as $argv
        | ([$argv[] | splits("[\\s=\"\\x27]+")] | map(select(. != ""))) as $t
        | select(any(range(0; ($t | length) - 1); ($t[.] == "--namespace" or $t[.] == "-n") and $t[. + 1] == $ns))
        | select(any($t[]; . == $rel))
        | "\(.metadata.name): \($argv | join(" ") | .[0:300])"' 2>/dev/null || true)
  if [[ -n "$ops" ]]; then
    _red "  Rancher Helm operation(s) still running in cattle-system for '$RELEASE' (namespace '$NAMESPACE'):"
    printf '%s\n' "$ops" | sed 's/^/    /'
    busy=0
  fi
  return "$busy"
}

# ---------- late-hook sweep ----------
late_hook_objects() {
  {
    kubectl -n "$NAMESPACE" get job,pod -l "app.kubernetes.io/instance=$RELEASE" -o json 2>/dev/null \
      | jq -r '.items[]
          | select(.metadata.deletionTimestamp == null)
          | select(.kind == "Job" or ((.metadata.ownerReferences // []) | all(.kind == "Job")))
          | "\(.kind | ascii_downcase)/\(.metadata.name)"' 2>/dev/null || true
    kubectl -n "$NAMESPACE" get job -o json 2>/dev/null \
      | jq -r --arg p "${RELEASE}-" '.items[]
          | select(.metadata.deletionTimestamp == null)
          | select(.metadata.name | startswith($p))
          | "job/\(.metadata.name)"' 2>/dev/null || true
  } | sort -u
}

late_hook_sweep() {
  local i found total=0
  if [[ "$DRY_RUN" == true ]]; then
    echo "  late-hook sweep: would re-check ${SWEEP_CHECKS}x, ${SWEEP_INTERVAL}s apart, and delete Jobs/Pods of '$RELEASE' that appear"
    return 0
  fi
  for ((i = 1; i <= SWEEP_CHECKS; i++)); do
    sleep "$SWEEP_INTERVAL"
    found=$(late_hook_objects)
    if [[ -z "$found" ]]; then
      echo "  late-hook sweep check $i/$SWEEP_CHECKS: nothing new"
      continue
    fi
    _yellow "  late-hook sweep check $i/$SWEEP_CHECKS: found"
    printf '%s\n' "$found" | sed 's/^/    /'
    total=$((total + $(grep -c . <<<"$found")))
    run "kubectl -n '$NAMESPACE' delete $(tr '\n' ' ' <<<"$found") --ignore-not-found --cascade=foreground --wait=true --timeout=2m"
  done
  if (( total > 0 )); then
    _yellow "  late-hook sweep deleted $total object(s) — a Helm operation was still creating hooks"
  else
    _green "  late-hook sweep: no late Jobs/Pods"
  fi
}

# ---------- pre-flight ----------
_blue "==> Pre-flight checks"
for tool in kubectl helm jq; do
  command -v "$tool" >/dev/null || { _red "$tool not found"; exit 1; }
done

if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  _yellow "  Namespace '$NAMESPACE' does not exist — nothing to do"
  exit 0
fi
if helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1; then
  HELM_RELEASE_EXISTS=true
  _green "  Helm release '$RELEASE' found in namespace '$NAMESPACE'"
else
  HELM_RELEASE_EXISTS=false
  _yellow "  Helm release '$RELEASE' not found — will skip helm uninstall"
fi
if helm_operation_in_progress; then
  if [[ "$FORCE" == true ]]; then
    _yellow "  --force: continuing anyway; the late-hook sweep will catch Jobs created meanwhile"
  else
    _red "  Refusing to uninstall while a Helm operation is running. Wait for it to finish, or pass --force."
    exit 1
  fi
fi
if kubectl -n "$POSTGRES_NAMESPACE" get pod "$PG_POD" >/dev/null 2>&1; then
  PG_POD_FOUND=true
else
  PG_POD_FOUND=false
  _yellow "  Postgres pod '$PG_POD' not found in '$POSTGRES_NAMESPACE' — database and IAM steps will be skipped"
fi

# ---------- plan ----------
_blue "==> Plan"
echo "Will DELETE:"
echo "  - Helm release:     $RELEASE (namespace: $NAMESPACE)"
echo "  - Jobs/Pods/ConfigMaps/Secrets labelled app.kubernetes.io/instance=$RELEASE"
[[ "$KEEP_DB" == false ]]  && echo "  - Console database: $CONSOLE_DB and role $CONSOLE_DB_USER (in $PG_POD)"
[[ "$KEEP_IAM" == false ]] && echo "  - IAM rows:         application '$CONSOLE_MNEMONIC' in DB '$IAM_DB' (with its roles/permissions)"
[[ "$DROP_SIGNING_SECRET" == true ]] && echo "  - Signing Secret:   $SIGNING_SECRET (--drop-signing-secret)"
echo "Will PRESERVE:"
echo "  - Keycloak client '$CONSOLE_MNEMONIC' (staff realm; a reinstall updates it)"
echo "  - Partner Management partners, keys and policies (managed there)"
[[ "$KEEP_DB" == true ]]  && echo "  - Console database $CONSOLE_DB (--keep-db)"
[[ "$KEEP_IAM" == true ]] && echo "  - IAM rows for '$CONSOLE_MNEMONIC' (--keep-iam)"
[[ "$DROP_SIGNING_SECRET" == false ]] && echo "  - Signing Secret '$SIGNING_SECRET' (use --drop-signing-secret to remove)"
echo
for kind in job configmap secret; do
  echo "${kind^}s (label app.kubernetes.io/instance=$RELEASE):"
  kubectl -n "$NAMESPACE" get "$kind" -l "app.kubernetes.io/instance=$RELEASE" --no-headers 2>/dev/null \
    | awk '{print "  - " $1}' || true
done
echo

[[ "$DRY_RUN" == true ]] && _yellow "DRY-RUN: no changes will be made."
if [[ "$ASSUME_YES" == false && "$DRY_RUN" == false ]]; then
  _red "Type the release name ('$RELEASE') to confirm:"
  read -r CONFIRM
  [[ "$CONFIRM" != "$RELEASE" ]] && { _red "Confirmation did not match. Aborting."; exit 1; }
fi

# ---------- 1. stop jobs ----------
_blue "==> [1/7] Stop in-flight Jobs"
run "kubectl -n '$NAMESPACE' delete job -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --wait=false"

# ---------- 2. helm uninstall ----------
_blue "==> [2/7] Helm uninstall"
if [[ "$HELM_RELEASE_EXISTS" == true ]]; then
  run "helm uninstall '$RELEASE' -n '$NAMESPACE' --wait --timeout 5m || true"
else
  echo "  (skipped — release not present)"
fi

# ---------- 3. leftover jobs ----------
_blue "==> [3/7] Delete leftover Jobs and their Pods"
run "kubectl -n '$NAMESPACE' delete job -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --wait=true --timeout=2m"
run "kubectl -n '$NAMESPACE' delete pod -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --field-selector=status.phase!=Running"
late_hook_sweep

# ---------- 4. secrets / configmaps ----------
_blue "==> [4/7] Sweep leftover ConfigMaps / Secrets"
run "kubectl -n '$NAMESPACE' delete configmap -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
# The signing Secret is yours (no release label), so the label sweep never touches it.
run "kubectl -n '$NAMESPACE' delete secret    -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"

# ---------- 5. console database ----------
_blue "==> [5/7] Drop the console database and role"
if [[ "$KEEP_DB" == true ]]; then
  _yellow "  (skipped — --keep-db)"
elif [[ "$PG_POD_FOUND" != true ]]; then
  echo "  (skipped — Postgres pod not reachable)"
else
  kexec_psql postgres "REVOKE CONNECT ON DATABASE \\\"$CONSOLE_DB\\\" FROM PUBLIC;"
  kexec_psql postgres "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$CONSOLE_DB' AND pid <> pg_backend_pid();"
  kexec_psql postgres "DROP DATABASE IF EXISTS \\\"$CONSOLE_DB\\\";"
  if [[ "$DRY_RUN" == true || "$(kexec_psql_capture postgres "SELECT 1 FROM pg_roles WHERE rolname = '$CONSOLE_DB_USER'")" == "1" ]]; then
    kexec_psql postgres "REASSIGN OWNED BY \\\"$CONSOLE_DB_USER\\\" TO postgres;"
    kexec_psql postgres "DROP OWNED BY \\\"$CONSOLE_DB_USER\\\";"
    kexec_psql postgres "DROP ROLE IF EXISTS \\\"$CONSOLE_DB_USER\\\";"
  else
    echo "  (role '$CONSOLE_DB_USER' not present)"
  fi
fi

# ---------- 6. IAM rows ----------
# The console registers itself in IAM at install (application_mnemonic =
# agri-composite); `helm uninstall` does not touch IAM. Remove its rows, child
# rows first, strictly by this mnemonic.
_blue "==> [6/7] Clean the console's IAM rows"
if [[ "$KEEP_IAM" == true ]]; then
  _yellow "  (skipped — --keep-iam)"
elif [[ "$PG_POD_FOUND" != true ]]; then
  echo "  (skipped — Postgres pod not reachable; cannot reach the IAM DB)"
elif [[ "$DRY_RUN" == true ]]; then
  echo "  Would delete IAM rows for application '$CONSOLE_MNEMONIC' from DB '$IAM_DB' (if the DB/tables exist)"
else
  if [[ "$(kexec_psql_capture postgres "SELECT 1 FROM pg_database WHERE datname = '$IAM_DB'")" != "1" ]]; then
    _yellow "  (skipped — IAM database '$IAM_DB' not found)"
  elif [[ "$(kexec_psql_capture "$IAM_DB" "SELECT to_regclass('public.staff_portal_applications') IS NOT NULL")" != "t" ]]; then
    _yellow "  (skipped — table staff_portal_applications not found in IAM DB '$IAM_DB')"
  else
    APP_FILTER="SELECT id FROM staff_portal_applications WHERE application_mnemonic = '$CONSOLE_MNEMONIC'"
    kexec_psql "$IAM_DB" "DELETE FROM staff_role_permissions WHERE role_id IN (SELECT id FROM staff_roles WHERE application_id IN ($APP_FILTER));"
    kexec_psql "$IAM_DB" "DELETE FROM staff_roles WHERE application_id IN ($APP_FILTER);"
    kexec_psql "$IAM_DB" "DELETE FROM staff_application_permissions WHERE application_id IN ($APP_FILTER);"
    kexec_psql "$IAM_DB" "DELETE FROM staff_portal_applications WHERE application_mnemonic = '$CONSOLE_MNEMONIC';"
  fi
fi

# ---------- 7. signing secret ----------
_blue "==> [7/7] Signing Secret"
if [[ "$DROP_SIGNING_SECRET" == true ]]; then
  run "kubectl -n '$NAMESPACE' delete secret '$SIGNING_SECRET' --ignore-not-found"
else
  echo "  (kept — '$SIGNING_SECRET')"
fi

echo
_green "==> Done."
[[ "$DRY_RUN" == true ]] && _yellow "    (dry-run — nothing was actually changed)"
exit 0
