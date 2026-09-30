"""Consents (append-only evidence): POST /consents (batch) and GET /consents/status.

Only valid MFA session required (this is how a new user clears the consent gate). Every item must match the
CURRENT document version; `doc_text_sha256` is the hash of the exact rendered text the user saw — when the
server knows the canonical hash for that version (settings.legal_doc_hashes) a mismatch is refused and a missing
client hash is filled with the canonical one; without either the consent is refused (the DB column is NOT NULL:
we never record a consent without evidence of which text was accepted).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api import validation as v
from app.api.deps import (
    SITE_DOCS,
    AuthCtx,
    JurisdictionBlocked,
    Services,
    current_user,
    get_services,
    user_limit,
)
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

router = APIRouter(tags=["consents"])


def consent_status(conn: Any, svc: Services, user_id: str) -> S.ConsentStatusOut:
    versions = svc.config.legal_versions
    required = {d: versions[d] for d in SITE_DOCS}
    accepted = svc.store.accepted_consents(conn, user_id)
    missing = [d for d, ver in required.items() if accepted.get(d) != ver]
    return S.ConsentStatusOut(required=required, accepted=accepted, missing=missing, complete=not missing)


@router.get("/consents/status", response_model=S.ConsentStatusOut)
def get_status(ctx: AuthCtx = Depends(current_user), svc: Services = Depends(get_services)) -> S.ConsentStatusOut:
    with svc.db.begin() as conn:
        return consent_status(conn, svc, ctx.user_id)


@router.post("/consents", response_model=S.ConsentStatusOut, dependencies=[user_limit("consents", 20, 60)])
def post_consents(body: S.ConsentBatchIn, ctx: AuthCtx = Depends(current_user),
                  svc: Services = Depends(get_services)) -> S.ConsentStatusOut:
    cfg, settings = svc.config, svc.settings
    recorded: list[dict[str, Any]] = []
    with svc.db.begin() as conn:
        for item in body.consents:
            current = cfg.legal_versions.get(item.doc)
            if item.doc_version != current:
                raise Conflict("this document has been updated; please review the current version",
                               doc=item.doc, current_version=current)
            canonical: Optional[str] = cfg.legal_doc_hashes.get(item.doc)
            if canonical and item.doc_text_sha256 and item.doc_text_sha256 != canonical:
                raise Conflict("the document text you accepted differs from the current text; please reload",
                               doc=item.doc)
            text_hash = item.doc_text_sha256 or canonical
            if not text_hash:
                raise ValidationFailed("doc_text_sha256 is required", doc=item.doc)
            if item.doc == "creator_agreement" and not settings.feature_creator_uploads:
                raise Forbidden("creator uploads are disabled")
            strategy_id = str(item.strategy_id) if item.strategy_id else None
            if strategy_id:
                st = svc.store.get_strategy(conn, strategy_id)
                if st is None or st["status"] not in ("listed", "paused"):
                    raise NotFound("strategy not found")
            if item.doc == "jurisdiction" and item.country:
                if item.country in settings.restricted_countries:
                    raise JurisdictionBlocked("service not available in your jurisdiction")
                svc.store.set_country_attested(conn, ctx.user_id, item.country)
            svc.store.insert_consent(conn, user_id=ctx.user_id, doc=item.doc, version=item.doc_version,
                                     doc_text_sha256=text_hash, context=item.context, strategy_id=strategy_id,
                                     ip_hash=ctx.ip_hash, ua_hash=ctx.ua_hash)
            client_ts = None
            if item.accepted_at:
                try:
                    client_ts = v.parse_issued_at(item.accepted_at).isoformat()
                except v.InputError:
                    client_ts = None
            recorded.append({"doc": item.doc, "version": item.doc_version, "context": item.context,
                             "strategy_id": strategy_id, "sha256": text_hash, "client_accepted_at": client_ts})
        if any(r["doc"] == "creator_agreement" for r in recorded) and ctx.role == "user":
            svc.store.set_role(conn, ctx.user_id, "creator")
            svc.audit.write(conn, actor=ctx.actor, action="role.creator", target=f"user:{ctx.user_id}",
                            payload={}, ip_hash=ctx.ip_hash)
        svc.audit.write(conn, actor=ctx.actor, action="consent.accept", target=f"user:{ctx.user_id}",
                        payload={"consents": recorded, "edge_country": ctx.country}, ip_hash=ctx.ip_hash)
        return consent_status(conn, svc, ctx.user_id)
