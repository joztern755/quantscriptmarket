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
: "${RUN_SUBNET:=aijalon-run}"                   # Direct VPC egress for api / migrate job
: "${RUN_SUBNET_RANGE:=10.10.0.0/24}"
# The executor has its OWN subnet + Cloud NAT + static egress IP (REVIEW_AUTH_API F1): Hyperliquid meters weight per
# IP, so user-triggered API reads can never spend the budget the executor needs for orders and exits.
: "${EXEC_SUBNET:=aijalon-run-exec}"
: "${EXEC_SUBNET_RANGE:=10.10.1.0/24}"
: "${PSA_RANGE_NAME:=aijalon-psa}"               # Private Service Access range (Cloud SQL private IP)
: "${PSA_RANGE_ADDR:=10.100.0.0}"
: "${PSA_RANGE_PREFIX:=20}"
: "${ROUTER:=aijalon-router}"
: "${NAT:=aijalon-nat}"
: "${NAT_IP_NAME:=aijalon-nat-ip-1}"            # api + migrate static egress IP (allow-list it at e-mail provider etc.)
: "${EXEC_NAT:=aijalon-nat-exec}"                # executor-only NAT on the same router
: "${EXEC_NAT_IP_NAME:=aijalon-nat-exec-ip-1}"  # executor static egress IP (Hyperliquid budget of its own)
: "${RUN_NET_TAG:=run-egress}"                  # firewall tag carried by api/executor/migrate egress
: "${SANDBOX_VPC:=aijalon-sandbox-vpc}"         # isolated: no NAT, no Private Google Access, deny-all egress
: "${SANDBOX_SUBNET:=aijalon-sandbox}"
: "${SANDBOX_SUBNET_RANGE:=10.20.0.0/24}"
: "${SANDBOX_NET_TAG:=sandbox-egress}"
: "${SANDBOX_DNS_POLICY:=aijalon-sandbox-nxdomain}"   # Cloud DNS response policy: every name → no answer (M5)
: "${SANDBOX_DNS_LOG_POLICY:=aijalon-sandbox-dnslog}" # DNS query logging on the sandbox VPC (alert on any query)

# ---- Cloud SQL -------------------------------------------------------------------------------------------
: "${SQL_INSTANCE:=aijalon-pg}"
: "${SQL_EDITION:=enterprise}"                   # explicit: PG16 otherwise defaults to enterprise-plus
: "${SQL_TIER:=db-custom-1-3840}"                # internal phase: 1 vCPU / 3.75 GB (owner 30 Sep 2026); public: db-custom-2-8192
: "${SQL_AVAILABILITY:=ZONAL}"                   # internal phase: ZONAL (owner, cost); REGIONAL (HA) before Gate C public
: "${SQL_STORAGE_GB:=20}"
: "${SQL_MAX_CONNECTIONS:=200}"                  # api 20 inst x 5 + executor 3 x 5 + migrate + headroom
: "${SQL_ENABLE_CMEK:=1}"                        # SPEC §2 "CMEK": disk encrypted with our HSM key (create-time only)
: "${DB_NAME:=aijalon}"
: "${DB_MIGRATOR_USER:=migrator}"                # built-in (password) user; password only in Secret Manager
SQL_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

# ---- KMS -------------------------------------------------------------------------------------------------
: "${KMS_KEYRING:=aijalon}"
: "${KMS_KEY:=agent-keys}"                       # SPEC §5.3 envelope KEK for agent keys (HSM, 90-day rotation)
: "${KMS_SQL_KEY:=cloudsql}"                     # CMEK for the Cloud SQL disk
: "${KMS_CODE_KEY:=creator-code}"                # creator strategy code KEK (HSM, 90-day rotation; REVIEW F2):
                                                 # api encrypt-only, executor decrypt-only — never agent-keys
