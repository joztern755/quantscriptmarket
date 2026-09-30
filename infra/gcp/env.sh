# shellcheck shell=bash
# Single source of truth for every Google Cloud name used by infra/gcp/*.sh and the deploy workflow.
# Sourced (not executed). Every value can be overridden from the environment, e.g.
#   PROJECT_ID=aijalon-prod-2 BILLING_ACCOUNT=0123AB-... ./infra/gcp/bootstrap.sh
# If you change a default here, also change: .firebaserc (project id) and the GitHub environment variables
# listed in docs/DEPLOY.md §6 (the deploy workflow reads them from GitHub, not from this file).

# ---- owner inputs ---------------------------------------------------------------------------------------
: "${PROJECT_ID:=aijalon-trade-prod}"            # must be globally unique; change if taken (DEPLOY.md §2)
: "${REGION:=asia-southeast1}"                   # Singapore — SPEC §2: everything in one region
: "${BILLING_ACCOUNT:=}"                         # e.g. 0123AB-4567CD-89EF01 (gcloud billing accounts list)
: "${OWNER_EMAIL:=app.aijalon@gmail.com}"        # Google account that owns the project
: "${ALERT_EMAIL:=${OWNER_EMAIL}}"               # Cloud Monitoring e-mail channel
: "${GITHUB_REPO:=joztern755/quantscriptmarket}" # only this repo (branch main, env production) may deploy
: "${GITHUB_REPO_ID:=}"                          # numeric id (gh api repos/$GITHUB_REPO --jq .id); pins WIF to
                                                 # the repo *instance* so a deleted+recreated repo cannot deploy
: "${BUDGET_USD:=400}"                           # monthly budget alert (50/90/100% actual, 120% forecast)

# ---- domains ---------------------------------------------------------------------------------------------
: "${WEB_DOMAIN:=aijalon.trade}"
: "${API_DOMAIN:=api.aijalon.trade}"
WEB_ORIGIN="https://${WEB_DOMAIN}"
API_ORIGIN="https://${API_DOMAIN}"

# ---- artifact registry -----------------------------------------------------------------------------------
: "${AR_REPO:=aijalon}"
AR_HOST="${REGION}-docker.pkg.dev"
BACKEND_IMAGE_REPO="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/backend"
SANDBOX_IMAGE_REPO="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/sandbox"
# Cloud SQL Auth Proxy sidecar (api + executor). `make pin` replaces the digest; deploy refuses PIN_ME.
# pin-image: gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.26.0
: "${CLOUDSQL_PROXY_IMAGE:=gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.26.0@sha256:PIN_ME}"

# ---- network ---------------------------------------------------------------------------------------------
: "${VPC:=aijalon-vpc}"
: "${RUN_SUBNET:=aijalon-run}"                   # Direct VPC egress for api / executor / migrate job
: "${RUN_SUBNET_RANGE:=10.10.0.0/24}"
: "${PSA_RANGE_NAME:=aijalon-psa}"               # Private Service Access range (Cloud SQL private IP)
: "${PSA_RANGE_ADDR:=10.100.0.0}"
: "${PSA_RANGE_PREFIX:=20}"
: "${ROUTER:=aijalon-router}"
: "${NAT:=aijalon-nat}"
: "${NAT_IP_NAME:=aijalon-nat-ip-1}"            # static egress IP (allow-list it at e-mail provider etc.)
: "${RUN_NET_TAG:=run-egress}"                  # firewall tag carried by api/executor/migrate egress
: "${SANDBOX_VPC:=aijalon-sandbox-vpc}"         # isolated: no NAT, no Private Google Access, deny-all egress
: "${SANDBOX_SUBNET:=aijalon-sandbox}"
: "${SANDBOX_SUBNET_RANGE:=10.20.0.0/24}"
: "${SANDBOX_NET_TAG:=sandbox-egress}"

# ---- Cloud SQL -------------------------------------------------------------------------------------------
: "${SQL_INSTANCE:=aijalon-pg}"
: "${SQL_EDITION:=enterprise}"                   # explicit: PG16 otherwise defaults to enterprise-plus
: "${SQL_TIER:=db-custom-2-8192}"                # 2 vCPU / 8 GB; resize later without data loss
: "${SQL_STORAGE_GB:=50}"
: "${SQL_MAX_CONNECTIONS:=200}"                  # api 20 inst x 5 + executor 3 x 5 + migrate + headroom
: "${SQL_ENABLE_CMEK:=1}"                        # SPEC §2 "CMEK": disk encrypted with our HSM key (create-time only)
: "${DB_NAME:=aijalon}"
: "${DB_MIGRATOR_USER:=migrator}"                # built-in (password) user; password only in Secret Manager
SQL_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

# ---- KMS -------------------------------------------------------------------------------------------------
: "${KMS_KEYRING:=aijalon}"
: "${KMS_KEY:=agent-keys}"                       # SPEC §5.3 envelope KEK for agent keys (HSM, 90-day rotation)
: "${KMS_SQL_KEY:=cloudsql}"                     # CMEK for the Cloud SQL disk
KMS_KEY_NAME="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_KEY}"
KMS_SQL_KEY_NAME="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_SQL_KEY}"

