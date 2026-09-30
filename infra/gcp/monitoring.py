#!/usr/bin/env python3
"""Idempotent Cloud Monitoring setup for aijalon.trade (called by bootstrap.sh step `monitoring`).

Stdlib only. Auth = `gcloud auth print-access-token` of the operator. Upserts by displayName, so re-running
updates policies in place. Creates:
  * an e-mail notification channel (ALERT_EMAIL)
  * log-match alert policies for security events (KMS / Secret Manager misuse, IAM changes, SQL admin,
    app CRITICAL logs, executor errors, Scheduler failures)
  * metric alert policies (api 5xx, Cloud SQL CPU / disk / connections)
  * uptime checks for https://API_DOMAIN/healthz and https://WEB_DOMAIN/ with alerting
Telegram paging is done by the application (app/alerts/notifier.py); Cloud Monitoring pages by e-mail.
Add the Cloud Monitoring mobile app / SMS channel in the console for a second, independent page path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

E = os.environ
P = E["PROJECT_ID"]
BASE = "https://monitoring.googleapis.com/v3"


def token() -> str:
    return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()


TOKEN = token()


def call(method: str, url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Goog-User-Project", P)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:  # surface the API message, fail the bootstrap
        sys.stderr.write(f"{method} {url} -> {e.code}: {e.read().decode(errors='replace')}\n")
        raise


def list_all(url: str, key: str) -> list[dict]:
    out, page = [], ""
    while True:
        sep = "&" if "?" in url else "?"
        d = call("GET", f"{url}{sep}pageSize=200" + (f"&pageToken={page}" if page else ""))
        out += d.get(key, [])
        page = d.get("nextPageToken", "")
        if not page:
            return out


def upsert_channel() -> str:
    url = f"{BASE}/projects/{P}/notificationChannels"
    for c in list_all(url, "notificationChannels"):
        if c.get("type") == "email" and c.get("labels", {}).get("email_address") == E["ALERT_EMAIL"]:
            return c["name"]
    c = call("POST", url, {"type": "email", "displayName": f"ops e-mail {E['ALERT_EMAIL']}",
                           "labels": {"email_address": E["ALERT_EMAIL"]}})
    print(f"  created channel {c['name']} (confirm nothing: e-mail channels need no verification)")
    return c["name"]


def upsert_policy(policy: dict, existing: dict[str, dict]) -> None:
    name = policy["displayName"]
    if name in existing:
        cur = existing[name]
        policy["name"] = cur["name"]
        call("PATCH", f"{BASE}/{cur['name']}", policy)
        print(f"  updated policy: {name}")
    else:
        call("POST", f"{BASE}/projects/{P}/alertPolicies", policy)
        print(f"  created policy: {name}")


def log_policy(name: str, filt: str, severity: str, doc: str, channel: str, rate_s: int = 300) -> dict:
    return {
        "displayName": name,
        "combiner": "OR",
        "severity": severity,
        "documentation": {"content": doc, "mimeType": "text/markdown"},
        "conditions": [{"displayName": name, "conditionMatchedLog": {"filter": filt}}],
        "alertStrategy": {"notificationRateLimit": {"period": f"{rate_s}s"}, "autoClose": "86400s"},
        "notificationChannels": [channel],
    }


def metric_policy(name: str, filt: str, threshold: float, duration_s: int, aligner: str, severity: str,
                  doc: str, channel: str, reducer: str | None = None, group_by: list[str] | None = None,
                  period_s: int = 60, comparison: str = "COMPARISON_GT") -> dict:
    agg: dict = {"alignmentPeriod": f"{period_s}s", "perSeriesAligner": aligner}
    if reducer:
        agg["crossSeriesReducer"] = reducer
        agg["groupByFields"] = group_by or []
    return {
        "displayName": name,
        "combiner": "OR",
        "severity": severity,
        "documentation": {"content": doc, "mimeType": "text/markdown"},
        "conditions": [{"displayName": name, "conditionThreshold": {
            "filter": filt, "comparison": comparison, "thresholdValue": threshold,
            "duration": f"{duration_s}s", "aggregations": [agg],
            "trigger": {"count": 1}}}],
        "alertStrategy": {"autoClose": "86400s"},
        "notificationChannels": [channel],
    }


def upsert_log_metric(name: str, filt: str, description: str) -> None:
    """Counter log-based metric (logging.googleapis.com/user/<name>), created or updated in place."""
    base = f"https://logging.googleapis.com/v2/projects/{P}/metrics"
    body = {"name": name, "filter": filt, "description": description,
            "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64", "unit": "1"}}
    try:
        call("GET", f"{base}/{name}")
        call("PUT", f"{base}/{name}", body)
        print(f"  updated log metric: {name}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        call("POST", base, body)
        print(f"  created log metric: {name}")


def upsert_uptime(display: str, host: str, path: str, content: str | None) -> str:
    url = f"{BASE}/projects/{P}/uptimeCheckConfigs"
    cfg: dict = {
        "displayName": display,
        "monitoredResource": {"type": "uptime_url", "labels": {"project_id": P, "host": host}},
        "httpCheck": {"path": path, "port": 443, "useSsl": True, "validateSsl": True, "requestMethod": "GET",
                      "acceptedResponseStatusCodes": [{"statusClass": "STATUS_CLASS_2XX"}]},
        "period": "60s",
        "timeout": "10s",
        # >= 3 regions required; Cloudflare geo-blocking exempts /healthz so US checkers are fine.
        "selectedRegions": ["ASIA_PACIFIC", "EUROPE", "USA"],
    }
    if content:
        cfg["contentMatchers"] = [{"content": content, "matcher": "CONTAINS_STRING"}]
    for u in list_all(url, "uptimeCheckConfigs"):
        if u.get("displayName") == display:
            upd = {k: v for k, v in cfg.items() if k not in ("monitoredResource",)}
            mask = ",".join(["displayName", "httpCheck", "period", "timeout", "selectedRegions"] +
                            (["contentMatchers"] if content else []))
            call("PATCH", f"{BASE}/{u['name']}?updateMask={mask}", upd)
            print(f"  updated uptime check: {display}")
            return u["name"].rsplit("/", 1)[-1]
    u = call("POST", url, cfg)
    print(f"  created uptime check: {display}")
    return u["name"].rsplit("/", 1)[-1]


def main() -> None:
    ch = upsert_channel()
    existing = {p["displayName"]: p for p in list_all(f"{BASE}/projects/{P}/alertPolicies", "alertPolicies")}
    api, ex, mig, _dep = E["SA_API"], E["SA_EXECUTOR"], E["SA_MIGRATOR"], E["SA_DEPLOYER"]  # all four required
    sbx = E.get("SA_SANDBOX", "sandbox-sa-not-set")
    builder, hosting = E.get("SA_BUILDER", "builder-sa-not-set"), E.get("SA_HOSTING", "hosting-sa-not-set")
    attest_key, binauthz_key = E.get("KMS_ATTEST_KEY", "agent-attest"), E.get("KMS_BINAUTHZ_KEY", "binauthz-attestor")
    run = 'resource.type="cloud_run_revision"'
    run_admin = ('protoPayload.serviceName="run.googleapis.com" AND protoPayload.methodName=('
                 '"google.cloud.run.v1.Services.ReplaceService" OR "google.cloud.run.v1.Services.CreateService" OR '
                 '"google.cloud.run.v1.Services.DeleteService" OR "google.cloud.run.v2.Services.CreateService" OR '
                 '"google.cloud.run.v2.Services.UpdateService" OR "google.cloud.run.v2.Services.DeleteService" OR '
                 '"google.cloud.run.v1.Jobs.ReplaceJob" OR "google.cloud.run.v1.Jobs.CreateJob" OR '
                 '"google.cloud.run.v1.Jobs.DeleteJob" OR "google.cloud.run.v1.Jobs.RunJob" OR '
                 '"google.cloud.run.v2.Jobs.CreateJob" OR "google.cloud.run.v2.Jobs.UpdateJob" OR '
                 '"google.cloud.run.v2.Jobs.DeleteJob" OR "google.cloud.run.v2.Jobs.RunJob" OR '
                 '"google.cloud.run.v1.Services.SetIamPolicy" OR "google.cloud.run.v2.Services.SetIamPolicy")')
    policies = [
        # ---- REVIEW_WEB_INFRA H2 / L8: deploy-path tampering is visible within a minute -----------------------------
        log_policy(
            "SEC: Cloud Run EXECUTOR changed (deploy / traffic / IAM)",
            f'{run_admin} AND protoPayload.resourceName:"services/{E["EXECUTOR_SERVICE"]}"',
            "CRITICAL", "The executor (KMS decrypt of every agent key) was redeployed, re-routed or its IAM changed. "
            "Expected ONLY during an approved deploy run (GitHub Actions → production). Anything else: kill_switch_global, "
            "freeze the deployer SA, investigate (docs/RUNBOOK.md).", ch, 60),
        log_policy(
            "SEC: Cloud Run service/job created, deleted or changed",
            f'{run_admin} AND NOT protoPayload.resourceName:"services/{E["EXECUTOR_SERVICE"]}"',
            "ERROR", "Cloud Run api/sandbox/migrate changed. Expected during deploys; a CreateService/CreateJob/"
            "DeleteService is NEVER expected (the deployer cannot create or delete).", ch, 300),
        log_policy(
            "SEC: Cloud Scheduler job created/updated/deleted (target, audience or OIDC SA)",
            'protoPayload.serviceName="cloudscheduler.googleapis.com" AND protoPayload.methodName=('
            '"google.cloud.scheduler.v1.CloudScheduler.CreateJob" OR "google.cloud.scheduler.v1.CloudScheduler.UpdateJob" OR '
            '"google.cloud.scheduler.v1.CloudScheduler.DeleteJob")',
            "CRITICAL", "A scheduler job's URI / audience / service account can retarget money-moving calls. Only "
            "bootstrap `scheduler` (the owner) does this; the deployer can merely pause/resume/run.", ch, 60),
        log_policy(
            "OPS: Cloud Scheduler job paused/resumed",
            'protoPayload.serviceName="cloudscheduler.googleapis.com" AND protoPayload.methodName=('
            '"google.cloud.scheduler.v1.CloudScheduler.PauseJob" OR "google.cloud.scheduler.v1.CloudScheduler.ResumeJob")',
            "WARNING", "tick is paused/resumed by every deploy (executor canary) and by go-live / pause-all. Outside "
            "those, someone is stopping or starting trading.", ch, 3600),
        log_policy(
            "SEC: agent-attest key used by anyone but executor",
            'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName="AsymmetricSign" '
            f'AND protoPayload.resourceName:"cryptoKeys/{attest_key}" '
            f'AND NOT protoPayload.authenticationInfo.principalEmail="{ex}"',
            "CRITICAL", "Only the executor signs agent attestations (REVIEW_WEB_INFRA H1). A foreign signer can make "
            "browsers approve an attacker agent: disable the key version, rotate, re-pin app-config.json.", ch, 60),
        log_policy(
            "SEC: Binary Authorization attestor key used by anyone but builder",
            'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName="AsymmetricSign" '
            f'AND protoPayload.resourceName:"cryptoKeys/{binauthz_key}" '
            f'AND NOT protoPayload.authenticationInfo.principalEmail="{builder}"',
            "CRITICAL", "Only the CI builder identity attests images.", ch, 60),
        log_policy(
            "SEC: Binary Authorization violation or breakglass",
            '(logName:"binaryauthorization.googleapis.com") OR (protoPayload.serviceName="run.googleapis.com" AND '
            'protoPayload.request.metadata.annotations."run.googleapis.com/binauthz-breakglass":*) OR '
            '(protoPayload.serviceName="run.googleapis.com" AND protoPayload.status.message:"Binary Authorization")',
            "CRITICAL", "An unattested image was deployed (dry-run) or blocked, or breakglass was used [VERIFY the "
            "log shape after the first dry-run deploy].", ch, 300),
        log_policy(
            "SEC: Hosting release by anyone but the hosting deployer",
            'protoPayload.serviceName="firebasehosting.googleapis.com" AND protoPayload.methodName:"CreateRelease" '
            f'AND NOT protoPayload.authenticationInfo.principalEmail="{hosting}"',
            "CRITICAL", "The site (and its pinned signing trust anchors) was replaced outside the deploy pipeline.",
            ch, 60),
        log_policy(
            "APP: CSP / Trusted Types violations reported by browsers",
            f'{run} AND resource.labels.service_name="{E["API_SERVICE"]}" AND jsonPayload.message="csp_violation"',
            "WARNING", "Browsers reported CSP/Trusted-Types violations (POST /v1/csp-report). A burst after a deploy "
            "= a policy regression; a burst from one blocked host = possible injection attempt.", ch, 3600),
        log_policy(
            "SEC: agent-keys DECRYPT by anyone but executor",
            'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName="Decrypt" '
            f'AND protoPayload.resourceName:"cryptoKeys/{E["KMS_KEY"]}" '
            f'AND NOT protoPayload.authenticationInfo.principalEmail="{ex}"',
            "CRITICAL", "Only the executor may decrypt agent keys (SPEC §2.1). Treat as key compromise: "
            "set kill_switch_global, revoke the principal, rotate agent keys (docs/RUNBOOK.md).", ch, 60),
        log_policy(
            "SEC: agent-keys ENCRYPT by anyone but executor",
            'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName="Encrypt" '
            f'AND protoPayload.resourceName:"cryptoKeys/{E["KMS_KEY"]}" '
            f'AND NOT protoPayload.authenticationInfo.principalEmail="{ex}"',
            "CRITICAL", "Only the executor generates and seals agent keys (migrations/0016). An encrypt by any other "
            "principal (the api included) means someone is trying to plant an agent key they know: set "
            "kill_switch_global, revoke the principal, check agent_keys rows without keygen_at (docs/RUNBOOK.md).",
            ch, 60),
        log_policy(
            "SEC: KMS key admin change (disable/destroy/primary/IAM)",
            'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName=('
            '"DestroyCryptoKeyVersion" OR "UpdateCryptoKeyVersion" OR "UpdateCryptoKeyPrimaryVersion" OR '
            '"UpdateCryptoKey" OR "SetIamPolicy" OR "CreateCryptoKey" OR "RestoreCryptoKeyVersion")',
            "CRITICAL", "A KMS key or its IAM changed. Confirm it was a planned change.", ch, 60),
        log_policy(
            "SEC: Secret accessed by unexpected principal",
            'protoPayload.serviceName="secretmanager.googleapis.com" AND '
            'protoPayload.methodName="google.cloud.secretmanager.v1.SecretManagerService.AccessSecretVersion" AND '
            # REVIEW_WEB_INFRA L8: the sandbox SA reads SANDBOX_SHARED_SECRET on every cold start — expected, not noise
            f'NOT protoPayload.authenticationInfo.principalEmail=("{api}" OR "{ex}" OR "{mig}" OR "{sbx}")',
            "WARNING", "A secret value was read by a human or unexpected SA (break-glass / db_bootstrap / "
            "dns.sh also trigger this — confirm it was you).", ch),
        log_policy(
            "SEC: Secret added/destroyed/IAM changed",
            'protoPayload.serviceName="secretmanager.googleapis.com" AND protoPayload.methodName=('
            '"google.cloud.secretmanager.v1.SecretManagerService.AddSecretVersion" OR '
            '"google.cloud.secretmanager.v1.SecretManagerService.DestroySecretVersion" OR '
            '"google.cloud.secretmanager.v1.SecretManagerService.DeleteSecret" OR '
            '"google.iam.v1.IAMPolicy.SetIamPolicy" OR "SetIamPolicy")',
            "ERROR", "BUILDER_ADDRESS / TREASURY_ADDRESS tampering is a fraud vector: verify any change "
            "against the hardware wallet address with a second admin.", ch, 60),
        log_policy(
            "SEC: IAM policy changed",
            'protoPayload.methodName=("SetIamPolicy" OR "google.iam.admin.v1.CreateServiceAccountKey" OR '
            '"google.iam.admin.v1.CreateServiceAccount" OR "CreateWorkloadIdentityPoolProvider" OR '
            '"UpdateWorkloadIdentityPoolProvider") AND severity>=NOTICE',
            "ERROR", "IAM changed. Service-account KEYS must never be created (WIF only).", ch),
        log_policy(
            "SEC: Cloud SQL admin operation",
            'protoPayload.serviceName="cloudsql.googleapis.com" AND protoPayload.methodName=('
            '"cloudsql.instances.delete" OR "cloudsql.instances.update" OR "cloudsql.instances.patch" OR '
            '"cloudsql.instances.restoreBackup" OR "cloudsql.instances.clone" OR "cloudsql.instances.export" OR '
            '"cloudsql.users.update" OR "cloudsql.users.create" OR "cloudsql.users.delete")',
            "ERROR", "Database admin action (export = possible data exfiltration). Confirm it was planned.", ch),
        log_policy(
            "APP: CRITICAL log from any service",
            f'{run} AND severity>=CRITICAL',
            "CRITICAL", "The app logged CRITICAL (it also pages Telegram and auto-pauses entries). "
            "See docs/RUNBOOK.md.", ch, 60),
        log_policy(
            "APP: executor errors",
            f'{run} AND resource.labels.service_name="{E["EXECUTOR_SERVICE"]}" AND severity>=ERROR',
            "ERROR", "Executor raised errors (orders, settlement, reconciliation).", ch),
        log_policy(
            "OPS: Cloud Scheduler job failed",
            'resource.type="cloud_scheduler_job" AND severity>=ERROR',
            "ERROR", "A scheduled internal call (tick/settle/reconcile/...) failed or timed out.", ch),
        log_policy(
            "SEC: sandbox egress attempt blocked",
            'resource.type="gce_subnetwork" AND jsonPayload.rule_details.reference:"deny-all-egress"',
            "WARNING", "Creator code in the sandbox tried to reach the network. Review the strategy version.", ch),
        metric_policy(
            "OPS: api 5xx > 1% (5 min)",
            f'metric.type="run.googleapis.com/request_count" AND resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{E["API_SERVICE"]}" AND metric.labels.response_code_class="5xx"',
            5, 300, "ALIGN_RATE", "ERROR", "api is returning 5xx.", ch,
            reducer="REDUCE_SUM", group_by=["resource.labels.service_name"], period_s=300),
        metric_policy(
            "OPS: Cloud SQL CPU > 80%",
            f'metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND resource.type="cloudsql_database" '
            f'AND resource.labels.database_id="{P}:{E["SQL_INSTANCE"]}"',
            0.8, 600, "ALIGN_MEAN", "WARNING", "Scale the tier (gcloud sql instances patch --tier).", ch),
        metric_policy(
            "OPS: Cloud SQL disk > 80%",
            f'metric.type="cloudsql.googleapis.com/database/disk/utilization" AND resource.type="cloudsql_database" '
            f'AND resource.labels.database_id="{P}:{E["SQL_INSTANCE"]}"',
            0.8, 600, "ALIGN_MEAN", "WARNING", "Storage auto-increase is on; check for runaway tables.", ch),
        metric_policy(
            "OPS: Cloud SQL connections > 160",
            f'metric.type="cloudsql.googleapis.com/database/postgresql/num_backends" AND '
            f'resource.type="cloudsql_database" AND resource.labels.database_id="{P}:{E["SQL_INSTANCE"]}"',
            160, 300, "ALIGN_MAX", "WARNING", "Near max_connections; check pool sizes / max instances.", ch,
            reducer="REDUCE_SUM", group_by=["resource.labels.database_id"]),
    ]
    for pol in policies:
        upsert_policy(pol, existing)

    # REVIEW_WEB_INFRA L8 / SECURITY §7: agent-key decrypt RATE (a legitimate principal decrypting far more keys
    # than the tick ever needs = exfiltration through a compromised executor revision).
    decrypt_metric = "aijalon_agent_key_decrypts"
    try:
        upsert_log_metric(decrypt_metric,
                          'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName="Decrypt" '
                          f'AND protoPayload.resourceName:"cryptoKeys/{E["KMS_KEY"]}"',
                          "Cloud KMS Decrypt calls on agent-keys (every principal)")
        limit = float(E.get("DECRYPT_ALERT_PER_10MIN", "600"))
        upsert_policy(metric_policy(
            f"SEC: agent-keys decrypt rate > {int(limit)} / 10 min",
            f'metric.type="logging.googleapis.com/user/{decrypt_metric}" AND resource.type="cloudkms_cryptokey"',
            limit, 0, "ALIGN_SUM", "CRITICAL", "Far more agent keys decrypted than the tick needs. Set "
            "kill_switch_global and investigate the executor revision (docs/RUNBOOK.md).", ch,
            reducer="REDUCE_SUM", group_by=[], period_s=600), existing)
    except urllib.error.HTTPError:
        print("  WARN: decrypt-rate metric/alert not created (re-run `bootstrap.sh monitoring` in a few minutes)")

    for display, host, path, content in (
        ("api /healthz", E["API_DOMAIN"], "/healthz", None),
        ("web /", E["WEB_DOMAIN"], "/", None),
    ):
        cid = upsert_uptime(display, host, path, content)
        upsert_policy(metric_policy(
            f"UPTIME: {display} failing",
            f'metric.type="monitoring.googleapis.com/uptime_check/check_passed" AND resource.type="uptime_url" '
            f'AND metric.labels.check_id="{cid}"',
            1, 120, "ALIGN_NEXT_OLDER", "CRITICAL", f"{host}{path} is failing from multiple regions.", ch,
            reducer="REDUCE_COUNT_FALSE", group_by=["resource.label.*"], period_s=1200), existing)


if __name__ == "__main__":
    main()
