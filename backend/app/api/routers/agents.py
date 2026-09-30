"""Agent wallet + builder-fee approval (SPEC §0, §5.3, §6).

POST /agents (step-up): generate a secp256k1 agent key and seal it with the ENCRYPT-ONLY KMS envelope
(app.security.agent_keys.generate_sealed_agent_key; AAD binds user id + agent address). The plaintext never leaves
that call; the API process cannot decrypt (no decryptor is ever built here; its KMS role lacks decrypt and its DB
role cannot even SELECT key_ciphertext). Returns the agent address and the EIP-712 payloads the user's wallet
signs; the BROWSER posts them to Hyperliquid /exchange — the API never relays user signatures.
POST /agents/{id}/confirm (step-up): verified on-chain via info.extraAgents → status active.
POST /builder-approval/confirm: verified on-chain via info.maxBuilderFee → builder_approvals row.

One live agent per master address (DB partial unique index). Rotation (rotate=true) marks the current active
agent 'rotated' immediately: the replacement shares the agent NAME, so the user's approval replaces it on-chain
and trading on that master pauses until the new agent is confirmed.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, get_services, step_up_user, user_limit
from app.api.user_alerts import builder_approval_missing
from app.errors import Conflict, ExternalServiceError, Forbidden, NotFound, ValidationFailed

router = APIRouter(tags=["agents"])

HL_EXCHANGE_URL_SUFFIX = "/exchange"


def _agent_out(row: dict) -> S.AgentOut:
    return S.AgentOut(id=row["id"], master_address=row["master_address"], agent_address=row["agent_address"],
                      agent_name=row["agent_name"], status=row["status"], approved_at=row.get("approved_at"),
                      created_at=row["created_at"])


@router.get("/agents", response_model=list[S.AgentOut])
def list_agents(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> list[S.AgentOut]:
    with svc.db.begin() as conn:
        return [_agent_out(r) for r in svc.store.list_agents(conn, ctx.user_id)]


@router.post("/agents", response_model=S.AgentCreateOut, status_code=201,
             dependencies=[user_limit("agents_create", 5, 3600)])
def create_agent(body: S.AgentCreateIn, ctx: AuthCtx = Depends(step_up_user),
                 svc: Services = Depends(get_services)) -> S.AgentCreateOut:
    s = svc.settings
    master = body.master_address
    with svc.db.begin() as conn:
        if svc.store.verified_wallet(conn, ctx.user_id, master) is None:
            raise Forbidden("verify ownership of this wallet first")
    # KMS encrypt (network) outside the DB transaction; the plaintext key is zeroised inside the helper.
    sealed = svc.agent_keys.generate_sealed(ctx.user_id)
    with svc.db.begin() as conn:
        svc.store.lock_user(conn, ctx.user_id)
        rotated: Optional[str] = None
        for live in svc.store.live_agents_for_master(conn, master):
            if str(live["user_id"]) != ctx.user_id:
                raise Conflict("this wallet is linked to another account")
            if live["status"] == "active":
                if not body.rotate:
                    raise Conflict("an active agent already exists for this wallet; pass rotate=true to replace it")
                svc.store.set_agent_status(conn, str(live["id"]), "rotated", svc.now())
                rotated = str(live["id"])
            else:  # stale pending approval: superseded by the new key
                svc.store.set_agent_status(conn, str(live["id"]), "revoked", svc.now())
        row = svc.store.insert_agent(conn, user_id=ctx.user_id, master=master, agent_address=sealed.agent_address,
                                     agent_name=s.agent_name, key_ciphertext=sealed.key_ciphertext,
                                     kms_key_version=sealed.kms_key_version)
        svc.audit.write(conn, actor=ctx.actor, action="agent.create", target=f"agent:{row['id']}",
                        payload={"master": master, "agent_address": sealed.agent_address, "rotated": rotated,
                                 "kms_key_version": sealed.kms_key_version}, ip_hash=ctx.ip_hash)
    nonce = int(svc.now().timestamp() * 1000)
    approve = svc.typed_data.approve_agent(agent_address=row["agent_address"], agent_name=s.agent_name, nonce=nonce,
                                           signature_chain_id=body.signature_chain_id)
    builder = None
    if s.builder_address:
        builder = svc.typed_data.approve_builder_fee(builder=s.builder_address,
                                                     max_fee_tenths_bp=s.economics.builder_fee_tenths_bp,
                                                     nonce=nonce + 1, signature_chain_id=body.signature_chain_id)
    return S.AgentCreateOut(agent=_agent_out(row), approve_agent=approve, approve_builder_fee=builder,
                            exchange_url=s.hl_api_url.rstrip("/") + HL_EXCHANGE_URL_SUFFIX,
                            required_builder_fee_tenths_bp=s.economics.builder_fee_tenths_bp)


@router.post("/agents/{agent_id}/confirm", response_model=S.AgentOut,
             dependencies=[user_limit("agents_confirm", 30, 3600)])
def confirm_agent(agent_id: UUID, ctx: AuthCtx = Depends(step_up_user),
                  svc: Services = Depends(get_services)) -> S.AgentOut:
    with svc.db.begin() as conn:
        agent = svc.store.get_agent(conn, str(agent_id), ctx.user_id)
    if agent is None:
        raise NotFound("agent not found")
    if agent["status"] == "active":
        return _agent_out(agent)
    if agent["status"] != "pending_approval":
        raise Conflict("this agent was replaced or revoked; create a new one")
    on_chain = svc.hl.extra_agents(agent["master_address"])   # agents live on the MASTER account
    now_ms = int(svc.now().timestamp() * 1000)
    match = next((a for a in on_chain if a["address"] == agent["agent_address"]), None)
    if match is None:
        raise ValidationFailed("approval not found on Hyperliquid yet; sign the approval in your wallet and retry")
    if match.get("name") and not str(match["name"]).startswith(agent["agent_name"]):
        raise ValidationFailed("the on-chain agent has an unexpected name")
    valid_until = match.get("validUntil")
    if isinstance(valid_until, int) and valid_until <= now_ms + 86_400_000:
        raise ValidationFailed("the on-chain approval expires within 24 h; approve again without an expiry")
    with svc.db.begin() as conn:
        agent = svc.store.get_agent(conn, str(agent_id), ctx.user_id, for_update=True)
        if agent is None or agent["status"] != "pending_approval":
            raise Conflict("agent state changed; reload")
        svc.store.set_agent_status(conn, str(agent_id), "active", svc.now())
        svc.audit.write(conn, actor=ctx.actor, action="agent.activate", target=f"agent:{agent_id}",
                        payload={"master": agent["master_address"], "agent_address": agent["agent_address"]},
                        ip_hash=ctx.ip_hash)
        agent = svc.store.get_agent(conn, str(agent_id), ctx.user_id)
    _check_builder_after_confirm(svc, ctx, agent["master_address"])
    return _agent_out(agent)


def _check_builder_after_confirm(svc: Services, ctx: AuthCtx, master: str) -> None:
    """Best effort (the agent is already active): an agent without a sufficient builder-fee approval cannot trade
    (every order carries our builder code) → mandatory ``builder_approval_missing`` alert. A Hyperliquid outage here
    never fails the confirmation; the daily agent-expiry scan re-checks."""
    s = svc.settings
    if not s.builder_address:
        return
    try:
        rate = svc.hl.max_builder_fee(master, s.builder_address)
    except ExternalServiceError:
        return
    with svc.db.begin() as conn:
        builder_approval_missing(conn, svc, user_id=ctx.user_id, master=master, approved_tenths_bp=rate,
                                 required_tenths_bp=s.economics.builder_fee_tenths_bp, where="agent_confirm",
                                 now=svc.now())


@router.post("/builder-approval/confirm", response_model=S.BuilderApprovalOut,
             dependencies=[user_limit("builder_confirm", 30, 3600)])
def confirm_builder(body: S.BuilderApprovalConfirmIn, ctx: AuthCtx = Depends(consented_user),
                    svc: Services = Depends(get_services)) -> S.BuilderApprovalOut:
    s = svc.settings
    if not s.builder_address:
        raise ExternalServiceError("builder address not configured")
    with svc.db.begin() as conn:
        if svc.store.verified_wallet(conn, ctx.user_id, body.master_address) is None:
            raise Forbidden("verify ownership of this wallet first")
    rate = svc.hl.max_builder_fee(body.master_address, s.builder_address)
    required = s.economics.builder_fee_tenths_bp
    with svc.db.begin() as conn:
        row = svc.store.insert_builder_approval(conn, user_id=ctx.user_id, master=body.master_address, rate=rate,
                                                now=svc.now())
        builder_approval_missing(conn, svc, user_id=ctx.user_id, master=body.master_address, approved_tenths_bp=rate,
                                 required_tenths_bp=required, where="builder_confirm", now=svc.now())
        svc.audit.write(conn, actor=ctx.actor, action="builder_fee.confirm", target=f"wallet:{body.master_address}",
                        payload={"max_fee_rate_tenths_bp": rate, "required": required}, ip_hash=ctx.ip_hash)
    return S.BuilderApprovalOut(master_address=row["master_address"], max_fee_rate_tenths_bp=rate,
                                required_tenths_bp=required, sufficient=rate >= required,
                                verified_on_chain_at=row["verified_on_chain_at"])
