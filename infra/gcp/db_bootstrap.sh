#!/usr/bin/env bash
# aijalon.trade — database bootstrap (idempotent). Run AFTER infra/gcp/bootstrap.sh, from the repo root.
#
#   ./infra/gcp/db_bootstrap.sh                 # all: db, users, open, pre, migrate, grants, verify, close
#   ./infra/gcp/db_bootstrap.sh migrate         # re-run migrations only (normally deploy.yml's Cloud Run Job does it)
#   ./infra/gcp/db_bootstrap.sh verify          # re-assert the privilege model
#   ./infra/gcp/db_bootstrap.sh promote-admin someone@example.com
#   ./infra/gcp/db_bootstrap.sh psql            # break-glass interactive session as `migrator` (alerts fire)
#   ./infra/gcp/db_bootstrap.sh close           # make sure the temporary public IP is gone
#
# Identities:
#   * `migrator`  — built-in Cloud SQL user (cloudsqlsuperuser member, CREATEROLE). Owns the schema and runs
#                   migrations. Its password exists ONLY in Secret Manager (DB_MIGRATOR_PASSWORD, readable by
#                   the aijalon-migrator SA and project owners). `postgres` gets a random, discarded password.
#   * api / executor — Cloud SQL IAM service-account users (no passwords), granted app_api / app_executor.
#
# Reaching a private-IP-only instance from a laptop: the instance has NO public IP. For the duration of this
# script we add a public IP with ZERO authorized networks (only the Cloud SQL Auth Proxy, which authenticates
# with your Google identity and uses mTLS, can connect), and remove it again on exit (trap). If the script is
# killed hard, run `./infra/gcp/db_bootstrap.sh close`. bootstrap.sh's `sql` step also warns if it is left on.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-aijalon-trade-prod}"
REGION="${REGION:-asia-southeast1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
# shellcheck source=infra/gcp/env.sh
source "${HERE}/env.sh"
export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

: "${LOCAL_PORT:=6543}"
: "${PYTHON:=python3}"                 # must have backend/requirements installed (make venv)
: "${ALLOW_TEMP_PUBLIC_IP:=}"          # set to 1 to skip the confirmation prompt

need gcloud; need psql; need cloud-sql-proxy; need openssl
mkdir -p "${OUT_DIR}"; chmod 700 "${OUT_DIR}"

PROXY_PID=""
OPENED_PUBLIC_IP=0

exists() { "$@" >/dev/null 2>&1; }
migrator_password() { gcloud secrets versions access latest --secret=DB_MIGRATOR_PASSWORD; }
public_ip_enabled() {
  [[ "$(gcloud sql instances describe "${SQL_INSTANCE}" --format='value(settings.ipConfiguration.ipv4Enabled)')" == "True" ]]
}

cleanup() {
  local rc=$?
  if [[ -n "${PROXY_PID}" ]]; then kill "${PROXY_PID}" 2>/dev/null || true; wait "${PROXY_PID}" 2>/dev/null || true; fi
  if [[ "${OPENED_PUBLIC_IP}" == "1" ]]; then
    log "removing the temporary public IP from ${SQL_INSTANCE}"
    gcloud sql instances patch "${SQL_INSTANCE}" --no-assign-ip --quiet >/dev/null \
      || warn "COULD NOT REMOVE PUBLIC IP — run: ./infra/gcp/db_bootstrap.sh close"
  fi
  exit "${rc}"
}
trap cleanup EXIT INT TERM

step_db() {
  log "database ${DB_NAME}"
  exists gcloud sql databases describe "${DB_NAME}" --instance="${SQL_INSTANCE}" || \
    gcloud sql databases create "${DB_NAME}" --instance="${SQL_INSTANCE}" --charset=UTF8
}

step_users() {
  log "users: ${DB_MIGRATOR_USER} (built-in), ${DB_IAM_USER_API}, ${DB_IAM_USER_EXECUTOR} (IAM)"
  migrator_password >/dev/null || die "DB_MIGRATOR_PASSWORD secret has no value (run bootstrap.sh secrets)"
  migrator_password | sql_user_password "${SQL_INSTANCE}" "${DB_MIGRATOR_USER}"   # create or set; never in argv
  local u
  for u in "${SA_API}" "${SA_EXECUTOR}"; do
    # gcloud takes the SA e-mail without ".gserviceaccount.com" for IAM service-account users
    local dbu="${u%.gserviceaccount.com}"
    exists gcloud sql users describe "${dbu}" --instance="${SQL_INSTANCE}" || \
      gcloud sql users create "${dbu}" --instance="${SQL_INSTANCE}" --type=cloud_iam_service_account >/dev/null
  done
}

