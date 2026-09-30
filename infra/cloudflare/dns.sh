#!/usr/bin/env bash
# aijalon.trade — Cloudflare zone configuration as code (idempotent). DNS, TLS, HSTS, WAF, rate limits, cache
# bypass and the Transform Rule that injects the secret X-Edge-Auth header on api requests.
#
# WHY THIS SHAPE (decision record — see docs/ARCHITECTURE.md §4):
#   aijalon.trade      -> Firebase Hosting, DNS-only (grey cloud). Firebase serves the static SPA with its own
#                         managed certificate and our security headers (firebase.json). Proxying it through
#                         Cloudflare would put Firebase's ACME renewals behind a proxy for no security gain on a
#                         static site; the money-moving surface is the API.
#   api.aijalon.trade  -> Cloudflare PROXIED -> Global external HTTPS Load Balancer (serverless NEG -> Cloud Run
#                         "api", ingress internal-and-cloud-load-balancing). Chosen over Cloud Run domain
#                         mappings (Preview / "not recommended for production", region availability uncertain)
#                         and over a Cloudflare Worker -> *.run.app proxy (would need ingress=all, leaving the
#                         run.app origin reachable around the WAF). The LB's Cloud Armor policy admits only
#                         Cloudflare IPs carrying X-Edge-Auth; the app re-checks X-Edge-Auth in constant time.
#                         LB certificate: Certificate Manager with DNS authorization (CNAME below), which works
#                         while Cloudflare proxies the name. SSL mode Full (strict) validates it.
#
# Inputs (environment):
#   EMAIL_DNS_RECORDS          optional. Lines "TYPE|NAME|VALUE" the e-mail provider (Resend) asks for.
#   CLOUDFLARE_API_TOKEN       required. Scoped to zone aijalon.trade only (permissions in docs/DEPLOY.md §8).
#   FIREBASE_A_RECORDS         required. Space-separated IPv4(s) Firebase shows under Hosting > Add custom domain
#                              (or `firebase hosting:sites:get` / console) for aijalon.trade, e.g. "199.36.158.100".
#   FIREBASE_TXT               required. The ownership TXT value Firebase shows, e.g. "hosting-site=aijalon-trade-prod".
#   FIREBASE_ACME_NAME/_VALUE  optional. If Firebase asks for an _acme-challenge record, pass it here.
#   WWW_REDIRECT=1             optional (default 1). Adds www with the same Firebase A records (add www as a second
#                              custom domain in Firebase, set to redirect to aijalon.trade).
#   API_LB_IP                  required. From bootstrap outputs (~/.aijalon-deploy/<project>/outputs.env).
#   API_CERT_DNS_AUTH_NAME     required. Certificate Manager DNS authorization record name (outputs.env).
#   API_CERT_DNS_AUTH_VALUE    required. ... and its CNAME target.
#   EDGE_AUTH_SECRET           optional; if unset it is read from Secret Manager (gcloud, PROJECT_ID).
#   CF_PLAN                    free | pro | business (default free). Pro+ enables the managed WAF rulesets.
#   GEO_BLOCK=1                block restricted jurisdictions at the API edge (webhooks + /healthz exempt).
#   RESTRICTED_COUNTRIES       default = app/config.py DEFAULT_RESTRICTED.
#   DMARC_POLICY               default "reject" (set up your e-mail provider's SPF/DKIM before sending mail).
#
# Rulesets are written as whole phase entrypoints (PUT): the rules below ARE the configuration. Manual edits in
# the Cloudflare dashboard for these phases are overwritten on the next run — change this file instead.
set -euo pipefail

