#!/usr/bin/env bash
# aijalon.trade — Google Cloud bootstrap (idempotent). Creates everything in SPEC §2 / §2.1 except the
# application code deploy (that is .github/workflows/deploy.yml) and database roles (db_bootstrap.sh).
#
# Run from the repo root on the owner's machine, signed in as the project owner:
#   gcloud auth login app.aijalon@gmail.com && gcloud auth application-default login
#   BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX ./infra/gcp/bootstrap.sh            # all steps
#   ./infra/gcp/bootstrap.sh network sql                                     # selected steps only
# Steps (in order): project apis firebase audit network kms registry sa secrets sql run lb scheduler wif
#                   monitoring budget harden outputs
# Re-running is safe: every step checks before it creates, and never deletes data.
set -euo pipefail

# ---- the three owner inputs (everything else derives from infra/gcp/env.sh) ------------------------------
PROJECT_ID="${PROJECT_ID:-aijalon-trade-prod}"
REGION="${REGION:-asia-southeast1}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
# ---------------------------------------------------------------------------------------------------------

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/gcp/env.sh
source "${HERE}/env.sh"
export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"   # every gcloud call targets this project; global config untouched
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
need gcloud; need python3; need openssl; need curl
mkdir -p "${OUT_DIR}"; chmod 700 "${OUT_DIR}"

: "${SCHEDULER_START_PAUSED:=1}"   # scheduler jobs are created PAUSED; resuming them is a go-live gate
: "${SQL_IAM_CONDITIONS:=1}"       # scope cloudsql.client/instanceUser to our instance with IAM conditions
: "${API_FIREBASE_AUTH_ADMIN:=0}"  # 1 = api may revoke sessions / reset MFA via Admin SDK (admin console)

project_number() { gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)'; }
exists() { "$@" >/dev/null 2>&1; }

