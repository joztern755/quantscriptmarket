#!/usr/bin/env bash
# aijalon.trade — application deploy steps. Called by .github/workflows/deploy.yml; each step runs as the CI
# identity of its GitHub environment (REVIEW_WEB_INFRA H2; bootstrap `sa`/`wif`); the owner can run the same steps
# locally for a break-glass deploy.
#
#   PROJECT_ID=... REGION=... DB_PRIVATE_IP=... ./infra/gcp/deploy.sh preflight
#   ./infra/gcp/deploy.sh images      # [builder]  build + push backend (and sandbox) images, record digests
#   ./infra/gcp/deploy.sh attest      # [builder]  Binary Authorization attestation of those digests (KMS)
#   ./infra/gcp/deploy.sh migrate     # [deployer] Cloud Run Job "migrate" BEFORE any new revision takes traffic
#   ./infra/gcp/deploy.sh services    # [deployer] sandbox -> executor CANARY (0 %, tick paused, selftest) -> api
#   ./infra/gcp/deploy.sh smoke-api   # api health + edge enforcement (+ run.app origin NOT reachable)
#   ./infra/gcp/deploy.sh hosting     # [hosting]  Firebase Hosting REST deploy of web/dist (infra/hosting/deploy_hosting.py)
#   ./infra/gcp/deploy.sh smoke-web   # served CSP == infra/csp.txt on every document path, new build live, .well-known
#   ./infra/gcp/deploy.sh rollback    # route 100% back to the recorded previous api/executor/sandbox revisions
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
# Plain (non-secret) runtime settings rendered into infra/gcp/run/*.yaml. deploy.yml passes the GitHub environment
# variables of the same name; an unset/empty variable takes the default below (= app/config.py defaults).
# ALLOWLIST_EMAILS / OPS_EMAILS are Secret Manager secrets (personal data), not variables. Table: DEPLOY.md §5.3.
: "${LAUNCH_PHASE:=internal}"
: "${PAYOUTS_ENABLED:=false}"
# No allocation / leverage caps (owner 30 Sep 2026, SPEC §12 "No caps"): MAX_ALLOCATION_PER_USER_USD,
# MAX_TOTAL_PLATFORM_ALLOCATION_USD and MAX_USER_LEVERAGE are deliberately NOT rendered into any template
# (unset = no cap in app/config.py). Re-introducing a cap = add the variable to api.service.yaml + here.
: "${STRIPE_MAX_TOPUP_USD:=10000}"
: "${STRIPE_FEE_ESTIMATE_BPS:=0}"            # 0 = no pre-payment fee estimate shown [CONFIRM from Stripe MY pricing]
: "${STRIPE_FEE_ESTIMATE_FIXED_USD:=0}"
: "${STRIPE_API_VERSION:=}"                  # empty = the Stripe account's default API version
: "${KYC_PROVIDER:=manual}"                  # manual | sumsub
: "${KYC_LEVEL_NAME:=}"                      # sumsub only
: "${TELEGRAM_BOT_USERNAME:=}"               # required (preflight): BotFather username, e.g. aijalon_alerts_bot
: "${SANDBOX_DOCKERFILE:=sandbox/Dockerfile}"
: "${SANDBOX_CONTEXT:=.}"               # build context for the sandbox image, relative to the repo root
: "${BINAUTHZ_ANNOTATE:=1}"              # render run.googleapis.com/binary-authorization: default (bootstrap `binauthz`)
: "${DB_TLS_HOST:=}"                     # Cloud SQL DNS name -> migrate sslmode=verify-full; empty -> verify-ca on the IP
: "${SELFTEST_TIMEOUT_S:=300}"
: "${SELFTEST_POLL_S:=10}"
: "${SELFTEST_SETTLE_S:=90}"
GIT_SHA_SHORT="${GIT_SHA:0:12}"

touch "${DEPLOY_STATE}"
state_get() { grep -E "^$1=" "${DEPLOY_STATE}" | tail -1 | cut -d= -f2- || true; }
state_set() { echo "$1=$2" >> "${DEPLOY_STATE}"; }
urlenc_at() { printf '%s' "$1" | sed 's/@/%40/g'; }
svc_url() { gcloud run services describe "$1" --region="${REGION}" --format='value(status.url)'; }