ZONE_NAME="${ZONE_NAME:-aijalon.trade}"
API_HOST="api.${ZONE_NAME}"
CF="${CF_API_BASE:-https://api.cloudflare.com/client/v4}"
: "${CF_PLAN:=free}"
: "${GEO_BLOCK:=1}"
: "${WWW_REDIRECT:=1}"
: "${RESTRICTED_COUNTRIES:=US CU IR KP SY RU BY MM}"
: "${DMARC_POLICY:=reject}"
: "${PROJECT_ID:=aijalon-trade-prod}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
command -v curl >/dev/null || die "curl required"
command -v python3 >/dev/null || die "python3 required"

[[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] || die "CLOUDFLARE_API_TOKEN is required"
[[ -n "${FIREBASE_A_RECORDS:-}" ]] || die "FIREBASE_A_RECORDS is required (from Firebase console > Hosting > Add custom domain)"
[[ -n "${FIREBASE_TXT:-}" ]] || die "FIREBASE_TXT is required"
[[ -n "${API_LB_IP:-}" ]] || die "API_LB_IP is required (bootstrap outputs.env)"
[[ -n "${API_CERT_DNS_AUTH_NAME:-}" && -n "${API_CERT_DNS_AUTH_VALUE:-}" ]] || die "API_CERT_DNS_AUTH_NAME/_VALUE required"
if [[ -z "${EDGE_AUTH_SECRET:-}" ]]; then
  command -v gcloud >/dev/null || die "EDGE_AUTH_SECRET unset and gcloud missing"
  EDGE_AUTH_SECRET="$(gcloud secrets versions access latest --secret=EDGE_AUTH_SECRET --project="${PROJECT_ID}")"
fi
[[ "${EDGE_AUTH_SECRET}" =~ ^[0-9a-f]{64}$ ]] || die "EDGE_AUTH_SECRET must be 64 lowercase hex chars"

# ---- API helper: cf METHOD PATH [JSON]  -> prints .result as JSON; dies on success:false ------------------
cf() {
  # token and body go through stdin/process substitution, never argv (not visible in `ps`)
  local method="$1" path="$2" body="${3:-}" out
  out="$(printf '%s' "${body}" | curl -sS -X "${method}" "${CF}${path}" \
        -H @<(printf 'Authorization: Bearer %s\n' "${CLOUDFLARE_API_TOKEN}") \
        -H "Content-Type: application/json" ${body:+--data-binary @-})"
  printf '%s' "${out}" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if not d.get("success"):
    sys.stderr.write("Cloudflare API error: "+json.dumps(d.get("errors"))+"\n"); sys.exit(1)
print(json.dumps(d.get("result")))' || die "${method} ${path} failed"
}
jget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1],{'d':d}))" "$1"; }

log "token check"
cf GET /user/tokens/verify >/dev/null
ZONE_ID="$(cf GET "/zones?name=${ZONE_NAME}" | jget "d[0]['id'] if d else ''")"
[[ -n "${ZONE_ID}" ]] || die "zone ${ZONE_NAME} not visible to this token"
log "zone ${ZONE_NAME} = ${ZONE_ID}"

