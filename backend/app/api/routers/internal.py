"""Internal jobs — mounted ONLY on the executor service (create_executor_app): DB role app_executor, ingress
internal, KMS decrypt for tick. Callers must present a Google-signed OIDC ID token whose audience is this
service's URL (settings.internal_audience) and whose email is the Cloud Scheduler service account
(settings.scheduler_sa_email) — verified with google.oauth2.id_token.verify_oauth2_token (deps.scheduler_auth).

Each job is idempotent in its own module (ledger keys, UNIQUE constraints), so Scheduler retries are safe.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends

from app.api import schemas as S
from app.api.deps import Services, get_services, scheduler_auth

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(scheduler_auth)])


def _run(svc: Services, job: str, params: dict[str, Any]) -> S.JobOut:
    now = svc.now()
    result = svc.jobs.run(job, db=svc.db, now=now, params=params)
    # bounded, canonical-JSON-safe audit payload (the audit chain refuses floats)
    summary = {str(k): str(result[k])[:200] for k in list(result)[:20]}
    with svc.db.begin() as conn:
        svc.audit.write(conn, actor="system:scheduler", action=f"job.{job}", target="",
                        payload={"params": {k: str(val) for k, val in params.items()}, "result": summary},
                        ip_hash=None)
    return S.JobOut(job=job, ok=True, result=result)


@router.post("/tick", response_model=S.JobOut)
def tick(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "tick", {})


@router.post("/settle-daily", response_model=S.JobOut)
def settle_daily(body: Optional[S.SettleIn] = Body(None), svc: Services = Depends(get_services)) -> S.JobOut:
    settle_date = body.settle_date if body and body.settle_date else (svc.now() - timedelta(days=1)).date()
    return _run(svc, "settle-daily", {"settle_date": settle_date})


@router.post("/ingest-signals", response_model=S.JobOut)
def ingest_signals(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "ingest-signals", {})


@router.post("/reconcile", response_model=S.JobOut)
def reconcile(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "reconcile", {})


@router.post("/deposits-scan", response_model=S.JobOut)
def deposits_scan(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "deposits-scan", {})


@router.post("/referral-tiers", response_model=S.JobOut)
def referral_tiers(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "referral-tiers", {})


# ------------------------------------------------------------------------------------------------ data jobs
# Owner: app/jobs_data (migrations/0006_data.sql). Each is fn(db=, now=, **params), idempotent, bounded per call and
# resumable (job_cursors). Registered with the jobs adapter here so the routes work before
# app.api.adapters.JOB_ENTRYPOINTS lists them (setdefault: an explicit entry there always wins).
def _register_data_jobs() -> None:
    try:
        from app.api.adapters import JOB_ENTRYPOINTS
    except ImportError:  # pragma: no cover - adapters always importable with the API
        return
    for name, fn in (("candles-sync", "candles_sync"), ("fills-ingest", "fills_ingest"),
                     ("funding-scan", "funding_scan"), ("agent-expiry-scan", "agent_expiry_scan")):
        JOB_ENTRYPOINTS.setdefault(name, (("app.jobs_data", fn),))


_register_data_jobs()


@router.post("/candles-sync", response_model=S.JobOut)
def candles_sync(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "candles-sync", {})


@router.post("/fills-ingest", response_model=S.JobOut)
def fills_ingest(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "fills-ingest", {})


@router.post("/funding-scan", response_model=S.JobOut)
def funding_scan(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "funding-scan", {})


@router.post("/agent-expiry-scan", response_model=S.JobOut)
def agent_expiry_scan(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "agent-expiry-scan", {})