export_render_env() {
  export PROJECT_ID REGION GIT_SHA_SHORT VPC RUN_SUBNET RUN_NET_TAG SANDBOX_VPC SANDBOX_SUBNET SANDBOX_NET_TAG \
    SANDBOX_EXEC_ENV SANDBOX_CONNECTOR SA_API SA_EXECUTOR SA_SANDBOX SA_SCHEDULER SA_MIGRATOR KMS_KEY_NAME DB_NAME \
    EXEC_SUBNET KMS_CODE_KEY_NAME \
    WEB_DOMAIN API_DOMAIN SQL_CONNECTION_NAME CLOUDSQL_PROXY_IMAGE DB_MIGRATOR_USER MIGRATE_CMD \
    LAUNCH_PHASE PAYOUTS_ENABLED STRIPE_PUBLISHABLE_KEY STRIPE_MAX_TOPUP_USD STRIPE_FEE_ESTIMATE_BPS \
    STRIPE_FEE_ESTIMATE_FIXED_USD STRIPE_API_VERSION KYC_PROVIDER KYC_LEVEL_NAME TELEGRAM_BOT_USERNAME \
    KMS_ATTEST_KEY_VERSION_NAME BINAUTHZ_ANNOTATE CANDIDATE_TAG
  : "${EXECUTOR_CANARY:=0}"
  export EXECUTOR_CANARY
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
  [[ "${TELEGRAM_BOT_USERNAME}" =~ ^[A-Za-z][A-Za-z0-9_]{3,30}[Bb][Oo][Tt]$ ]] \
    || { warn "TELEGRAM_BOT_USERNAME (GitHub variable) missing/invalid — BotFather username without @ (DEPLOY.md §5.1)"; bad=1; }
  [[ "${LAUNCH_PHASE}" == "internal" || "${LAUNCH_PHASE}" == "public" ]] || { warn "LAUNCH_PHASE must be internal|public"; bad=1; }
  [[ "${PAYOUTS_ENABLED}" == "true" || "${PAYOUTS_ENABLED}" == "false" ]] || { warn "PAYOUTS_ENABLED must be true|false"; bad=1; }
  [[ "${KYC_PROVIDER}" == "manual" || "${KYC_PROVIDER}" == "sumsub" ]] || { warn "KYC_PROVIDER must be manual|sumsub"; bad=1; }
  if [[ "${KYC_PROVIDER}" == "sumsub" && -z "${KYC_LEVEL_NAME}" ]]; then warn "KYC_PROVIDER=sumsub needs KYC_LEVEL_NAME"; bad=1; fi
  local n
  for n in STRIPE_MAX_TOPUP_USD STRIPE_FEE_ESTIMATE_BPS STRIPE_FEE_ESTIMATE_FIXED_USD; do
    [[ "${!n}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { warn "${n}='${!n}' is not a non-negative number"; bad=1; }
  done
  [[ -n "${STRIPE_API_VERSION}" ]] || warn "STRIPE_API_VERSION empty: Stripe uses the account default (pin it to the webhook endpoint's version)"
  python3 "${REPO_ROOT}/infra/csp_sync.py" check >/dev/null || { warn "firebase.json CSP != infra/csp.txt"; bad=1; }
  # REVIEW_WEB_INFRA M2: the production web build refuses to build without SRI; fail early here too
  [[ -f "${REPO_ROOT}/web/sri.json" ]] || { warn "web/sri.json missing — run 'make sri' (with network), review, commit"; bad=1; }
  # REVIEW_WEB_INFRA H1: until the trust anchors are pinned the site refuses every wallet signature (fail closed)
  if grep -q '"REPLACE_ME"' <(python3 -c "import json;print(json.dumps(json.load(open('${REPO_ROOT}/web/public/app-config.json'))['trust']))"); then
    warn "web/public/app-config.json trust anchors still REPLACE_ME: wallet signing stays disabled (DEPLOY.md §12)"
    [[ "${LAUNCH_PHASE}" == "public" ]] && { warn "LAUNCH_PHASE=public requires pinned trust anchors"; bad=1; }
  fi
  # REVIEW_WEB_INFRA M4: the public smoke test (and its automatic rollback) may be skipped only before go-live
  if [[ "${SMOKE_SKIP_PUBLIC:-}" == "true" && "${LAUNCH_PHASE}" == "public" ]]; then
    warn "SMOKE_SKIP_PUBLIC=true is not allowed with LAUNCH_PHASE=public (delete the GitHub variable)"; bad=1
  fi
  ((bad == 0)) || die "preflight failed"
  log "preflight ok"
}

cmd_images() {
  log "build + push images for ${GIT_SHA_SHORT}"
  gcloud auth configure-docker "${AR_HOST}" --quiet >/dev/null
  local tag="${BACKEND_IMAGE_REPO}:${GIT_SHA}"
  # BuildKit is required: backend/Dockerfile copies /srv/legal (LEGAL_DIR) from the named build context `legal`,
  # and sandbox/Dockerfile uses COPY --chmod
  DOCKER_BUILDKIT=1 docker build --file "${REPO_ROOT}/backend/Dockerfile" --tag "${tag}" \
    --build-context "legal=${REPO_ROOT}/legal" \
    --label "org.opencontainers.image.revision=${GIT_SHA}" "${REPO_ROOT}/backend"
  docker push "${tag}" >/dev/null
  local d; d="$(docker inspect --format='{{index .RepoDigests 0}}' "${tag}")"
  [[ "${d}" =~ @sha256:[0-9a-f]{64}$ ]] || die "could not resolve backend digest (${d})"
  state_set BACKEND_IMAGE "${d}"; log "backend = ${d}"
  if [[ -f "${REPO_ROOT}/${SANDBOX_DOCKERFILE}" ]]; then
    tag="${SANDBOX_IMAGE_REPO}:${GIT_SHA}"
    DOCKER_BUILDKIT=1 docker build --file "${REPO_ROOT}/${SANDBOX_DOCKERFILE}" --tag "${tag}" \
      --label "org.opencontainers.image.revision=${GIT_SHA}" "${REPO_ROOT}/${SANDBOX_CONTEXT}"
    docker push "${tag}" >/dev/null
    d="$(docker inspect --format='{{index .RepoDigests 0}}' "${tag}")"
    [[ "${d}" =~ @sha256:[0-9a-f]{64}$ ]] || die "could not resolve sandbox digest (${d})"
    state_set SANDBOX_IMAGE "${d}"; log "sandbox = ${d}"
  else
    warn "${SANDBOX_DOCKERFILE} not found — sandbox service keeps its current revision"
  fi
}

# REVIEW_WEB_INFRA H2: Binary Authorization attestations, signed with the builder-only KMS key.
cmd_attest() {
  local img
  for img in "$(state_get BACKEND_IMAGE)" "$(state_get SANDBOX_IMAGE)"; do
    [[ -n "${img}" ]] || continue
    [[ "${img}" =~ @sha256:[0-9a-f]{64}$ ]] || die "refusing to attest a non-digest reference ${img}"
    log "attest ${img}"
    gcloud beta container binauthz attestations sign-and-create --artifact-url="${img}" \
      --attestor="${BINAUTHZ_ATTESTOR}" --attestor-project="${PROJECT_ID}" \
      --keyversion-project="${PROJECT_ID}" --keyversion-location="${REGION}" --keyversion-keyring="${KMS_KEYRING}" \
      --keyversion-key="${KMS_BINAUTHZ_KEY}" --keyversion=1 >/dev/null
  done
}

cmd_migrate() {
  export_render_env
  [[ -n "${BACKEND_IMAGE}" ]] || die "no BACKEND_IMAGE in ${DEPLOY_STATE} (run images first)"
  export DB_PRIVATE_IP="${DB_PRIVATE_IP:?DB_PRIVATE_IP required}"
  # REVIEW_WEB_INFRA L6: verify the server certificate (CA bundle secret CLOUDSQL_SERVER_CA, bootstrap `sqlca`)
  if [[ -n "${DB_TLS_HOST}" ]]; then
    export DB_SSLMODE=verify-full
  else
    export DB_TLS_HOST="${DB_PRIVATE_IP}" DB_SSLMODE=verify-ca
  fi
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

# The revision currently receiving 100 % of a service's traffic (not merely the latest ready one).
serving_revision() {
  gcloud run services describe "$1" --region="${REGION}" --format=json | python3 -c '
import json, sys
st = json.load(sys.stdin).get("status", {})
full = [t for t in st.get("traffic", []) if int(t.get("percent") or 0) == 100 and t.get("revisionName")]
print(full[0]["revisionName"] if full else st.get("latestReadyRevisionName", ""))'
}

TICK_BEFORE=""
restore_tick() {   # resume `tick` only if it was running before this deploy paused it (go-live state is sacred)
  if [[ "${TICK_BEFORE}" == "ENABLED" && "$(sched_state tick)" == "PAUSED" ]]; then
    gcloud scheduler jobs resume tick --location="${REGION}" >/dev/null && log "  tick resumed"
  fi
}

sched_state() { gcloud scheduler jobs describe "$1" --location="${REGION}" --format='value(state)' 2>/dev/null || true; }

# Runs the deploy probe (Cloud Scheduler job SELFTEST_JOB -> candidate tag URL, OIDC as the scheduler SA) and waits
# for its result. The deployer may only run/pause/resume that job (bootstrap `sa`), never change its target.
run_selftest() {
  local before st0 t0 now js last code
  js="$(gcloud scheduler jobs describe "${SELFTEST_JOB}" --location="${REGION}" --format=json)" \
    || die "scheduler job ${SELFTEST_JOB} missing — run ./infra/gcp/bootstrap.sh scheduler"
  before="$(printf '%s' "${js}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("lastAttemptTime",""))')"
  st0="$(printf '%s' "${js}" | python3 -c 'import json,sys;print(int((json.load(sys.stdin).get("status") or {}).get("code") or 0))')"
  gcloud scheduler jobs resume "${SELFTEST_JOB}" --location="${REGION}" >/dev/null 2>&1 || true
  gcloud scheduler jobs run "${SELFTEST_JOB}" --location="${REGION}" >/dev/null
  t0="$(date +%s)"
  local seen="" settle=0
  while :; do
    sleep "${SELFTEST_POLL_S}"
    now="$(date +%s)"
    js="$(gcloud scheduler jobs describe "${SELFTEST_JOB}" --location="${REGION}" --format=json)"
    last="$(printf '%s' "${js}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("lastAttemptTime",""))')"
    code="$(printf '%s' "${js}" | python3 -c 'import json,sys;print(int((json.load(sys.stdin).get("status") or {}).get("code") or 0))')"
    if [[ -n "${last}" && "${last}" != "${before}" ]]; then
      [[ -z "${seen}" ]] && seen="${now}"
      # the status of THIS attempt is known once it differs from the previous one, or after a settle period
      # (the attempt deadline is 300 s; the selftest itself takes seconds)
      if [[ "${code}" != "${st0}" ]] || (( now - seen >= SELFTEST_SETTLE_S )); then settle=1; fi
    fi
    (( settle == 1 )) && break
    (( now - t0 > SELFTEST_TIMEOUT_S )) && { code=-1; break; }
  done
  gcloud scheduler jobs pause "${SELFTEST_JOB}" --location="${REGION}" >/dev/null 2>&1 || true
  [[ "${code}" == "0" ]]
}

# REVIEW_WEB_INFRA M4: the executor moves real money every minute, so a new revision never gets traffic blind:
#   1. `tick` is paused (only if it was running; restored on any exit);
#   2. the new revision is deployed at 0 % under the `candidate` tag (serving revision keeps 100 %);
#   3. /v1/internal/selftest runs ON THE CANDIDATE through Cloud Scheduler OIDC (dry-run of the tick's inputs:
#      DB, flags, signals, due subscriptions, Hyperliquid, one KMS agent-key open, the attestation key — no order);
#   4. pass -> 100 % to the candidate, tag removed; fail -> traffic untouched, tag removed, deploy fails.
# Not skippable (SMOKE_SKIP_PUBLIC does not apply to it).
executor_canary() {
  local prev tick_before
  prev="$(serving_revision "${EXECUTOR_SERVICE}")"
  [[ -n "${prev}" ]] || die "cannot determine the executor's serving revision"
  state_set PREV_EXECUTOR "${prev}"
  tick_before="$(sched_state tick)"
  state_set TICK_STATE_BEFORE "${tick_before}"
  TICK_BEFORE="${tick_before}"
  trap restore_tick EXIT             # also on die / set -e failures below
  if [[ "${tick_before}" == "ENABLED" ]]; then
    gcloud scheduler jobs pause tick --location="${REGION}" >/dev/null
    log "  tick paused for the executor rollout"
  else
    log "  tick is ${tick_before:-absent}: left as it is"
  fi
  export EXECUTOR_CANARY=1 PREV_EXECUTOR_REVISION="${prev}"
  replace_service "${EXECUTOR_SERVICE}" "${HERE}/run/executor.service.yaml"
  export EXECUTOR_CANARY=0
  if run_selftest; then
    log "  selftest passed on the candidate: moving 100 % to it"
    gcloud run services update-traffic "${EXECUTOR_SERVICE}" --region="${REGION}" --to-latest >/dev/null
    gcloud run services update-traffic "${EXECUTOR_SERVICE}" --region="${REGION}" --remove-tags="${CANDIDATE_TAG}" >/dev/null || true
  else
    gcloud run services update-traffic "${EXECUTOR_SERVICE}" --region="${REGION}" --remove-tags="${CANDIDATE_TAG}" >/dev/null || true
    die "executor selftest FAILED on the candidate revision — traffic stays on ${prev} (logs: executor_selftest)"
  fi
  restore_tick
  trap - EXIT
}

cmd_services() {
  export_render_env
  [[ -n "${BACKEND_IMAGE}" ]] || die "no BACKEND_IMAGE in ${DEPLOY_STATE} (run images first)"
  state_set PREV_API "$(serving_revision "${API_SERVICE}")"
  state_set PREV_SANDBOX "$(serving_revision "${SANDBOX_SERVICE}")"
  if [[ -n "${SANDBOX_IMAGE}" ]]; then
    if [[ "${SANDBOX_EGRESS_MODE}" == "connector" ]]; then
      replace_service "${SANDBOX_SERVICE}" "${HERE}/run/sandbox.connector.service.yaml"
    else
      replace_service "${SANDBOX_SERVICE}" "${HERE}/run/sandbox.service.yaml"
    fi
  fi
  executor_canary
  replace_service "${API_SERVICE}" "${HERE}/run/api.service.yaml"
}

# REVIEW_WEB_INFRA H2: Hosting is deployed through the Firebase Hosting REST API with the hosting-only identity's
# access token (no firebase-tools / npm while credentials exist). Needs HOSTING_ACCESS_TOKEN (auth step output).
cmd_hosting() {
  : "${HOSTING_ACCESS_TOKEN:?HOSTING_ACCESS_TOKEN required (google-github-actions/auth token_format: access_token)}"
  [[ -f "${REPO_ROOT}/web/dist/index.html" ]] || die "web/dist missing (download the web-dist artifact first)"
  python3 "${REPO_ROOT}/infra/hosting/deploy_hosting.py" --site "${HOSTING_SITE:-${PROJECT_ID}}" \
    --public "${REPO_ROOT}/web/dist" --config "${REPO_ROOT}/firebase.json" \
    --message "deploy ${GIT_SHA_SHORT} run ${GITHUB_RUN_ID:-local}"
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
  local host; host="$(svc_url "${EXECUTOR_SERVICE}")"; host="${host#https://}"
  c="$(http_code "https://${CANDIDATE_TAG}---${host}/healthz")"
  [[ "${c}" != 2* ]] || die "executor candidate tag URL is publicly reachable (got ${c})"
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
  # REVIEW_WEB_INFRA M1: document headers on EVERY path the SPA is served from (not only /), incl. /__x-style paths
  local p h
  for p in /__x /__ /_x /s/x /index.html; do
    h="$(curl -sSI --max-time 15 "https://${WEB_DOMAIN}${p}" | tr -d '\r' || true)"
    printf '%s' "${h}" | grep -qi '^x-frame-options: DENY' || die "X-Frame-Options missing on ${p}"
    printf '%s' "${h}" | grep -qi "^content-security-policy: .*frame-ancestors 'none'" || die "CSP frame-ancestors missing on ${p}"
    printf '%s' "${h}" | grep -qi '^reporting-endpoints: csp=' || die "Reporting-Endpoints missing on ${p}"
  done
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
  for s in "${API_SERVICE}" "${EXECUTOR_SERVICE}" "${SANDBOX_SERVICE}"; do
    prev="$(state_get "PREV_${s^^}")"
    if [[ -n "${prev}" ]]; then
      warn "rolling ${s} back to ${prev}"
      gcloud run services update-traffic "${s}" --region="${REGION}" --to-revisions="${prev}=100" >/dev/null
    fi
  done
  gcloud run services update-traffic "${EXECUTOR_SERVICE}" --region="${REGION}" --remove-tags="${CANDIDATE_TAG}" >/dev/null 2>&1 || true
  if [[ "$(state_get TICK_STATE_BEFORE)" == "ENABLED" && "$(sched_state tick)" == "PAUSED" ]]; then
    gcloud scheduler jobs resume tick --location="${REGION}" >/dev/null && warn "tick resumed (it was running before the deploy)"
  fi
  warn "Hosting is not rolled back automatically: Firebase console > Hosting > release history > Rollback"
}

case "${1:-}" in
  preflight) cmd_preflight ;;
  images)    cmd_images ;;
  attest)    cmd_attest ;;
  migrate)   cmd_migrate ;;
  services)  cmd_services ;;
  smoke-api) cmd_smoke_api ;;
  smoke-web) cmd_smoke_web ;;
  hosting)   cmd_hosting ;;
  rollback)  cmd_rollback ;;
  *) die "usage: $0 preflight|images|attest|migrate|services|smoke-api|hosting|smoke-web|rollback" ;;
esac
