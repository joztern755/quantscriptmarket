#!/usr/bin/env bash
# aijalon.trade — application deploy steps. Called by .github/workflows/deploy.yml (as the deployer SA via
# Workload Identity Federation); the owner can run the same steps locally for a break-glass deploy.
#
#   PROJECT_ID=... REGION=... DB_PRIVATE_IP=... ./infra/gcp/deploy.sh preflight
#   ./infra/gcp/deploy.sh images      # build + push backend (and sandbox) images, record digests
#   ./infra/gcp/deploy.sh migrate     # Cloud Run Job "migrate" BEFORE any new revision takes traffic
#   ./infra/gcp/deploy.sh services    # sandbox -> executor -> api (records previous revisions for rollback)
#   ./infra/gcp/deploy.sh smoke-api   # api health + edge enforcement (+ run.app origin NOT reachable)
#   ./infra/gcp/deploy.sh smoke-web   # served CSP == infra/csp.txt, new build live, .well-known
#   ./infra/gcp/deploy.sh rollback    # route 100% back to the recorded previous api/executor revisions
#
# State between steps (image digests, previous revisions) lives in $DEPLOY_STATE (default $RUNNER_TEMP).
# Migrations must be backward compatible with the revision still serving (expand -> deploy -> contract),
# because they run before traffic shifts and a rollback does not undo them.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
: "${PROJECT_ID:?PROJECT_ID required}"
: "${REGION:?REGION required}"
# shellcheck source=infra/gcp/env.sh
source "${HERE}/env.sh"
export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

: "${GIT_SHA:=$(git -C "${REPO_ROOT}" rev-parse HEAD)}"
: "${DEPLOY_STATE:=${RUNNER_TEMP:-/tmp}/aijalon-deploy.state}"
: "${LAUNCH_PHASE:=internal}"
: "${ALLOWLIST_EMAILS:=${OWNER_EMAIL}}"
: "${PAYOUTS_ENABLED:=false}"
: "${OPS_EMAILS:=${ALERT_EMAIL}}"
: "${SANDBOX_DOCKERFILE:=sandbox/Dockerfile}"
: "${SANDBOX_CONTEXT:=.}"               # build context for the sandbox image, relative to the repo root
GIT_SHA_SHORT="${GIT_SHA:0:12}"

touch "${DEPLOY_STATE}"
state_get() { grep -E "^$1=" "${DEPLOY_STATE}" | tail -1 | cut -d= -f2- || true; }
state_set() { echo "$1=$2" >> "${DEPLOY_STATE}"; }
urlenc_at() { printf '%s' "$1" | sed 's/@/%40/g'; }
svc_url() { gcloud run services describe "$1" --region="${REGION}" --format='value(status.url)'; }

export_render_env() {
  export PROJECT_ID REGION GIT_SHA_SHORT VPC RUN_SUBNET RUN_NET_TAG SANDBOX_VPC SANDBOX_SUBNET SANDBOX_NET_TAG \
    SANDBOX_EXEC_ENV SANDBOX_CONNECTOR SA_API SA_EXECUTOR SA_SANDBOX SA_SCHEDULER SA_MIGRATOR KMS_KEY_NAME DB_NAME \
    WEB_DOMAIN API_DOMAIN SQL_CONNECTION_NAME CLOUDSQL_PROXY_IMAGE DB_MIGRATOR_USER MIGRATE_CMD \
    LAUNCH_PHASE ALLOWLIST_EMAILS PAYOUTS_ENABLED STRIPE_PUBLISHABLE_KEY OPS_EMAILS
  DB_IAM_USER_API_URLENC="$(urlenc_at "${DB_IAM_USER_API}")"
  DB_IAM_USER_EXECUTOR_URLENC="$(urlenc_at "${DB_IAM_USER_EXECUTOR}")"
  BACKEND_IMAGE="$(state_get BACKEND_IMAGE)"
  SANDBOX_IMAGE="$(state_get SANDBOX_IMAGE)"
  EXECUTOR_URL="$(svc_url "${EXECUTOR_SERVICE}")"
  SANDBOX_URL="$(svc_url "${SANDBOX_SERVICE}")"
  export DB_IAM_USER_API_URLENC DB_IAM_USER_EXECUTOR_URLENC BACKEND_IMAGE SANDBOX_IMAGE EXECUTOR_URL SANDBOX_URL
}

render() { python3 "${HERE}/render.py" "$1"; }