step_open() {
  if public_ip_enabled; then
    warn "${SQL_INSTANCE} already has a public IP (no authorized networks expected); it will be removed on exit"
    OPENED_PUBLIC_IP=1
  else
    if [[ "${ALLOW_TEMP_PUBLIC_IP}" != "1" ]]; then
      read -r -p "Temporarily add a public IP (no authorized networks, proxy-only) to ${SQL_INSTANCE}? [y/N] " a
      [[ "${a}" == "y" || "${a}" == "Y" ]] || die "aborted"
    fi
    log "adding a temporary public IP (Auth-Proxy-only; removed on exit)"
    OPENED_PUBLIC_IP=1
    gcloud sql instances patch "${SQL_INSTANCE}" --assign-ip --quiet >/dev/null
  fi
  log "starting cloud-sql-proxy on 127.0.0.1:${LOCAL_PORT}"
  cloud-sql-proxy --port "${LOCAL_PORT}" --address 127.0.0.1 "${SQL_CONNECTION_NAME}" >"${OUT_DIR}/cloud-sql-proxy.log" 2>&1 &
  PROXY_PID=$!
  local i
  for i in $(seq 1 60); do
    if PGPASSWORD="$(migrator_password)" psql "host=127.0.0.1 port=${LOCAL_PORT} dbname=postgres user=${DB_MIGRATOR_USER} sslmode=disable" -Atqc 'select 1' >/dev/null 2>&1; then
      return 0
    fi
    kill -0 "${PROXY_PID}" 2>/dev/null || die "cloud-sql-proxy exited — see ${OUT_DIR}/cloud-sql-proxy.log"
    sleep 2
  done
  die "could not connect through the proxy after 120 s"
}

run_psql() { # file [extra psql args...]
  local f="$1"; shift
  PGPASSWORD="$(migrator_password)" psql "host=127.0.0.1 port=${LOCAL_PORT} dbname=${DB_NAME} user=${DB_MIGRATOR_USER} sslmode=disable" \
    -X -v ON_ERROR_STOP=1 -v db="${DB_NAME}" -v api_user="${DB_IAM_USER_API}" -v executor_user="${DB_IAM_USER_EXECUTOR}" \
    "$@" -f "${f}"
}

step_pre() { log "pre-migration SQL"; run_psql "${HERE}/sql/00_pre_migrate.sql"; }

step_migrate() {
  log "migrations via local Auth Proxy (${MIGRATE_CMD}, cwd backend/)"
  [[ -d "${REPO_ROOT}/backend/migrations" ]] || die "backend/migrations not found"
  local script="${MIGRATE_CMD#python }"
  [[ -f "${REPO_ROOT}/backend/${script}" ]] || die "backend/${script} not found — migration runner not written yet"
  (
    cd "${REPO_ROOT}/backend"
    DATABASE_URL="postgresql://${DB_MIGRATOR_USER}:$(migrator_password)@127.0.0.1:${LOCAL_PORT}/${DB_NAME}?sslmode=disable" \
    SERVICE_ROLE=migrate "${PYTHON}" "${script}"
  )
}

step_grants() { log "grants (IAM users -> app roles, per-login limits)"; run_psql "${HERE}/sql/10_grants.sql"; }
step_verify() { log "verify privilege model"; run_psql "${HERE}/sql/20_verify.sql"; }

step_close() {
  if public_ip_enabled; then
    log "removing public IP from ${SQL_INSTANCE}"
    gcloud sql instances patch "${SQL_INSTANCE}" --no-assign-ip --quiet >/dev/null
  fi
  OPENED_PUBLIC_IP=0
  log "public IP: $(public_ip_enabled && echo STILL ON || echo off)"
}

main() {
  local cmd="${1:-all}"; shift || true
  case "${cmd}" in
    all)           step_db; step_users; step_open; step_pre; step_migrate; step_grants; step_verify ;;
    db)            step_db ;;
    users)         step_users ;;
    migrate)       step_open; step_pre; step_migrate; step_grants; step_verify ;;
    grants)        step_open; step_grants; step_verify ;;
    verify)        step_open; step_verify ;;
    promote-admin) [[ $# -eq 1 ]] || die "usage: $0 promote-admin <email>"
                   step_open; run_psql "${HERE}/sql/30_promote_admin.sql" -v email="$1" ;;
    psql)          step_open
                   warn "break-glass session as ${DB_MIGRATOR_USER}; every statement is pgaudit-logged (DDL/ROLE)"
                   PGPASSWORD="$(migrator_password)" psql "host=127.0.0.1 port=${LOCAL_PORT} dbname=${DB_NAME} user=${DB_MIGRATOR_USER} sslmode=disable" ;;
    close)         step_close ;;
    *)             die "unknown command ${cmd}" ;;
  esac
  log "done: ${cmd}"
}
main "$@"