KMS_KEY_NAME="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_KEY}"
KMS_CODE_KEY_NAME="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_CODE_KEY}"
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
# name|who may read (space separated: api executor migrator sandbox)|value source:
#   0     = the owner adds it (DEPLOY.md §5); bootstrap warns while it has no version
#   1     = bootstrap generates a random value
#   owner = bootstrap seeds it with OWNER_EMAIL when empty (e-mail lists: personal data, so a secret, not a
#           plain env var or a GitHub variable); the owner replaces it with the real list (DEPLOY.md §5)
#   opt   = optional: created with its accessor binding but no value; referenced by a Cloud Run template only
#           when the feature is switched on (KYC_* when KYC_PROVIDER=sumsub), so an empty one never blocks a deploy
SECRETS_SPEC=(
  "STRIPE_SECRET_KEY|api|0"
  "STRIPE_WEBHOOK_SECRET|api|0"
  "TELEGRAM_BOT_TOKEN|api executor|0"
  "TELEGRAM_OPS_CHAT_ID|api executor|0"
  "EMAIL_PROVIDER_API_KEY|api executor|0"
  "SIGNALS_PUBKEY_B64|api executor|0"
  "AUDIT_PEPPER|api executor|1"
  "EDGE_AUTH_SECRET|api|1"
  "TELEGRAM_WEBHOOK_SECRET|api|1"      # Telegram setWebhook secret_token; checked on /v1/webhooks/telegram
  "BUILDER_ADDRESS|api executor|0"
  "TREASURY_ADDRESS|api executor|0"
  "DB_MIGRATOR_PASSWORD|migrator|1"
  # Sandbox inbound shared secret (X-Sandbox-Secret, app/sandbox/service.py refuses to start without it).
  # The ONLY grant the sandbox SA has: it protects nothing but the sandbox itself, and the sandbox has no
  # route to Google APIs (no Private Google Access / NAT), so it cannot be used from inside anyway.
  "SANDBOX_SHARED_SECRET|api executor sandbox|1"
  # HMAC key of our order cloids (app/hl/client.make_cloid, REVIEW_MONEY H2): users must not be able to compute the
  # cloids of our orders. Executor only (it places orders and runs fills-ingest). Never rotate while orders are open.
  "CLOID_SECRET|executor|1"
  # comma-separated e-mail lists (app/config.py). ALLOWLIST_EMAILS: LAUNCH_PHASE=internal refuses to start
  # without it (every prod role). OPS_EMAILS: ops alert recipients (api + executor notifier).
  "ALLOWLIST_EMAILS|api executor|owner"
  "OPS_EMAILS|api executor|owner"
  # creator KYC (app/kyc/sumsub.py) — only when KYC_PROVIDER=sumsub; the launch default is `manual`
  "KYC_APP_TOKEN|api|opt"
  "KYC_SECRET_KEY|api|opt"
  "KYC_WEBHOOK_SECRET|api|opt"
)