cmd_preflight() {
  log "preflight"
  local bad=0
  [[ -f "${REPO_ROOT}/backend/requirements.lock" ]] || { warn "backend/requirements.lock missing (make lock)"; bad=1; }
  if grep -q 'sha256:PIN_ME' "${REPO_ROOT}/backend/Dockerfile" "${REPO_ROOT}/sandbox/Dockerfile" "${HERE}/env.sh"; then
    warn "image digests not pinned (PIN_ME) in backend/Dockerfile, sandbox/Dockerfile or infra/gcp/env.sh (make pin)"; bad=1
  fi
  if [[ -f "${REPO_ROOT}/${SANDBOX_DOCKERFILE}" ]] && grep -Eq '^FROM [^@ ]+( |$)' "${REPO_ROOT}/${SANDBOX_DOCKERFILE}"; then
    warn "${SANDBOX_DOCKERFILE}: base image not pinned by digest (go-live gate G1)"
  fi
  [[ -n "${DB_PRIVATE_IP:-}" ]] || { warn "DB_PRIVATE_IP (GitHub variable) is empty"; bad=1; }
  [[ "${STRIPE_PUBLISHABLE_KEY:-}" =~ ^pk_(live|test)_ ]] || { warn "STRIPE_PUBLISHABLE_KEY (GitHub variable) missing"; bad=1; }
  python3 "${REPO_ROOT}/infra/csp_sync.py" check >/dev/null || { warn "firebase.json CSP != infra/csp.txt"; bad=1; }
  ((bad == 0)) || die "preflight failed"
  log "preflight ok"
}

cmd_images() {
  log "build + push images for ${GIT_SHA_SHORT}"
  gcloud auth configure-docker "${AR_HOST}" --quiet >/dev/null
  local tag="${BACKEND_IMAGE_REPO}:${GIT_SHA}"
  docker build --file "${REPO_ROOT}/backend/Dockerfile" --tag "${tag}" \
    --label "org.opencontainers.image.revision=${GIT_SHA}" "${REPO_ROOT}/backend"
  docker push "${tag}" >/dev/null
  local d; d="$(docker inspect --format='{{index .RepoDigests 0}}' "${tag}")"
  [[ "${d}" =~ @sha256:[0-9a-f]{64}$ ]] || die "could not resolve backend digest (${d})"
  state_set BACKEND_IMAGE "${d}"; log "backend = ${d}"
  if [[ -f "${REPO_ROOT}/${SANDBOX_DOCKERFILE}" ]]; then
    tag="${SANDBOX_IMAGE_REPO}:${GIT_SHA}"
    docker build --file "${REPO_ROOT}/${SANDBOX_DOCKERFILE}" --tag "${tag}" \
      --label "org.opencontainers.image.revision=${GIT_SHA}" "${REPO_ROOT}/${SANDBOX_CONTEXT}"
    docker push "${tag}" >/dev/null
    d="$(docker inspect --format='{{index .RepoDigests 0}}' "${tag}")"
    [[ "${d}" =~ @sha256:[0-9a-f]{64}$ ]] || die "could not resolve sandbox digest (${d})"
    state_set SANDBOX_IMAGE "${d}"; log "sandbox = ${d}"
  else
    warn "${SANDBOX_DOCKERFILE} not found — sandbox service keeps its current revision"
  fi
}

cmd_migrate() {
  export_render_env
  [[ -n "${BACKEND_IMAGE}" ]] || die "no BACKEND_IMAGE in ${DEPLOY_STATE} (run images first)"
  export DB_PRIVATE_IP="${DB_PRIVATE_IP:?DB_PRIVATE_IP required}"
  local f; f="$(mktemp --suffix=.yaml)"
  render "${HERE}/run/migrate.job.yaml" > "${f}"
  log "migrate job -> ${BACKEND_IMAGE}"
  gcloud run jobs replace "${f}" --region="${REGION}" >/dev/null
  gcloud run jobs execute "${MIGRATE_JOB}" --region="${REGION}" --wait
  rm -f "${f}"
}

replace_service() { # service template
  local svc="$1" tpl="$2" f
  f="$(mktemp --suffix=.yaml)"
  render "${tpl}" > "${f}"
  log "deploy ${svc}"
  gcloud run services replace "${f}" --region="${REGION}" >/dev/null
  rm -f "${f}"
  log "  ${svc} -> $(gcloud run services describe "${svc}" --region="${REGION}" --format='value(status.latestReadyRevisionName)')"
}

