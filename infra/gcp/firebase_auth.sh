#!/usr/bin/env bash
# aijalon.trade — Firebase Auth / Identity Platform hardening (idempotent; Identity Toolkit Admin REST API v2).
#
#   ./infra/gcp/firebase_auth.sh            # upgrade to Identity Platform, TOTP MFA, lock down providers/domains
#   GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... ./infra/gcp/firebase_auth.sh google
#   APPLE_SERVICES_ID=... APPLE_TEAM_ID=... APPLE_KEY_ID=... APPLE_PRIVATE_KEY_FILE=key.p8 ./infra/gcp/firebase_auth.sh apple
#   ./infra/gcp/firebase_auth.sh show        # print the current config (no secrets are returned by the API)
#   ./infra/gcp/firebase_auth.sh apikey      # restrict the public Web API key: referrer + Identity Toolkit/Securetoken
#   AUTH_DROP_FIREBASEAPP_DOMAIN=1 ./infra/gcp/firebase_auth.sh   # authorized domains = aijalon.trade ONLY
#
# SPEC §5.2: sign-in with Google or Apple ONLY; TOTP MFA required (enforced by the API: tokens without
# firebase.sign_in_second_factor are rejected). SMS MFA stays OFF (SIM-swap). Email/password, phone and
# anonymous sign-in stay OFF. Authorized domains: aijalon.trade (+ the project's firebaseapp.com handler).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-aijalon-trade-prod}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/gcp/env.sh
source "${HERE}/env.sh"
need gcloud; need curl; need python3

: "${TOTP_ADJACENT_INTERVALS:=1}"   # accept codes from +-1 x 30 s windows (tight; phones keep good time)
IDT="https://identitytoolkit.googleapis.com"

api() { # METHOD URL [JSON] — token via stdin-free header file so it never shows in `ps`
  local method="$1" url="$2" body="${3:-}" out code
  out="$(mktemp)"
  code="$(printf '%s' "${body}" | curl -sS -o "${out}" -w '%{http_code}' -X "${method}" "${url}" \
      -H @<(printf 'Authorization: Bearer %s\nX-Goog-User-Project: %s\n' "$(gcloud auth print-access-token)" "${PROJECT_ID}") \
      -H 'Content-Type: application/json' ${body:+--data-binary @-})"
  if [[ "${code}" != 2* ]]; then cat "${out}" >&2; rm -f "${out}"; die "${method} ${url} -> HTTP ${code}"; fi
  cat "${out}"; rm -f "${out}"
}

cmd_base() {
  log "Identity Platform upgrade (idempotent; 'already initialized' is fine)"
  api POST "${IDT}/v2/projects/${PROJECT_ID}/identityPlatform:initializeAuth" '{}' >/dev/null 2>&1 \
    || warn "initializeAuth refused (usually: already upgraded). Check console > Authentication > Settings"

  log "TOTP MFA (adjacentIntervals=${TOTP_ADJACENT_INTERVALS}); SMS MFA not enabled"
  api PATCH "${IDT}/admin/v2/projects/${PROJECT_ID}/config?updateMask=mfa" \
    "{\"mfa\":{\"state\":\"ENABLED\",\"enabledProviders\":[],\"providerConfigs\":[{\"state\":\"ENABLED\",\"totpProviderConfig\":{\"adjacentIntervals\":${TOTP_ADJACENT_INTERVALS}}}]}}" >/dev/null

  log "disable email/password, phone and anonymous sign-in; one account per e-mail"
  api PATCH "${IDT}/admin/v2/projects/${PROJECT_ID}/config?updateMask=signIn.email.enabled,signIn.phoneNumber.enabled,signIn.anonymous.enabled,signIn.allowDuplicateEmails" \
    '{"signIn":{"email":{"enabled":false},"phoneNumber":{"enabled":false},"anonymous":{"enabled":false},"allowDuplicateEmails":false}}' >/dev/null

  # REVIEW_WEB_INFRA L5: authDomain is aijalon.trade (Hosting serves /__/auth/* on our own domain), so the project's
  # firebaseapp.com domain is not needed for redirect/popup sign-in. It stays until G9 has been re-tested WITHOUT it
  # (AUTH_DROP_FIREBASEAPP_DOMAIN=1): every extra authorised domain is a look-alike origin that can run sign-in.
  local domains="[\"${WEB_DOMAIN}\",\"${PROJECT_ID}.firebaseapp.com\"]"
  if [[ "${AUTH_DROP_FIREBASEAPP_DOMAIN:-0}" == "1" ]]; then domains="[\"${WEB_DOMAIN}\"]"; fi
  log "authorized domains: ${domains} (localhost and web.app removed)"
  api PATCH "${IDT}/admin/v2/projects/${PROJECT_ID}/config?updateMask=authorizedDomains" \
    "{\"authorizedDomains\":${domains}}" >/dev/null

  log "e-mail enumeration protection; users cannot delete themselves client-side (ledger retention)"
  api PATCH "${IDT}/admin/v2/projects/${PROJECT_ID}/config?updateMask=emailPrivacyConfig.enableImprovedEmailPrivacy,client.permissions.disabledUserDeletion" \
    '{"emailPrivacyConfig":{"enableImprovedEmailPrivacy":true},"client":{"permissions":{"disabledUserDeletion":true}}}' >/dev/null
  cmd_show
}