# ---- Cloud Scheduler (all UTC) ---------------------------------------------------------------------------
# Every job: POST ${EXECUTOR_URL}${INTERNAL_PREFIX}/<path>, body {}, OIDC token as SA_SCHEDULER with audience =
# the executor URL (= its INTERNAL_AUDIENCE), created PAUSED (bootstrap `scheduler`; `make go-live` resumes them).
# Routes: backend/app/api/routers/internal.py + alerts_settings.internal_router, mounted only by
# create_executor_app (SPEC §8). Every job is idempotent, so retries and overlapping runs are safe.
# Deadlines: data jobs stop themselves after max_seconds=240 (app/jobs_data), so they get >= 300s.
#
# Ordering around the daily settlement (00:30, settles yesterday; PnL = fills + funding with time <= 00:00):
#   fills-ingest runs every 10 min (00:00, 00:10, 00:20 each finish by ~00:24) AND once more at 00:25
#   (fills-ingest-presettle) so the last fills of the day are stored before 00:30 — a fill stored after its
#   day was settled is booked into the NEXT settlement (REVIEW_MONEY M3: settlement claims every unclaimed row).
#   funding-scan runs at :07 (the 00:00 funding payment is stored at 00:07, before settlement).
#   daily-pnl-summary (00:15) reads the previous day's fills after the 00:00/00:10 fills-ingest runs.
#   referral-tiers (01:15) runs after settlement on purpose: the NEXT settlement uses the new tier.
#   verify-chain (03:40) verifies every ledger/audit/account hash chain + running balances + earlier anchors, then
#   anchors today's chain heads (DB row + ops Telegram/email) — REVIEW_MONEY M7(a).
#   settle-daily DEFERS any subscription whose trading address fills-ingest / funding-scan have not synced past the
#   cut-off (ops event `settlement_deferred`); settle-daily-retry (02:30 and 06:30, same route, body {} = yesterday)
#   settles those once the data jobs have caught up. Re-running settle-daily is always a no-op for settled days.
# candles-sync runs at :05/:15/…/:55 (not :00/:10) so it never starts together with fills-ingest. Every Hyperliquid
# /info caller on the executor (tick, data jobs, reconcile) charges ONE shared per-egress-IP budget in Postgres
# (hl_rate_budget; HL_BUDGET_WEIGHT_PER_MINUTE=800 with HL_TICK_RESERVE_PER_MINUTE=300 kept for the tick — data jobs
# back off to the next minute when their share is spent). Hyperliquid's own limit is ~1200/min/IP (UNVERIFIED).
# name|cron|path (under INTERNAL_PREFIX)|attempt deadline|max retries
SCHEDULER_SPEC=(
  "tick|* * * * *|tick|180s|0"
  "deliver-alerts|* * * * *|deliver-alerts|120s|0"
  "deposits-scan|*/5 * * * *|deposits-scan|300s|0"
  "candles-sync|5-59/10 * * * *|candles-sync|300s|0"
  "fills-ingest|*/10 * * * *|fills-ingest|300s|0"
  "fills-ingest-presettle|25 0 * * *|fills-ingest|300s|1"
  "funding-scan|7 * * * *|funding-scan|300s|1"
  "ingest-signals|50 * * * *|ingest-signals|300s|1"
  "reconcile|0 * * * *|reconcile|900s|1"
  "daily-pnl-summary|15 0 * * *|daily-pnl-summary|600s|2"
  "settle-daily|30 0 * * *|settle-daily|1800s|3"
  "settle-daily-retry|30 2,6 * * *|settle-daily|1800s|3"
  "referral-tiers|15 1 * * *|referral-tiers|900s|3"
  "verify-chain|40 3 * * *|verify-chain|900s|3"
  "agent-expiry-scan|13 */6 * * *|agent-expiry-scan|300s|1"
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

# sql_user_password INSTANCE USER  (password on STDIN) — creates the built-in user or sets its password through
# the Cloud SQL Admin REST API, so the password never appears in argv or in gcloud's own command logs
# (~/.config/gcloud/logs records the arguments of `gcloud sql users ... --password=`).
sql_user_password() {
  SQLPW_TOKEN="$(gcloud auth print-access-token)" SQLPW_PROJECT="${PROJECT_ID}" SQLPW_INSTANCE="$1" SQLPW_USER="$2" \
  python3 -c '
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
pw = sys.stdin.read()
if not pw:
    sys.exit("sql_user_password: empty password on stdin")
p, i, u, tok = (os.environ[k] for k in ("SQLPW_PROJECT", "SQLPW_INSTANCE", "SQLPW_USER", "SQLPW_TOKEN"))
base = f"https://sqladmin.googleapis.com/v1/projects/{p}"
def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")
body = {"name": u, "password": pw}
try:
    op = call("PUT", f"{base}/instances/{i}/users?name={urllib.parse.quote(u)}", body)      # users.update
except urllib.error.HTTPError as e:
    if e.code != 404:
        sys.exit(f"users.update {u}: HTTP {e.code}")
    op = call("POST", f"{base}/instances/{i}/users", body)                                   # users.insert
opname = op.get("name", "")
for _ in range(120):
    st = call("GET", base + "/operations/" + opname)
    if st.get("status") == "DONE":
        err = st.get("error")
        if err:
            sys.exit("sql user " + u + ": " + json.dumps(err))
        sys.exit(0)
    time.sleep(2)
sys.exit(f"sql user {u}: operation did not finish")'
}
