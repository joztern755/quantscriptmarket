"""Executor-only internal routes for the signing-trust fixes (docs/security/REVIEW_WEB_INFRA.md H1, M4). Mounted
by ``create_executor_app`` next to routers/internal.py; same Cloud Scheduler OIDC authentication.

POST /v1/internal/attest-agents             every minute  — app.execution.jobs.attest_agents
                                                              (generates keys for pending agent requests first —
                                                              executor keygen, migrations/0016 — then attests)
POST /v1/internal/generate-agents           on demand     — app.execution.jobs.generate_agents (keygen only; the
                                                              tick also runs it with a small budget every minute)
POST /v1/internal/agent-substitution-scan   every 10 min  — app.execution.jobs.agent_substitution_scan
POST /v1/internal/selftest                  deploy only   — app.execution.jobs.executor_selftest

/selftest is called by the deploy (infra/gcp/deploy.sh executor-canary) through the PAUSED Cloud Scheduler job
``executor-selftest``, whose URI is the new revision's ``candidate---`` tag URL, BEFORE any traffic moves to it.
Its OIDC token (scheduler SA, as for every internal route) may carry either the service URL or that tag URL as
audience. It never writes the database (no audit row either) and answers 503 when any check fails.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api import schemas as S
from app.api.deps import Services, _bearer, get_services, scheduler_auth
from app.errors import Forbidden, Unauthorized

router = APIRouter(prefix="/internal", tags=["internal"])

CANDIDATE_TAG = "candidate"


def _register_jobs() -> None:
    """Same pattern as routers/internal.py data jobs: an explicit JOB_ENTRYPOINTS entry always wins."""
    try:
        from app.api.adapters import JOB_ENTRYPOINTS
    except ImportError:  # pragma: no cover
        return
    for name, fn in (("attest-agents", "attest_agents"), ("generate-agents", "generate_agents"),
                     ("agent-substitution-scan", "agent_substitution_scan"), ("selftest", "executor_selftest")):
        JOB_ENTRYPOINTS.setdefault(name, (("app.execution.jobs", fn),))


_register_jobs()


def candidate_audience(service_url: str) -> str:
    """https://executor-abc-as.a.run.app → https://candidate---executor-abc-as.a.run.app (Cloud Run tag URL)."""
    p = urlsplit(service_url)
    if p.scheme != "https" or not p.netloc:
        return ""
    return f"https://{CANDIDATE_TAG}---{p.netloc}"


def selftest_auth(request: Request, svc: Services = Depends(get_services)) -> dict[str, Any]:
    """scheduler_auth, but the audience may also be the candidate tag URL of this service."""
    cfg = svc.config
    if not cfg.scheduler_sa_email:
        raise Unauthorized("internal auth not configured")
    token = _bearer(request)
    claims: dict[str, Any] | None = None
    last: Exception | None = None
    for aud in (cfg.internal_audience, candidate_audience(cfg.internal_audience)):
        if not aud:
            continue
        try:
            claims = svc.oidc.verify(token, aud)
            break
        except Exception as e:  # noqa: BLE001 - try the next audience, re-raise the last failure
            last = e
    if claims is None:
        raise last if last is not None else Unauthorized("invalid token")
    email = str(claims.get("email") or "").lower()
    if email != cfg.scheduler_sa_email or claims.get("email_verified") is not True:
        raise Forbidden("caller not allowed")
    return claims


def _run(svc: Services, job: str) -> S.JobOut:
    from app.api.routers.internal import _run as audited_run

    return audited_run(svc, job, {})


@router.post("/attest-agents", response_model=S.JobOut, dependencies=[Depends(scheduler_auth)])
def attest_agents(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "attest-agents")


@router.post("/generate-agents", response_model=S.JobOut, dependencies=[Depends(scheduler_auth)])
def generate_agents(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "generate-agents")


@router.post("/agent-substitution-scan", response_model=S.JobOut, dependencies=[Depends(scheduler_auth)])
def agent_substitution_scan(svc: Services = Depends(get_services)) -> S.JobOut:
    return _run(svc, "agent-substitution-scan")


@router.post("/selftest", response_model=S.JobOut, dependencies=[Depends(selftest_auth)])
def selftest(svc: Services = Depends(get_services)) -> Any:
    result = svc.jobs.run("selftest", db=svc.db, now=svc.now(), params={})
    ok = bool(result.get("ok"))
    body = S.JobOut(job="selftest", ok=ok, result=result).model_dump(mode="json")
    return JSONResponse(status_code=200 if ok else 503, content=body)