# ---- service accounts (ids must be 6-30 chars) ----------------------------------------------------------
SA_API_ID="aijalon-api"
SA_EXECUTOR_ID="aijalon-executor"
SA_SANDBOX_ID="aijalon-sandbox"
SA_SCHEDULER_ID="aijalon-scheduler"
SA_DEPLOYER_ID="aijalon-deployer"
SA_MIGRATOR_ID="aijalon-migrator"
sa_email() { printf '%s@%s.iam.gserviceaccount.com' "$1" "${PROJECT_ID}"; }
SA_API="$(sa_email "${SA_API_ID}")"
SA_EXECUTOR="$(sa_email "${SA_EXECUTOR_ID}")"
SA_SANDBOX="$(sa_email "${SA_SANDBOX_ID}")"
SA_SCHEDULER="$(sa_email "${SA_SCHEDULER_ID}")"
SA_DEPLOYER="$(sa_email "${SA_DEPLOYER_ID}")"
SA_MIGRATOR="$(sa_email "${SA_MIGRATOR_ID}")"
# Cloud SQL IAM database user name of a service account = its e-mail without ".gserviceaccount.com"
DB_IAM_USER_API="${SA_API_ID}@${PROJECT_ID}.iam"
DB_IAM_USER_EXECUTOR="${SA_EXECUTOR_ID}@${PROJECT_ID}.iam"

# ---- Cloud Run -------------------------------------------------------------------------------------------
API_SERVICE="api"
EXECUTOR_SERVICE="executor"
SANDBOX_SERVICE="sandbox"
MIGRATE_JOB="migrate"
: "${INTERNAL_PREFIX:=/v1/internal}"             # SPEC §8: internal endpoints, OIDC from Scheduler only
: "${MIGRATE_CMD:=python scripts/migrate.py}"    # run inside the backend image, cwd /srv (= backend/)
: "${SANDBOX_EXEC_ENV:=gen1}"                    # gVisor kernel boundary (backend/app/sandbox/runner.py)
: "${SANDBOX_EGRESS_MODE:=direct}"               # direct | connector (fallback if Direct VPC egress + gen1
                                                 # is refused in your region — see DEPLOY.md troubleshooting)
SANDBOX_CONNECTOR="aijalon-sandbox-conn"
SANDBOX_CONNECTOR_RANGE="10.30.0.0/28"

# ---- edge / load balancer --------------------------------------------------------------------------------
LB_IP_NAME="api-ip"
LB_NEG="api-neg"
LB_BACKEND="api-backend"
LB_URLMAP="api-urlmap"
LB_PROXY="api-https-proxy"
LB_FWD_RULE="api-https-fr"
LB_SSL_POLICY="api-tls12-modern"
LB_ARMOR_POLICY="api-edge-only-cloudflare"
CERT_DNS_AUTH="api-dnsauth"
CERT_NAME="api-cert"
CERT_MAP="api-certmap"
CERT_MAP_ENTRY="api-certmap-entry"
: "${ARMOR_EDGE_HEADER_CHECK:=1}"                # also enforce X-Edge-Auth at Cloud Armor (the app checks too)

# ---- secrets (names only; values are added by the owner, never committed) --------------------------------
# name|who may read (space separated: api executor migrator)|generated? (1 = bootstrap generates a random value)
SECRETS_SPEC=(
  "STRIPE_SECRET_KEY|api|0"
  "STRIPE_WEBHOOK_SECRET|api|0"
  "TELEGRAM_BOT_TOKEN|api executor|0"
  "TELEGRAM_OPS_CHAT_ID|api executor|0"
  "EMAIL_PROVIDER_API_KEY|api executor|0"
  "SIGNALS_PUBKEY_B64|api executor|0"
  "AUDIT_PEPPER|api executor|1"
  "EDGE_AUTH_SECRET|api|1"
  "BUILDER_ADDRESS|api executor|0"
  "TREASURY_ADDRESS|api executor|0"
  "DB_MIGRATOR_PASSWORD|migrator|1"
)

# ---- Cloud Scheduler (all UTC) ---------------------------------------------------------------------------
# name|cron|path (under INTERNAL_PREFIX)|attempt deadline|max retries
SCHEDULER_SPEC=(
  "tick|* * * * *|tick|180s|0"
  "settle-daily|30 0 * * *|settle-daily|1800s|3"
  "ingest-signals|*/15 * * * *|ingest-signals|120s|1"
  "reconcile|7 * * * *|reconcile|900s|1"
  "deposits-scan|* * * * *|deposits-scan|120s|0"
  "referral-tiers|15 1 * * *|referral-tiers|900s|3"
)

# ---- Workload Identity Federation ------------------------------------------------------------------------
WIF_POOL="github"
WIF_PROVIDER="github-oidc"

# ---- local outputs (never inside the repo) ---------------------------------------------------------------
: "${OUT_DIR:=${HOME}/.aijalon-deploy/${PROJECT_ID}}"

# ---- helpers ---------------------------------------------------------------------------------------------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
# utc_in_days N -> RFC3339 timestamp N days from now (portable: GNU and BSD date both lack a common flag)
utc_in_days() { python3 -c "import datetime,sys;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"; }