# project-level binding; --condition=None is mandatory once any conditional binding exists (non-interactive)
bind_project() { # member role [condition-expression condition-title]
  if [[ $# -ge 4 ]]; then
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="$1" --role="$2" \
      --condition="expression=$3,title=$4" --quiet >/dev/null
  else
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="$1" --role="$2" --condition=None --quiet >/dev/null
  fi
}

# =========================================================================================================
step_project() {
  log "project ${PROJECT_ID}"
  if ! exists gcloud projects describe "${PROJECT_ID}"; then
    gcloud projects create "${PROJECT_ID}" --name="aijalon-trade" --labels=app=aijalon,env=prod
  fi
  [[ -n "${BILLING_ACCOUNT}" ]] || die "BILLING_ACCOUNT is empty (gcloud billing accounts list)"
  local cur; cur="$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingAccountName)' 2>/dev/null || true)"
  if [[ "${cur}" != "billingAccounts/${BILLING_ACCOUNT}" ]]; then
    gcloud billing projects link "${PROJECT_ID}" --billing-account="${BILLING_ACCOUNT}"
  fi
}

step_apis() {
  log "enable APIs"
  gcloud services enable \
    compute.googleapis.com run.googleapis.com sqladmin.googleapis.com sql-component.googleapis.com \
    servicenetworking.googleapis.com vpcaccess.googleapis.com cloudkms.googleapis.com \
    secretmanager.googleapis.com artifactregistry.googleapis.com containerscanning.googleapis.com \
    containeranalysis.googleapis.com cloudscheduler.googleapis.com iam.googleapis.com \
    iamcredentials.googleapis.com sts.googleapis.com cloudresourcemanager.googleapis.com \
    serviceusage.googleapis.com monitoring.googleapis.com logging.googleapis.com \
    certificatemanager.googleapis.com cloudbilling.googleapis.com billingbudgets.googleapis.com \
    firebase.googleapis.com firebasehosting.googleapis.com identitytoolkit.googleapis.com \
    securetoken.googleapis.com essentialcontacts.googleapis.com
}

step_firebase() {
  log "Firebase (project + web app)"
  if ! command -v firebase >/dev/null 2>&1; then
    warn "firebase CLI not found: run 'firebase projects:addfirebase ${PROJECT_ID}' yourself (DEPLOY.md §4)"; return 0
  fi
  if ! firebase projects:list --json 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);sys.exit(0 if any(p.get('projectId')=='${PROJECT_ID}' for p in d.get('result',[])) else 1)"; then
    firebase projects:addfirebase "${PROJECT_ID}"
  fi
  local app_id
  app_id="$(firebase apps:list WEB --project "${PROJECT_ID}" --json 2>/dev/null | python3 -c "import json,sys;r=json.load(sys.stdin).get('result',[]);print(next((a['appId'] for a in r if a.get('displayName')=='aijalon-web'),''))")"
  if [[ -z "${app_id}" ]]; then
    firebase apps:create WEB aijalon-web --project "${PROJECT_ID}" >/dev/null
    app_id="$(firebase apps:list WEB --project "${PROJECT_ID}" --json | python3 -c "import json,sys;r=json.load(sys.stdin).get('result',[]);print(next((a['appId'] for a in r if a.get('displayName')=='aijalon-web'),''))")"
  fi
  firebase apps:sdkconfig WEB "${app_id}" --project "${PROJECT_ID}" --json \
    | python3 -c "
import json,re,sys
r=json.load(sys.stdin)['result']
c=r.get('sdkConfig') or json.loads(re.search(r'\{.*\}', r.get('fileContents',''), re.S).group(0))
print('FIREBASE_WEB_API_KEY='+c['apiKey']); print('FIREBASE_APP_ID='+c['appId'])" > "${OUT_DIR}/firebase.env" \
    || warn "could not read the web app config; copy apiKey/appId from Firebase console > Project settings"
  log "Firebase web config -> ${OUT_DIR}/firebase.env (public values; they go to GitHub variables)"
}

step_audit() {
  log "Data Access audit logs for KMS, Secret Manager, Cloud SQL admin, IAM"
  local tmp; tmp="$(mktemp)"
  gcloud projects get-iam-policy "${PROJECT_ID}" --format=json > "${tmp}"
  python3 - "${tmp}" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
want = ["cloudkms.googleapis.com", "secretmanager.googleapis.com", "sqladmin.googleapis.com", "iam.googleapis.com"]
cfgs = {c["service"]: c for c in p.get("auditConfigs", [])}
for s in want:
    c = cfgs.setdefault(s, {"service": s, "auditLogConfigs": []})
    have = {a["logType"] for a in c["auditLogConfigs"]}
    for t in ("ADMIN_READ", "DATA_READ", "DATA_WRITE"):
        if t not in have:
            c["auditLogConfigs"].append({"logType": t})
p["auditConfigs"] = list(cfgs.values())
p["version"] = 3  # keep conditional bindings intact
json.dump(p, open(sys.argv[1], "w"))
PY
  gcloud projects set-iam-policy "${PROJECT_ID}" "${tmp}" --quiet >/dev/null
  rm -f "${tmp}"
}

step_network() {
  log "VPC ${VPC} (+ Private Service Access, Cloud NAT) and isolated sandbox VPC ${SANDBOX_VPC}"
  exists gcloud compute networks describe "${VPC}" || \
    gcloud compute networks create "${VPC}" --subnet-mode=custom --bgp-routing-mode=regional
  exists gcloud compute networks subnets describe "${RUN_SUBNET}" --region="${REGION}" || \
    gcloud compute networks subnets create "${RUN_SUBNET}" --network="${VPC}" --region="${REGION}" \
      --range="${RUN_SUBNET_RANGE}" --enable-private-ip-google-access \
      --enable-flow-logs --logging-aggregation-interval=interval-5-sec --logging-flow-sampling=0.5
  # executor-only subnet (own NAT + static IP below; REVIEW_AUTH_API F1)
  exists gcloud compute networks subnets describe "${EXEC_SUBNET}" --region="${REGION}" || \
    gcloud compute networks subnets create "${EXEC_SUBNET}" --network="${VPC}" --region="${REGION}" \
      --range="${EXEC_SUBNET_RANGE}" --enable-private-ip-google-access \
      --enable-flow-logs --logging-aggregation-interval=interval-5-sec --logging-flow-sampling=0.5
  # Private Service Access for Cloud SQL private IP
  exists gcloud compute addresses describe "${PSA_RANGE_NAME}" --global || \
    gcloud compute addresses create "${PSA_RANGE_NAME}" --global --purpose=VPC_PEERING \
      --addresses="${PSA_RANGE_ADDR}" --prefix-length="${PSA_RANGE_PREFIX}" --network="${VPC}"
  if ! gcloud services vpc-peerings list --network="${VPC}" --format='value(peering)' 2>/dev/null | grep -q servicenetworking; then
    gcloud services vpc-peerings connect --service=servicenetworking.googleapis.com \
      --ranges="${PSA_RANGE_NAME}" --network="${VPC}"
  fi
  # Cloud NAT with reserved static IPs: api/executor route ALL egress through the VPC (needed so that calls
  # to the internal-ingress sandbox count as internal) and reach Hyperliquid/Stripe/Telegram via NAT.
  # TWO NATs on one router, one per subnet: api + migrate leave through ${NAT_IP_NAME}, the executor through its
  # own ${EXEC_NAT_IP_NAME}. Hyperliquid meters weight per IP, so a user flooding API routes cannot 429 the executor's
  # orders / exits (REVIEW_AUTH_API F1). Each service charges its own HL_EGRESS_KEY in hl_rate_budget.
  exists gcloud compute routers describe "${ROUTER}" --region="${REGION}" || \
    gcloud compute routers create "${ROUTER}" --network="${VPC}" --region="${REGION}"
  exists gcloud compute addresses describe "${NAT_IP_NAME}" --region="${REGION}" || \
    gcloud compute addresses create "${NAT_IP_NAME}" --region="${REGION}"
  exists gcloud compute routers nats describe "${NAT}" --router="${ROUTER}" --region="${REGION}" || \
    gcloud compute routers nats create "${NAT}" --router="${ROUTER}" --region="${REGION}" \
      --nat-custom-subnet-ip-ranges="${RUN_SUBNET}" --nat-external-ip-pool="${NAT_IP_NAME}" \
      --enable-logging --log-filter=ERRORS_ONLY
  exists gcloud compute addresses describe "${EXEC_NAT_IP_NAME}" --region="${REGION}" || \
    gcloud compute addresses create "${EXEC_NAT_IP_NAME}" --region="${REGION}"
  exists gcloud compute routers nats describe "${EXEC_NAT}" --router="${ROUTER}" --region="${REGION}" || \
    gcloud compute routers nats create "${EXEC_NAT}" --router="${ROUTER}" --region="${REGION}" \
      --nat-custom-subnet-ip-ranges="${EXEC_SUBNET}" --nat-external-ip-pool="${EXEC_NAT_IP_NAME}" \
      --enable-logging --log-filter=ERRORS_ONLY
  # Egress firewall for workloads tagged ${RUN_NET_TAG}: HTTPS anywhere, Postgres only to the PSA range.
  local psa="${PSA_RANGE_ADDR}/${PSA_RANGE_PREFIX}"
  exists gcloud compute firewall-rules describe "${VPC}-allow-egress-https" || \
    gcloud compute firewall-rules create "${VPC}-allow-egress-https" --network="${VPC}" --direction=EGRESS \
      --action=ALLOW --rules=tcp:443 --destination-ranges=0.0.0.0/0 --target-tags="${RUN_NET_TAG}" --priority=1000
  exists gcloud compute firewall-rules describe "${VPC}-allow-egress-sql" || \
    gcloud compute firewall-rules create "${VPC}-allow-egress-sql" --network="${VPC}" --direction=EGRESS \
      --action=ALLOW --rules=tcp:5432,tcp:3307 --destination-ranges="${psa}" --target-tags="${RUN_NET_TAG}" --priority=1000
  exists gcloud compute firewall-rules describe "${VPC}-deny-egress-other" || \
    gcloud compute firewall-rules create "${VPC}-deny-egress-other" --network="${VPC}" --direction=EGRESS \
      --action=DENY --rules=all --destination-ranges=0.0.0.0/0 --target-tags="${RUN_NET_TAG}" --priority=65000

  # ---- sandbox: a VPC with NO NAT, NO Private Google Access, NO internet route, deny-all firewall --------
  exists gcloud compute networks describe "${SANDBOX_VPC}" || \
    gcloud compute networks create "${SANDBOX_VPC}" --subnet-mode=custom --bgp-routing-mode=regional
  exists gcloud compute networks subnets describe "${SANDBOX_SUBNET}" --region="${REGION}" || \
    gcloud compute networks subnets create "${SANDBOX_SUBNET}" --network="${SANDBOX_VPC}" --region="${REGION}" \
      --range="${SANDBOX_SUBNET_RANGE}" --no-enable-private-ip-google-access \
      --enable-flow-logs --logging-aggregation-interval=interval-5-sec --logging-flow-sampling=1.0
  exists gcloud compute firewall-rules describe "${SANDBOX_VPC}-deny-all-egress" || \
    gcloud compute firewall-rules create "${SANDBOX_VPC}-deny-all-egress" --network="${SANDBOX_VPC}" \
      --direction=EGRESS --action=DENY --rules=all --destination-ranges=0.0.0.0/0 --priority=100 --enable-logging
  exists gcloud compute firewall-rules describe "${SANDBOX_VPC}-deny-all-ingress" || \
    gcloud compute firewall-rules create "${SANDBOX_VPC}-deny-all-ingress" --network="${SANDBOX_VPC}" \
      --direction=INGRESS --action=DENY --rules=all --source-ranges=0.0.0.0/0 --priority=100
  local r
  for r in $(gcloud compute routes list --filter="network~/${SANDBOX_VPC}\$ AND destRange=0.0.0.0/0" --format='value(name)'); do
    log "deleting internet route ${r} from ${SANDBOX_VPC}"; gcloud compute routes delete "${r}" --quiet
  done
  # DNS (REVIEW_WEB_INFRA M5a): Cloud Run resolves through the metadata server → the VPC's Cloud DNS, which would
  # recurse to public authoritative servers, so `<data>.attacker.tld` lookups could exfiltrate data although no
  # packet leaves the VPC. A response policy on the sandbox VPC answers EVERY name locally (wildcard `*.`, local data
  # 0.0.0.0 — a sinkhole; queries never recurse) and DNS query logging records every attempt (the sandbox makes no
  # legitimate lookups: alert on any log entry). [VERIFY at go-live: from a sandbox probe, `getaddrinfo` of a random
  # name under a domain we control returns no public answer AND our authoritative server sees no query.]
  exists gcloud dns response-policies describe "${SANDBOX_DNS_POLICY}" || \
    gcloud dns response-policies create "${SANDBOX_DNS_POLICY}" --networks="${SANDBOX_VPC}" \
      --description="sandbox: answer every name locally (no recursion, no DNS exfiltration)"
  exists gcloud dns response-policies rules describe deny-all --response-policy="${SANDBOX_DNS_POLICY}" || \
    gcloud dns response-policies rules create deny-all --response-policy="${SANDBOX_DNS_POLICY}" --dns-name="*." \
      --local-data="name=*.,type=A,ttl=300,rrdatas=0.0.0.0"
  exists gcloud dns policies describe "${SANDBOX_DNS_LOG_POLICY}" || \
    gcloud dns policies create "${SANDBOX_DNS_LOG_POLICY}" --networks="${SANDBOX_VPC}" --enable-logging \
      --description="sandbox: log every DNS query (there should be none)"
  if [[ "${SANDBOX_EGRESS_MODE}" == "connector" ]]; then
    exists gcloud compute networks vpc-access connectors describe "${SANDBOX_CONNECTOR}" --region="${REGION}" || \
      gcloud compute networks vpc-access connectors create "${SANDBOX_CONNECTOR}" --region="${REGION}" \
        --network="${SANDBOX_VPC}" --range="${SANDBOX_CONNECTOR_RANGE}" --min-instances=2 --max-instances=3 \
        --machine-type=e2-micro
  fi
}

step_kms() {
  log "KMS keyring ${KMS_KEYRING} / key ${KMS_KEY} (HSM, ENCRYPT_DECRYPT, 90-day rotation)"
  exists gcloud kms keyrings describe "${KMS_KEYRING}" --location="${REGION}" || \
    gcloud kms keyrings create "${KMS_KEYRING}" --location="${REGION}"
  exists gcloud kms keys describe "${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" || \
    gcloud kms keys create "${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" \
      --purpose=encryption --protection-level=hsm --rotation-period=90d \
      --next-rotation-time="$(utc_in_days 90)" --destroy-scheduled-duration=90d --labels=app=aijalon,data=agent-keys
  # Dedicated creator-code key (REVIEW_TRADING_KEYS F2 / SECURITY §3.4): creator strategy code never shares a KEK,
  # IAM grant or rotation with agent private keys.
  exists gcloud kms keys describe "${KMS_CODE_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" || \
    gcloud kms keys create "${KMS_CODE_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" \
      --purpose=encryption --protection-level=hsm --rotation-period=90d \
      --next-rotation-time="$(utc_in_days 90)" --destroy-scheduled-duration=90d --labels=app=aijalon,data=creator-code
  # Agent attestation key (REVIEW_WEB_INFRA H1): the executor signs "aijalon-agent-v1|user|agent" after opening the
  # sealed key; browsers verify with the public key pinned in web/public/app-config.json. P-256 because WebCrypto
  # verifies it natively. IAM (step `sa`): executor = signer, nobody else.
  exists gcloud kms keys describe "${KMS_ATTEST_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" || \
    gcloud kms keys create "${KMS_ATTEST_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" \
      --purpose=asymmetric-signing --default-algorithm=ec-sign-p256-sha256 --protection-level=hsm \
      --destroy-scheduled-duration=90d --labels=app=aijalon,data=agent-attest
  # Binary Authorization attestor key (REVIEW_WEB_INFRA H2): only the image builder SA signs with it (step `binauthz`).
  exists gcloud kms keys describe "${KMS_BINAUTHZ_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" || \
    gcloud kms keys create "${KMS_BINAUTHZ_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" \
      --purpose=asymmetric-signing --default-algorithm=ec-sign-p256-sha256 --protection-level=hsm \
      --destroy-scheduled-duration=90d --labels=app=aijalon,data=binauthz
  if [[ "${SQL_ENABLE_CMEK}" == "1" ]]; then
    exists gcloud kms keys describe "${KMS_SQL_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" || \
      gcloud kms keys create "${KMS_SQL_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" \
        --purpose=encryption --protection-level=hsm --rotation-period=365d \
        --next-rotation-time="$(utc_in_days 365)" --destroy-scheduled-duration=90d --labels=app=aijalon,data=cloudsql
    local sql_agent
    sql_agent="$(gcloud beta services identity create --service=sqladmin.googleapis.com --format='value(email)' 2>/dev/null || true)"
    [[ -n "${sql_agent}" ]] || sql_agent="service-$(project_number)@gcp-sa-cloud-sql.iam.gserviceaccount.com"
    gcloud kms keys add-iam-policy-binding "${KMS_SQL_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" \
      --member="serviceAccount:${sql_agent}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter >/dev/null
  fi
}

step_registry() {
  log "Artifact Registry ${AR_REPO} (immutable tags, vulnerability scanning on push)"
  exists gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" || \
    gcloud artifacts repositories create "${AR_REPO}" --repository-format=docker --location="${REGION}" \
      --immutable-tags --description="aijalon.trade images (deployed by digest)" --labels=app=aijalon
}

step_sa() {
  log "service accounts (SPEC §2.1 least privilege)"
  local id
  for id in "${SA_API_ID}" "${SA_EXECUTOR_ID}" "${SA_SANDBOX_ID}" "${SA_SCHEDULER_ID}" "${SA_DEPLOYER_ID}" "${SA_MIGRATOR_ID}"; do
    exists gcloud iam service-accounts describe "$(sa_email "${id}")" || \
      gcloud iam service-accounts create "${id}" --display-name="aijalon ${id#aijalon-}"
  done
  local kf=(--keyring="${KMS_KEYRING}" --location="${REGION}")
  # api: KMS ENCRYPT only, executor: KMS DECRYPT only — on agent-keys and, separately, on creator-code. No other
  # principal gets either role; no SA gets encrypt AND decrypt on the same key.
  local key
  for key in "${KMS_KEY}" "${KMS_CODE_KEY}"; do
    gcloud kms keys add-iam-policy-binding "${key}" "${kf[@]}" \
      --member="serviceAccount:${SA_API}" --role=roles/cloudkms.cryptoKeyEncrypter >/dev/null
    gcloud kms keys add-iam-policy-binding "${key}" "${kf[@]}" \
      --member="serviceAccount:${SA_EXECUTOR}" --role=roles/cloudkms.cryptoKeyDecrypter >/dev/null
  done
  # api + executor: Cloud SQL client + IAM database login, scoped to our instance
  local cond="resource.name == 'projects/${PROJECT_ID}/instances/${SQL_INSTANCE}' && resource.type == 'sqladmin.googleapis.com/Instance'"
  local sa
  for sa in "${SA_API}" "${SA_EXECUTOR}"; do
    if [[ "${SQL_IAM_CONDITIONS}" == "1" ]]; then
      bind_project "serviceAccount:${sa}" roles/cloudsql.client "${cond}" "only-${SQL_INSTANCE}"
      bind_project "serviceAccount:${sa}" roles/cloudsql.instanceUser "${cond}" "only-${SQL_INSTANCE}"
    else
      bind_project "serviceAccount:${sa}" roles/cloudsql.client
      bind_project "serviceAccount:${sa}" roles/cloudsql.instanceUser
    fi
  done
  if [[ "${API_FIREBASE_AUTH_ADMIN}" == "1" ]]; then
    bind_project "serviceAccount:${SA_API}" roles/firebaseauth.admin
  fi
  # sandbox: NO project/KMS/SQL roles (only secretAccessor on SANDBOX_SHARED_SECRET, step_secrets). scheduler: run.invoker on executor only (step_run). migrator: one secret (step_secrets).
  # deployer (GitHub Actions via WIF): deploy Run + Hosting, push images, run the migrate job. It gets NO
  # secret accessor and NO KMS role. It can "act as" only the runtime SAs it deploys.
  bind_project "serviceAccount:${SA_DEPLOYER}" roles/run.developer
  bind_project "serviceAccount:${SA_DEPLOYER}" roles/firebasehosting.admin
  bind_project "serviceAccount:${SA_DEPLOYER}" roles/firebase.viewer
  bind_project "serviceAccount:${SA_DEPLOYER}" roles/serviceusage.serviceUsageConsumer
  for sa in "${SA_API}" "${SA_EXECUTOR}" "${SA_SANDBOX}" "${SA_MIGRATOR}"; do
    gcloud iam service-accounts add-iam-policy-binding "${sa}" \
      --member="serviceAccount:${SA_DEPLOYER}" --role=roles/iam.serviceAccountUser >/dev/null
  done
  gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" --location="${REGION}" \
    --member="serviceAccount:${SA_DEPLOYER}" --role=roles/artifactregistry.writer >/dev/null
}

gen_secret_value() { # name -> random value on stdout (never echoed to the terminal)
  case "$1" in
    AUDIT_PEPPER) openssl rand -base64 32 | tr -d '\n' ;;       # env AUDIT_PEPPER_B64 (>= 32 bytes)
    *)            openssl rand -hex 32 | tr -d '\n' ;;          # hex: safe in URLs, headers, CEL strings
  esac
}