# ---- DNS -------------------------------------------------------------------------------------------------
# set_records TYPE NAME PROXIED VALUE... : ensures VALUE... exist at NAME. With EXCLUSIVE=1 (default) other
# records of TYPE at NAME are deleted; EXCLUSIVE=0 only adds (used for apex TXT, which other services share).
set_records() {
  local type="$1" name="$2" proxied="$3"; shift 3
  local want=("$@") existing
  existing="$(cf GET "/zones/${ZONE_ID}/dns_records?type=${type}&name=${name}&per_page=100")"
  local v id
  # delete records not wanted
  while IFS=$'\t' read -r id v; do
    [[ -z "${id}" ]] && continue
    local keep=0 w
    for w in "${want[@]}"; do [[ "${v}" == "${w//\"/}" ]] && keep=1; done
    if [[ "${keep}" == 0 && "${EXCLUSIVE:-1}" == 1 ]]; then log "  delete ${type} ${name} ${v}"; cf DELETE "/zones/${ZONE_ID}/dns_records/${id}" >/dev/null; fi
  done < <(printf '%s' "${existing}" | python3 -c '
import json,sys
for r in json.load(sys.stdin):
    c=r["content"].replace("\"","")
    if r["type"]=="MX": c=str(r.get("priority"))+" "+c
    print(r["id"]+"\t"+c)')
  # create or update wanted
  for w in "${want[@]}"; do
    id="$(printf '%s' "${existing}" | python3 -c '
import json,sys
w=sys.argv[1].replace("\"","")
key=lambda r: (str(r.get("priority"))+" " if r["type"]=="MX" else "")+r["content"].replace("\"","")
print(next((r["id"] for r in json.load(sys.stdin) if key(r)==w),""))' "${w}")"
    local body
    body="$(python3 -c '
import json,sys
t,n,p,c=sys.argv[1:5]
r={"type":t,"name":n,"content":c,"ttl":1 if p=="true" else 300,"proxied":p=="true"}
if t=="TXT": r["content"]="\""+c+"\""
if t=="MX":
    prio,host=c.split(" ",1); r["content"]=host; r["priority"]=int(prio); r.pop("proxied")
if t=="CAA":
    flags,tag,val=c.split(" ",2); r.pop("content"); r["data"]={"flags":int(flags),"tag":tag,"value":val.strip("\"")}
if t in ("TXT","CAA"): r.pop("proxied")
print(json.dumps(r))' "${type}" "${name}" "${proxied}" "${w}")"
    if [[ -n "${id}" ]]; then
      cf PUT "/zones/${ZONE_ID}/dns_records/${id}" "${body}" >/dev/null
    else
      log "  create ${type} ${name} ${w} (proxied=${proxied})"; cf POST "/zones/${ZONE_ID}/dns_records" "${body}" >/dev/null
    fi
  done
}

log "DNS: apex -> Firebase Hosting (DNS only)"
# shellcheck disable=SC2086
set_records A "${ZONE_NAME}" false ${FIREBASE_A_RECORDS}
EXCLUSIVE=0 set_records TXT "${ZONE_NAME}" false "${FIREBASE_TXT}"
if ! cf GET "/zones/${ZONE_ID}/dns_records?type=TXT&name=${ZONE_NAME}&per_page=100" \
     | python3 -c 'import json,sys;sys.exit(0 if any(r["content"].replace("\"","").startswith("v=spf1") for r in json.load(sys.stdin)) else 1)'; then
  EXCLUSIVE=0 set_records TXT "${ZONE_NAME}" false "v=spf1 -all"   # no mail yet; a provider SPF replaces this
fi
if [[ "${WWW_REDIRECT}" == "1" ]]; then
  # shellcheck disable=SC2086
  set_records A "www.${ZONE_NAME}" false ${FIREBASE_A_RECORDS}
fi
if [[ -n "${FIREBASE_ACME_NAME:-}" && -n "${FIREBASE_ACME_VALUE:-}" ]]; then
  EXCLUSIVE=0 set_records TXT "${FIREBASE_ACME_NAME}" false "${FIREBASE_ACME_VALUE}"
fi
log "DNS: api -> Global HTTPS LB (PROXIED) + Certificate Manager DNS authorization"
set_records A "${API_HOST}" true "${API_LB_IP}"
set_records CNAME "${API_CERT_DNS_AUTH_NAME%.}" false "${API_CERT_DNS_AUTH_VALUE%.}"
log "DNS: CAA (Let's Encrypt for Firebase, Google Trust Services for the LB cert; Cloudflare adds its own CAs)"
set_records CAA "${ZONE_NAME}" false '0 issue "letsencrypt.org"' '0 issue "pki.goog"' '0 iodef "mailto:app.aijalon@gmail.com"'
log "DNS: DMARC (${DMARC_POLICY})"
set_records TXT "_dmarc.${ZONE_NAME}" false "v=DMARC1; p=${DMARC_POLICY}; adkim=s; aspf=r; rua=mailto:app.aijalon@gmail.com"
if [[ -n "${EMAIL_DNS_RECORDS:-}" ]]; then
  # Provider records exactly as the provider shows them (Resend: DKIM TXT resend._domainkey, and on the
  # send.<domain> return-path subdomain an MX + SPF TXT). One per line:  TYPE|NAME|VALUE   (MX VALUE = "10 host")
  log "DNS: e-mail provider records"
  while IFS='|' read -r t n v; do
    [[ -z "${t}" || "${t}" == \#* ]] && continue
    EXCLUSIVE=0 set_records "${t}" "${n}" false "${v}"
  done <<<"${EMAIL_DNS_RECORDS}"
fi
log "apex SPF stays 'v=spf1 -all': Resend sends with DKIM d=${ZONE_NAME} (DMARC aligned) and uses send.${ZONE_NAME} for SPF."

# ---- zone settings ---------------------------------------------------------------------------------------
setting() { cf PATCH "/zones/${ZONE_ID}/settings/$1" "{\"value\":$2}" >/dev/null && log "  $1 = $2"; }
log "zone TLS/security settings"
setting ssl '"strict"'
setting always_use_https '"on"'
setting min_tls_version '"1.2"'
setting tls_1_3 '"on"'
setting automatic_https_rewrites '"on"'
setting 0rtt '"off"'                  # 0-RTT data is replayable — never for a money API
setting security_header '{"strict_transport_security":{"enabled":true,"max_age":63072000,"include_subdomains":true,"preload":true,"nosniff":true}}'
setting browser_check '"on"'
setting email_obfuscation '"off"'     # rewrites HTML with inline script -> would violate our CSP
setting rocket_loader '"off"'         # same reason
setting always_online '"off"'
setting opportunistic_encryption '"on"'
setting ip_geolocation '"on"'         # CF-IPCountry header (app uses it only after X-Edge-Auth validates)

log "DNSSEC"
cf PATCH "/zones/${ZONE_ID}/dnssec" '{"status":"active"}' >/dev/null || warn "enable DNSSEC in the dashboard"

# ---- rulesets ---------------------------------------------------------------------------------------------
put_phase() { # phase json-rules-array   (rules via env, not argv: the transform rule carries the edge secret)
  local body; body="$(RULES="$2" python3 -c 'import json,os;print(json.dumps({"rules":json.loads(os.environ["RULES"])}))')"
  cf PUT "/zones/${ZONE_ID}/rulesets/phases/$1/entrypoint" "${body}" >/dev/null && log "  phase $1 written"
}
H="http.host eq \"${API_HOST}\""
countries="$(printf '"%s" ' ${RESTRICTED_COUNTRIES})"

log "Transform Rule: inject X-Edge-Auth on ${API_HOST} (client-supplied values are overwritten)"
put_phase http_request_late_transform "$(EDGE_AUTH_SECRET="${EDGE_AUTH_SECRET}" python3 -c '
import json,os,sys
h,secret=sys.argv[1],os.environ["EDGE_AUTH_SECRET"]
print(json.dumps([{"description":"api: inject edge auth header (checked by Cloud Armor + app)","expression":h,
  "action":"rewrite","action_parameters":{"headers":{"X-Edge-Auth":{"operation":"set","value":secret}}},"enabled":True}]))' "${H}")"

log "WAF custom rules"
rules="$(python3 -c '
import json,sys
h,countries,geo=sys.argv[1:4]
r=[
 {"description":"api: internal endpoints are never public","action":"block",
  "expression":f"({h} and starts_with(http.request.uri.path, \"/v1/internal/\"))"},
 {"description":"api: only /v1/* and /healthz exist","action":"block",
  "expression":f"({h} and not starts_with(http.request.uri.path, \"/v1/\") and http.request.uri.path ne \"/healthz\")"},
 {"description":"api: Telegram bot webhook only from the published Telegram ranges","action":"block",
  "expression":f"({h} and http.request.uri.path eq \"/v1/webhooks/telegram\" and not ip.src in {{149.154.160.0/20 91.108.4.0/22}})"},
 {"description":"api: allowed methods only","action":"block",
  "expression":f"({h} and not http.request.method in {{\"GET\" \"POST\" \"PATCH\" \"PUT\" \"DELETE\" \"OPTIONS\" \"HEAD\"}})"},
]
if geo=="1":
  r.append({"description":"api: restricted jurisdictions (webhooks and health exempt)","action":"block",
   "expression":f"({h} and ip.src.country in {{{countries.strip()}}} and not http.request.uri.path in {{\"/v1/webhooks/stripe\" \"/v1/webhooks/telegram\" \"/healthz\"}})"})
for x in r: x["enabled"]=True
print(json.dumps(r))' "${H}" "${countries}" "${GEO_BLOCK}")"
put_phase http_request_firewall_custom "${rules}"

log "rate limiting (plan: ${CF_PLAN})"
rl="$(python3 -c '
import json,sys
h,plan=sys.argv[1:3]
free = plan=="free"
r=[{"description":"api: per-IP ceiling","action":"block","enabled":True,
    "expression":f"({h} and not http.request.uri.path in {{\"/v1/webhooks/stripe\" \"/v1/webhooks/telegram\"}})",
    "ratelimit":{"characteristics":["cf.colo.id","ip.src"],"period":10,"requests_per_period":50,"mitigation_timeout":10}}]
if not free:
  r[0]["ratelimit"]={"characteristics":["cf.colo.id","ip.src"],"period":60,"requests_per_period":300,"mitigation_timeout":600}
  r.append({"description":"api: money / key endpoints (agents, withdrawals, deposits, subscriptions writes)","action":"block","enabled":True,
    "expression":f"({h} and http.request.method ne \"GET\" and (starts_with(http.request.uri.path, \"/v1/agents\") or starts_with(http.request.uri.path, \"/v1/withdrawals\") or starts_with(http.request.uri.path, \"/v1/deposits\") or starts_with(http.request.uri.path, \"/v1/subscriptions\") or starts_with(http.request.uri.path, \"/v1/admin\")))",
    "ratelimit":{"characteristics":["cf.colo.id","ip.src"],"period":60,"requests_per_period":20,"mitigation_timeout":600}})
print(json.dumps(r))' "${H}" "${CF_PLAN}")"
put_phase http_ratelimit "${rl}"

log "cache: never cache the API at the edge"
put_phase http_request_cache_settings "$(python3 -c '
import json,sys
print(json.dumps([{"description":"api: bypass cache","expression":sys.argv[1],"action":"set_cache_settings",
  "action_parameters":{"cache":False},"enabled":True}]))' "${H}")"

if [[ "${CF_PLAN}" != "free" ]]; then
  log "managed WAF: Cloudflare Managed Ruleset + OWASP Core Ruleset (Pro+)"
  # IDs are Cloudflare's published managed-ruleset ids (verify in dashboard: Security > WAF > Managed rules).
  put_phase http_request_firewall_managed '[
    {"description":"Cloudflare Managed Ruleset","action":"execute","expression":"true","enabled":true,
     "action_parameters":{"id":"efb7b8c949ac4650a09736fc376e9aee"}},
    {"description":"Cloudflare OWASP Core Ruleset","action":"execute","expression":"http.host eq \"'"${API_HOST}"'\"","enabled":true,
     "action_parameters":{"id":"4814384a9e5d4991b9815dcfc25d2f1f"}}]'
else
  warn "CF_PLAN=free: only the auto-deployed Free Managed Ruleset applies. Pro is recommended before public launch."
fi

log "done. Check: https://${ZONE_NAME} (Firebase cert may take up to 24 h), https://${API_HOST}/healthz"
log "the X-Edge-Auth value is now in Cloudflare config: rotate it with docs/RUNBOOK.md 'edge secret rotation'"