upsert_idp() { # idpId json
  local id="$1" body="$2"
  if api GET "${IDT}/admin/v2/projects/${PROJECT_ID}/defaultSupportedIdpConfigs/${id}" >/dev/null 2>&1; then
    api PATCH "${IDT}/admin/v2/projects/${PROJECT_ID}/defaultSupportedIdpConfigs/${id}?updateMask=enabled,clientId,clientSecret$( [[ "${id}" == apple.com ]] && echo ',appleSignInConfig')" "${body}" >/dev/null
  else
    api POST "${IDT}/admin/v2/projects/${PROJECT_ID}/defaultSupportedIdpConfigs?idpId=${id}" "${body}" >/dev/null
  fi
  log "${id} enabled"
}

cmd_google() {
  # The OAuth web client is created in the console (APIs & Services > Credentials) — no API for that.
  # Authorized redirect URI: https://aijalon.trade/__/auth/handler ; JS origin: https://aijalon.trade
  : "${GOOGLE_OAUTH_CLIENT_ID:?}"; : "${GOOGLE_OAUTH_CLIENT_SECRET:?}"
  upsert_idp google.com "$(GOOGLE_OAUTH_CLIENT_SECRET="${GOOGLE_OAUTH_CLIENT_SECRET}" python3 -c '
import json,os
print(json.dumps({"enabled":True,"clientId":os.environ["GOOGLE_OAUTH_CLIENT_ID"],"clientSecret":os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]}))')"
}

cmd_apple() {
  # Apple Developer: Services ID (= clientId) with Sign in with Apple, domain aijalon.trade, return URL
  # https://aijalon.trade/__/auth/handler ; a Sign in with Apple key (.p8) -> KEY_ID ; TEAM_ID.
  : "${APPLE_SERVICES_ID:?}"; : "${APPLE_TEAM_ID:?}"; : "${APPLE_KEY_ID:?}"; : "${APPLE_PRIVATE_KEY_FILE:?}"
  upsert_idp apple.com "$(python3 -c '
import json,os
print(json.dumps({"enabled":True,"clientId":os.environ["APPLE_SERVICES_ID"],
  "appleSignInConfig":{"codeFlowConfig":{"teamId":os.environ["APPLE_TEAM_ID"],"keyId":os.environ["APPLE_KEY_ID"],
                                         "privateKey":open(os.environ["APPLE_PRIVATE_KEY_FILE"]).read()}}}))')"
  warn "if the API rejects the Apple payload shape, enter the same four values in Firebase console > Authentication > Sign-in method > Apple"
}

cmd_show() {
  api GET "${IDT}/admin/v2/projects/${PROJECT_ID}/config" | python3 -c '
import json,sys
c=json.load(sys.stdin)
print(json.dumps({k:c.get(k) for k in ("signIn","mfa","authorizedDomains","emailPrivacyConfig","client")},indent=1))'
}

# REVIEW_WEB_INFRA L5: the Web API key is public by design, but unrestricted it can be used from any origin against
# every enabled API that accepts API keys. Restrict it to our site as referrer and to the two Auth APIs.
# (FIREBASE_WEB_API_KEY_ID: the key's resource id — `gcloud services api-keys list`; default = the key whose
# keyString equals FIREBASE_WEB_API_KEY from ~/.aijalon-deploy/<project>/firebase.env.)
cmd_apikey() {
  local kid="${FIREBASE_WEB_API_KEY_ID:-}"
  if [[ -z "${kid}" ]]; then
    local want="${FIREBASE_WEB_API_KEY:-}"
    [[ -z "${want}" && -f "${OUT_DIR}/firebase.env" ]] && want="$(sed -n 's/^FIREBASE_WEB_API_KEY=//p' "${OUT_DIR}/firebase.env")"
    [[ -n "${want}" ]] || die "set FIREBASE_WEB_API_KEY_ID or FIREBASE_WEB_API_KEY"
    local k
    for k in $(gcloud services api-keys list --project="${PROJECT_ID}" --format='value(name)'); do
      if [[ "$(gcloud services api-keys get-key-string "${k}" --format='value(keyString)')" == "${want}" ]]; then kid="${k}"; break; fi
    done
    [[ -n "${kid}" ]] || die "no API key in ${PROJECT_ID} matches FIREBASE_WEB_API_KEY"
  fi
  log "restricting API key ${kid##*/}: referrer https://${WEB_DOMAIN}/*, APIs identitytoolkit + securetoken"
  gcloud services api-keys update "${kid}" --project="${PROJECT_ID}" \
    --allowed-referrers="https://${WEB_DOMAIN}/*" \
    --api-target=service=identitytoolkit.googleapis.com --api-target=service=securetoken.googleapis.com >/dev/null
  gcloud services api-keys describe "${kid}" --project="${PROJECT_ID}" --format='yaml(displayName,restrictions)'
}

case "${1:-base}" in
  base) cmd_base ;; google) cmd_google ;; apple) cmd_apple ;; show) cmd_show ;; apikey) cmd_apikey ;;
  *) die "usage: $0 [base|google|apple|show|apikey]" ;;
esac