step_secrets() {
  log "Secret Manager: create secrets (names only), per-secret accessor bindings"
  local spec name readers gen reader member
  for spec in "${SECRETS_SPEC[@]}"; do
    IFS='|' read -r name readers gen <<<"${spec}"
    exists gcloud secrets describe "${name}" || \
      gcloud secrets create "${name}" --replication-policy=user-managed --locations="${REGION}" --labels=app=aijalon
    if [[ "${gen}" == "1" || "${gen}" == "owner" ]] && \
       [[ -z "$(gcloud secrets versions list "${name}" --filter='state=ENABLED' --format='value(name)' --limit=1)" ]]; then
      if [[ "${gen}" == "1" ]]; then
        log "  generating a random value for ${name}"
        gen_secret_value "${name}" | gcloud secrets versions add "${name}" --data-file=- >/dev/null
      else
        log "  seeding ${name} with OWNER_EMAIL (replace it with the real list: DEPLOY.md §5)"
        printf '%s' "${OWNER_EMAIL}" | gcloud secrets versions add "${name}" --data-file=- >/dev/null
      fi
    fi
    for reader in ${readers}; do
      case "${reader}" in
        api) member="serviceAccount:${SA_API}" ;;
        executor) member="serviceAccount:${SA_EXECUTOR}" ;;
        migrator) member="serviceAccount:${SA_MIGRATOR}" ;;
        sandbox) member="serviceAccount:${SA_SANDBOX}" ;;
        *) die "unknown reader ${reader}" ;;
      esac
      gcloud secrets add-iam-policy-binding "${name}" --member="${member}" \
        --role=roles/secretmanager.secretAccessor >/dev/null
    done
  done
  local missing=()
  for spec in "${SECRETS_SPEC[@]}"; do
    IFS='|' read -r name _ gen <<<"${spec}"
    [[ "${gen}" == "opt" ]] && continue      # optional (KYC_* until KYC_PROVIDER=sumsub)
    [[ -n "$(gcloud secrets versions list "${name}" --filter='state=ENABLED' --format='value(name)' --limit=1)" ]] || missing+=("${name}")
  done
  ((${#missing[@]} == 0)) || warn "secrets still without a value (add them — DEPLOY.md §5): ${missing[*]}"
}

step_sql() {
  log "Cloud SQL ${SQL_INSTANCE} (Postgres 16, private IP, ${SQL_AVAILABILITY}, PITR, 30 backups, IAM auth, CMEK=${SQL_ENABLE_CMEK})"
  # ^:^ switches gcloud's list delimiter to ':' so pgaudit.log can contain a comma
  local flags="^:^cloudsql.iam_authentication=on:max_connections=${SQL_MAX_CONNECTIONS}:cloudsql.enable_pgaudit=on:pgaudit.log=ddl,role:log_connections=on:log_disconnections=on:log_lock_waits=on:log_min_duration_statement=1000:log_temp_files=0:log_checkpoints=on"
  if exists gcloud sql instances describe "${SQL_INSTANCE}"; then
    log "  instance exists — verifying critical settings (no patch: patching flags restarts the instance)"
    gcloud sql instances describe "${SQL_INSTANCE}" --format=json | python3 - "${SQL_AVAILABILITY}" <<'PY' \
      || warn "fix the mismatches above (gcloud sql instances patch ...) during a maintenance window"
import json,sys
d=json.load(sys.stdin); s=d["settings"]; bad=[]
if not s.get("deletionProtectionEnabled"): bad.append("deletion protection OFF")
if s.get("availabilityType")!=sys.argv[1]: bad.append("availability %s != SQL_AVAILABILITY %s" % (s.get("availabilityType"), sys.argv[1]))
if s.get("availabilityType")!="REGIONAL": print("  NOTE: not HA (ZONAL) — switch to REGIONAL before Gate C (public)")
b=s.get("backupConfiguration",{})
if not b.get("pointInTimeRecoveryEnabled"): bad.append("PITR OFF")
if int(b.get("backupRetentionSettings",{}).get("retainedBackups",0))<30: bad.append("<30 retained backups")
if s.get("ipConfiguration",{}).get("ipv4Enabled"): bad.append("PUBLIC IP ENABLED (run db_bootstrap cleanup)")
fl={f["name"]:f["value"] for f in s.get("databaseFlags",[])}
if fl.get("cloudsql.iam_authentication")!="on": bad.append("cloudsql.iam_authentication not on")
print("  OK" if not bad else "  MISMATCH: "+"; ".join(bad))
sys.exit(1 if bad else 0)
PY
    return 0
  fi
  local cmek=()
  [[ "${SQL_ENABLE_CMEK}" == "1" ]] && cmek=(--disk-encryption-key="${KMS_SQL_KEY_NAME}")
  gcloud sql instances create "${SQL_INSTANCE}" \
    --database-version=POSTGRES_16 --edition="${SQL_EDITION}" --tier="${SQL_TIER}" --region="${REGION}" \
    --availability-type="${SQL_AVAILABILITY}" \
    --storage-type=SSD --storage-size="${SQL_STORAGE_GB}GB" --storage-auto-increase \
    --network="projects/${PROJECT_ID}/global/networks/${VPC}" --no-assign-ip --ssl-mode=ENCRYPTED_ONLY \
    --backup-start-time=18:00 --retained-backups-count=30 \
    --enable-point-in-time-recovery --retained-transaction-log-days=7 \
    --maintenance-window-day=SUN --maintenance-window-hour=19 --maintenance-release-channel=production \
    --deletion-protection --insights-config-query-insights-enabled \
    --database-flags="${flags}" --labels=app=aijalon,env=prod "${cmek[@]}"
  # Nobody keeps the built-in superuser password: set a random one and discard it.
  openssl rand -hex 32 | tr -d '\n' | sql_user_password "${SQL_INSTANCE}" postgres
}

deploy_placeholder() { # service sa ingress [extra gcloud flags...]
  local svc="$1" sa="$2" ingress="$3"; shift 3
  if ! exists gcloud run services describe "${svc}" --region="${REGION}"; then
    log "  placeholder revision for ${svc} (real image comes from the deploy workflow)"
    gcloud run deploy "${svc}" --region="${REGION}" --image=us-docker.pkg.dev/cloudrun/container/hello \
      --service-account="${sa}" --ingress="${ingress}" --no-allow-unauthenticated \
      --min-instances=0 --max-instances=1 --labels=app=aijalon "$@" --quiet >/dev/null
  fi
}

step_run() {
  log "Cloud Run services (placeholders) + invoker IAM"
  deploy_placeholder "${API_SERVICE}" "${SA_API}" internal-and-cloud-load-balancing
  deploy_placeholder "${EXECUTOR_SERVICE}" "${SA_EXECUTOR}" internal
  deploy_placeholder "${SANDBOX_SERVICE}" "${SA_SANDBOX}" internal
  # api: public users authenticate in-app (Firebase ID token + MFA); network path is forced through the LB
  # by ingress=internal-and-cloud-load-balancing, and the LB only admits Cloudflare (Cloud Armor).
  gcloud run services add-iam-policy-binding "${API_SERVICE}" --region="${REGION}" \
    --member=allUsers --role=roles/run.invoker >/dev/null
  gcloud run services add-iam-policy-binding "${EXECUTOR_SERVICE}" --region="${REGION}" \
    --member="serviceAccount:${SA_SCHEDULER}" --role=roles/run.invoker >/dev/null
  local sa
  for sa in "${SA_API}" "${SA_EXECUTOR}"; do
    gcloud run services add-iam-policy-binding "${SANDBOX_SERVICE}" --region="${REGION}" \
      --member="serviceAccount:${sa}" --role=roles/run.invoker >/dev/null
  done
}

armor_rule() { # priority action [--src-ip-ranges=..|--expression=..]
  local prio="$1"; shift
  if exists gcloud compute security-policies rules describe "${prio}" --security-policy="${LB_ARMOR_POLICY}"; then
    gcloud compute security-policies rules update "${prio}" --security-policy="${LB_ARMOR_POLICY}" "$@" >/dev/null
  else
    gcloud compute security-policies rules create "${prio}" --security-policy="${LB_ARMOR_POLICY}" "$@" >/dev/null
  fi
}

cloudflare_ipv4() {
  # Live list from Cloudflare; falls back to the published list (stable since 2021) if unreachable.
  curl -fsS --max-time 15 https://api.cloudflare.com/client/v4/ips 2>/dev/null \
    | python3 -c "import json,sys;print('\n'.join(json.load(sys.stdin)['result']['ipv4_cidrs']))" 2>/dev/null \
  || printf '%s\n' 173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 \
       108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 \
       104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22
}

step_lb() {
  log "Global external HTTPS LB -> serverless NEG(api), Cloud Armor (Cloudflare only), Certificate Manager"
  exists gcloud compute addresses describe "${LB_IP_NAME}" --global || \
    gcloud compute addresses create "${LB_IP_NAME}" --global --ip-version=IPV4 --network-tier=PREMIUM
  exists gcloud compute network-endpoint-groups describe "${LB_NEG}" --region="${REGION}" || \
    gcloud compute network-endpoint-groups create "${LB_NEG}" --region="${REGION}" \
      --network-endpoint-type=serverless --cloud-run-service="${API_SERVICE}"

  # Cloud Armor: default deny; allow only Cloudflare edge IPs; (optionally) require X-Edge-Auth.
  exists gcloud compute security-policies describe "${LB_ARMOR_POLICY}" || \
    gcloud compute security-policies create "${LB_ARMOR_POLICY}" \
      --description="api: only Cloudflare edge with X-Edge-Auth may reach the origin"
  gcloud compute security-policies rules update 2147483647 --security-policy="${LB_ARMOR_POLICY}" --action=deny-403 >/dev/null
  if [[ "${ARMOR_EDGE_HEADER_CHECK}" == "1" ]]; then
    local edge; edge="$(gcloud secrets versions access latest --secret=EDGE_AUTH_SECRET)"
    [[ "${edge}" =~ ^[0-9a-f]{64}$ ]] || die "EDGE_AUTH_SECRET must be 64 hex chars (bootstrap generates it)"
    armor_rule 900 --action=deny-403 --expression="request.headers['x-edge-auth'] != '${edge}'" \
      --description="deny requests without the Cloudflare-injected edge secret"
  fi
  local ranges=() chunk=() i prio=1000
  mapfile -t ranges < <(cloudflare_ipv4)
  for ((i = 0; i < ${#ranges[@]}; i += 10)); do   # max 10 ranges per rule
    chunk=("${ranges[@]:i:10}")
    armor_rule "${prio}" --action=allow --src-ip-ranges="$(IFS=,; echo "${chunk[*]}")" --description="Cloudflare IPv4 ${prio}"
    prio=$((prio + 1))
  done

  exists gcloud compute backend-services describe "${LB_BACKEND}" --global || \
    gcloud compute backend-services create "${LB_BACKEND}" --global --load-balancing-scheme=EXTERNAL_MANAGED \
      --enable-logging --logging-sample-rate=1.0
  gcloud compute backend-services update "${LB_BACKEND}" --global --security-policy="${LB_ARMOR_POLICY}" >/dev/null
  if ! gcloud compute backend-services describe "${LB_BACKEND}" --global --format='value(backends[].group)' | grep -q "${LB_NEG}"; then
    gcloud compute backend-services add-backend "${LB_BACKEND}" --global \
      --network-endpoint-group="${LB_NEG}" --network-endpoint-group-region="${REGION}"
  fi
  exists gcloud compute url-maps describe "${LB_URLMAP}" --global || \
    gcloud compute url-maps create "${LB_URLMAP}" --global --default-service="${LB_BACKEND}"

  # Certificate via DNS authorization: works while Cloudflare proxies the hostname (HTTP-01 would not).
  exists gcloud certificate-manager dns-authorizations describe "${CERT_DNS_AUTH}" || \
    gcloud certificate-manager dns-authorizations create "${CERT_DNS_AUTH}" --domain="${API_DOMAIN}"
  exists gcloud certificate-manager certificates describe "${CERT_NAME}" || \
    gcloud certificate-manager certificates create "${CERT_NAME}" --domains="${API_DOMAIN}" \
      --dns-authorizations="${CERT_DNS_AUTH}"
  exists gcloud certificate-manager maps describe "${CERT_MAP}" || \
    gcloud certificate-manager maps create "${CERT_MAP}"
  exists gcloud certificate-manager maps entries describe "${CERT_MAP_ENTRY}" --map="${CERT_MAP}" || \
    gcloud certificate-manager maps entries create "${CERT_MAP_ENTRY}" --map="${CERT_MAP}" \
      --certificates="${CERT_NAME}" --hostname="${API_DOMAIN}"
  exists gcloud compute ssl-policies describe "${LB_SSL_POLICY}" --global || \
    gcloud compute ssl-policies create "${LB_SSL_POLICY}" --global --profile=MODERN --min-tls-version=1.2
  exists gcloud compute target-https-proxies describe "${LB_PROXY}" --global || \
    gcloud compute target-https-proxies create "${LB_PROXY}" --global --url-map="${LB_URLMAP}" \
      --certificate-map="${CERT_MAP}" --ssl-policy="${LB_SSL_POLICY}"
  exists gcloud compute forwarding-rules describe "${LB_FWD_RULE}" --global || \
    gcloud compute forwarding-rules create "${LB_FWD_RULE}" --global --load-balancing-scheme=EXTERNAL_MANAGED \
      --network-tier=PREMIUM --address="${LB_IP_NAME}" --target-https-proxy="${LB_PROXY}" --ports=443
}

step_scheduler() {
  log "Cloud Scheduler jobs -> executor (OIDC as ${SA_SCHEDULER}); new jobs start PAUSED=${SCHEDULER_START_PAUSED}"
  local url; url="$(gcloud run services describe "${EXECUTOR_SERVICE}" --region="${REGION}" --format='value(status.url)')"
  [[ -n "${url}" ]] || die "executor service not found — run the 'run' step first"
  local spec name cron path deadline retries common
  for spec in "${SCHEDULER_SPEC[@]}"; do
    IFS='|' read -r name cron path deadline retries <<<"${spec}"
    common=(--location="${REGION}" --schedule="${cron}" --time-zone=Etc/UTC
      --uri="${url}${INTERNAL_PREFIX}/${path}" --http-method=POST --message-body='{}'
      --oidc-service-account-email="${SA_SCHEDULER}" --oidc-token-audience="${url}"
      --attempt-deadline="${deadline}" --max-retry-attempts="${retries}" --min-backoff=30s --max-backoff=300s)
    if exists gcloud scheduler jobs describe "${name}" --location="${REGION}"; then
      gcloud scheduler jobs update http "${name}" "${common[@]}" --update-headers=Content-Type=application/json >/dev/null
    else
      gcloud scheduler jobs create http "${name}" "${common[@]}" --headers=Content-Type=application/json >/dev/null
      [[ "${SCHEDULER_START_PAUSED}" == "1" ]] && gcloud scheduler jobs pause "${name}" --location="${REGION}" >/dev/null
    fi
  done
}

step_wif() {
  log "Workload Identity Federation: ${GITHUB_REPO} @ main, environment 'production', deploy.yml only"
  exists gcloud iam workload-identity-pools describe "${WIF_POOL}" --location=global || \
    gcloud iam workload-identity-pools create "${WIF_POOL}" --location=global --display-name="GitHub Actions"
  local cond="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main' && assertion.environment == 'production' && assertion.workflow_ref == '${GITHUB_REPO}/.github/workflows/deploy.yml@refs/heads/main'"
  [[ -n "${GITHUB_REPO_ID}" ]] && cond+=" && assertion.repository_id == '${GITHUB_REPO_ID}'"
  local mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref,attribute.environment=assertion.environment,attribute.workflow_ref=assertion.workflow_ref,attribute.actor=assertion.actor"
  if exists gcloud iam workload-identity-pools providers describe "${WIF_PROVIDER}" --workload-identity-pool="${WIF_POOL}" --location=global; then
    gcloud iam workload-identity-pools providers update-oidc "${WIF_PROVIDER}" --workload-identity-pool="${WIF_POOL}" \
      --location=global --attribute-mapping="${mapping}" --attribute-condition="${cond}" >/dev/null
  else
    gcloud iam workload-identity-pools providers create-oidc "${WIF_PROVIDER}" --workload-identity-pool="${WIF_POOL}" \
      --location=global --issuer-uri=https://token.actions.githubusercontent.com \
      --attribute-mapping="${mapping}" --attribute-condition="${cond}"
  fi
  gcloud iam service-accounts add-iam-policy-binding "${SA_DEPLOYER}" --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/projects/$(project_number)/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_REPO}" >/dev/null
}

step_monitoring() {
  log "alerts (log-match + metric), uptime checks, e-mail channel ${ALERT_EMAIL}"
  PROJECT_ID="${PROJECT_ID}" ALERT_EMAIL="${ALERT_EMAIL}" SA_API="${SA_API}" SA_EXECUTOR="${SA_EXECUTOR}" \
  SA_MIGRATOR="${SA_MIGRATOR}" SA_DEPLOYER="${SA_DEPLOYER}" KMS_KEY="${KMS_KEY}" SQL_INSTANCE="${SQL_INSTANCE}" \
  API_DOMAIN="${API_DOMAIN}" WEB_DOMAIN="${WEB_DOMAIN}" EXECUTOR_SERVICE="${EXECUTOR_SERVICE}" \
  API_SERVICE="${API_SERVICE}" SANDBOX_SERVICE="${SANDBOX_SERVICE}" \
    python3 "${HERE}/monitoring.py"
}

step_budget() {
  log "budget alert ${BUDGET_USD} USD/month"
  [[ -n "${BILLING_ACCOUNT}" ]] || { warn "BILLING_ACCOUNT empty — skipping budget"; return 0; }
  if ! gcloud billing budgets list --billing-account="${BILLING_ACCOUNT}" --format='value(displayName)' 2>/dev/null | grep -qx "aijalon-${PROJECT_ID}"; then
    gcloud billing budgets create --billing-account="${BILLING_ACCOUNT}" --display-name="aijalon-${PROJECT_ID}" \
      --budget-amount="${BUDGET_USD}USD" --filter-projects="projects/${PROJECT_ID}" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
      --threshold-rule=percent=1.2,basis=forecasted-spend
  fi
}

step_harden() {
  log "hardening: strip Editor from the default compute SA; essential contacts"
  local dsa; dsa="$(project_number)-compute@developer.gserviceaccount.com"
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${dsa}" --role=roles/editor \
    --condition=None --quiet >/dev/null 2>&1 || true
  if ! gcloud essential-contacts list --format='value(email)' 2>/dev/null | grep -qx "${ALERT_EMAIL}"; then
    gcloud essential-contacts create --email="${ALERT_EMAIL}" --notification-categories=ALL >/dev/null \
      || warn "could not add essential contact ${ALERT_EMAIL} (add it in IAM > Essential contacts)"
  fi
}

step_outputs() {
  log "outputs"
  local num lb_ip dns_name dns_data exec_url sbx_url db_ip nat_ip exec_nat_ip
  num="$(project_number)"
  lb_ip="$(gcloud compute addresses describe "${LB_IP_NAME}" --global --format='value(address)' 2>/dev/null || true)"
  dns_name="$(gcloud certificate-manager dns-authorizations describe "${CERT_DNS_AUTH}" --format='value(dnsResourceRecord.name)' 2>/dev/null || true)"
  dns_data="$(gcloud certificate-manager dns-authorizations describe "${CERT_DNS_AUTH}" --format='value(dnsResourceRecord.data)' 2>/dev/null || true)"
  exec_url="$(gcloud run services describe "${EXECUTOR_SERVICE}" --region="${REGION}" --format='value(status.url)' 2>/dev/null || true)"
  sbx_url="$(gcloud run services describe "${SANDBOX_SERVICE}" --region="${REGION}" --format='value(status.url)' 2>/dev/null || true)"
  db_ip="$(gcloud sql instances describe "${SQL_INSTANCE}" --format='value(ipAddresses[0].ipAddress)' 2>/dev/null || true)"
  nat_ip="$(gcloud compute addresses describe "${NAT_IP_NAME}" --region="${REGION}" --format='value(address)' 2>/dev/null || true)"
  exec_nat_ip="$(gcloud compute addresses describe "${EXEC_NAT_IP_NAME}" --region="${REGION}" --format='value(address)' 2>/dev/null || true)"
  cat > "${OUT_DIR}/outputs.env" <<EOF
# generated by infra/gcp/bootstrap.sh $(date -u +%FT%TZ) — no secrets in this file
GCP_PROJECT_ID=${PROJECT_ID}
GCP_PROJECT_NUMBER=${num}
GCP_REGION=${REGION}
WIF_PROVIDER=projects/${num}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}
DEPLOYER_SA=${SA_DEPLOYER}
SQL_CONNECTION_NAME=${SQL_CONNECTION_NAME}
DB_PRIVATE_IP=${db_ip}
EXECUTOR_URL=${exec_url}
SANDBOX_URL=${sbx_url}
API_LB_IP=${lb_ip}
API_CERT_DNS_AUTH_NAME=${dns_name}
API_CERT_DNS_AUTH_VALUE=${dns_data}
NAT_EGRESS_IP=${nat_ip}
EXECUTOR_NAT_EGRESS_IP=${exec_nat_ip}
EOF
  cat "${OUT_DIR}/outputs.env"
  echo
  log "GitHub environment variables (run once; repo ${GITHUB_REPO}, environment 'production'):"
  local k v
  while IFS='=' read -r k v; do
    case "${k}" in GCP_PROJECT_ID|GCP_PROJECT_NUMBER|GCP_REGION|WIF_PROVIDER|DEPLOYER_SA|SQL_CONNECTION_NAME|DB_PRIVATE_IP|EXECUTOR_URL|SANDBOX_URL)
      echo "gh variable set ${k} --env production -R ${GITHUB_REPO} --body '${v}'" ;; esac
  done < "${OUT_DIR}/outputs.env"
  if [[ -f "${OUT_DIR}/firebase.env" ]]; then
    while IFS='=' read -r k v; do
      [[ -n "${k}" ]] && echo "gh variable set ${k} --env production -R ${GITHUB_REPO} --body '${v}'"
    done < "${OUT_DIR}/firebase.env"
  fi
  echo
  log "Cloudflare inputs for infra/cloudflare/dns.sh: API_LB_IP, API_CERT_DNS_AUTH_NAME, API_CERT_DNS_AUTH_VALUE"
}

ALL_STEPS=(project apis firebase audit network kms registry sa secrets sql run lb scheduler wif monitoring budget harden outputs)
main() {
  local steps=("$@")
  ((${#steps[@]})) || steps=("${ALL_STEPS[@]}")
  local s
  for s in "${steps[@]}"; do
    declare -F "step_${s}" >/dev/null || die "unknown step '${s}' (valid: ${ALL_STEPS[*]})"
    "step_${s}"
  done
  log "done: ${steps[*]}"
}
main "$@"