cmd_services() {
  export_render_env
  [[ -n "${BACKEND_IMAGE}" ]] || die "no BACKEND_IMAGE in ${DEPLOY_STATE} (run images first)"
  local s
  for s in "${API_SERVICE}" "${EXECUTOR_SERVICE}"; do
    state_set "PREV_${s^^}" "$(gcloud run services describe "${s}" --region="${REGION}" --format='value(status.latestReadyRevisionName)')"
  done
  if [[ -n "${SANDBOX_IMAGE}" ]]; then
    if [[ "${SANDBOX_EGRESS_MODE}" == "connector" ]]; then
      replace_service "${SANDBOX_SERVICE}" "${HERE}/run/sandbox.connector.service.yaml"
    else
      replace_service "${SANDBOX_SERVICE}" "${HERE}/run/sandbox.service.yaml"
    fi
  fi
  replace_service "${EXECUTOR_SERVICE}" "${HERE}/run/executor.service.yaml"
  replace_service "${API_SERVICE}" "${HERE}/run/api.service.yaml"
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@" || true; }

cmd_smoke_api() {
  log "smoke: api"
  local body
  body="$(curl -fsS --retry 10 --retry-all-errors --retry-delay 6 --max-time 15 "https://${API_DOMAIN}/healthz")" \
    || die "https://${API_DOMAIN}/healthz failing"
  [[ "${body}" == *'"ok"'* ]] || die "unexpected /healthz body: ${body}"
  local c
  c="$(http_code -X POST "https://${API_DOMAIN}/v1/internal/tick")"
  [[ "${c}" == "403" ]] || die "edge did not block /v1/internal/* (got ${c})"
  c="$(http_code "$(svc_url "${API_SERVICE}")/healthz")"
  [[ "${c}" != 2* ]] || die "api run.app origin is publicly reachable (got ${c}) — ingress misconfigured"
  c="$(http_code "$(svc_url "${EXECUTOR_SERVICE}")/healthz")"
  [[ "${c}" != 2* ]] || die "executor is publicly reachable (got ${c}) — ingress misconfigured"
  log "smoke api ok"
}

cmd_smoke_web() {
  log "smoke: web"
  local want got hdrs i
  want="$(tr -d '\n' < "${REPO_ROOT}/infra/csp.txt")"
  for i in $(seq 1 10); do
    hdrs="$(curl -sSI --max-time 15 "https://${WEB_DOMAIN}/" || true)"
    got="$(printf '%s' "${hdrs}" | tr -d '\r' | sed -n 's/^[Cc]ontent-[Ss]ecurity-[Pp]olicy: //p' | head -1)"
    [[ "${got}" == "${want}" ]] && break
    sleep 6
  done
  [[ "${got}" == "${want}" ]] || die "served CSP differs from infra/csp.txt: '${got}'"
  printf '%s' "${hdrs}" | grep -qi '^strict-transport-security: max-age=63072000' || die "HSTS header missing"
  if [[ -f "${REPO_ROOT}/web/dist/build-info.json" ]]; then
    local build live
    build="$(python3 -c "import json;print(json.load(open('${REPO_ROOT}/web/dist/build-info.json'))['build'])")"
    live="$(curl -fsS --max-time 15 "https://${WEB_DOMAIN}/build-info.json" | python3 -c "import json,sys;print(json.load(sys.stdin)['build'])")"
    [[ "${build}" == "${live}" ]] || die "live build ${live} != deployed build ${build}"
  fi
  local pid; pid="$(curl -fsS --max-time 15 "https://${WEB_DOMAIN}/app-config.json" | python3 -c "import json,sys;print(json.load(sys.stdin)['firebase']['projectId'])")"
  [[ "${pid}" == "${PROJECT_ID}" ]] || die "live app-config projectId=${pid}"
  if ls "${REPO_ROOT}/infra/hosting/well-known/"* 2>/dev/null | grep -qv README.md; then
    [[ "$(http_code "https://${WEB_DOMAIN}/.well-known/apple-developer-merchantid-domain-association")" == "200" ]] \
      || die "Apple Pay domain association file not served"
  fi
  log "smoke web ok"
}

cmd_rollback() {
  local s prev
  for s in "${API_SERVICE}" "${EXECUTOR_SERVICE}"; do
    prev="$(state_get "PREV_${s^^}")"
    if [[ -n "${prev}" ]]; then
      warn "rolling ${s} back to ${prev}"
      gcloud run services update-traffic "${s}" --region="${REGION}" --to-revisions="${prev}=100" >/dev/null
    fi
  done
  warn "Hosting is not rolled back automatically: Firebase console > Hosting > release history > Rollback"
}

case "${1:-}" in
  preflight) cmd_preflight ;;
  images)    cmd_images ;;
  migrate)   cmd_migrate ;;
  services)  cmd_services ;;
  smoke-api) cmd_smoke_api ;;
  smoke-web) cmd_smoke_web ;;
  rollback)  cmd_rollback ;;
  *) die "usage: $0 preflight|images|migrate|services|smoke-api|smoke-web|rollback" ;;
esac
