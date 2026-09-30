"""Signing-trust read endpoints (docs/security/REVIEW_WEB_INFRA.md H1). API service.

GET /v1/agents/{agent_id}/attestation            (the owner of the agent)
    The executor's KMS attestation of the agent address (``agent_keys.attestation_*``, written only by the executor
    job /v1/internal/attest-agents; migrations/0013). ``attestation`` is null until the job has run (≤ ~1 minute
    after POST /v1/agents): the web client polls, verifies the signature against the public key PINNED in
    app-config.json, and only then asks the wallet to sign ApproveAgent. The api can read but never write or forge it.

GET /v1/admin/payouts/{kind}/{payout_id}/wallet-proof     (admin)
    The beneficiary's latest EIP-4361 ownership proof (message + personal_sign signature) of the payout / withdrawal
    destination (``wallets.proof_*``). The admin's browser recovers the signer itself and refuses to approve or sign
    unless it is the destination. 404 when no proof is on file.

``record_wallet_proof`` is called by POST /v1/wallets/verify after the server-side SIWE checks passed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import AuthCtx, Services, admin_user, consented_user, get_services, user_limit
from app.api.schemas import Out
from app.errors import NotFound

router = APIRouter(tags=["trust"])

__all__ = ["router", "record_wallet_proof", "AgentAttestationOut", "WalletProofOut"]


def _rows(conn: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    from app.jobs_data import _db

    return _db.rows(conn, sql, **params)


class AttestationOut(Out):
    signature_b64: str
    key_version: str
    attested_at: datetime


class AgentAttestationOut(Out):
    agent_id: UUID
    user_id: UUID
    agent_address: str
    status: str
    attestation: Optional[AttestationOut] = None


class WalletProofOut(Out):
    user_id: UUID
    address: str
    message: str
    signature: str
    recorded_at: Optional[datetime] = None


@router.get("/agents/{agent_id}/attestation", response_model=AgentAttestationOut,
            dependencies=[user_limit("agent_attestation", 120, 3600)])
def agent_attestation(agent_id: UUID, ctx: AuthCtx = Depends(consented_user),
                      svc: Services = Depends(get_services)) -> AgentAttestationOut:
    with svc.db.begin() as conn:
        rows = _rows(conn, """
            SELECT id::text AS id, user_id::text AS user_id, agent_address, status::text AS status,
                   attestation_sig, attestation_key_version, attested_at
              FROM agent_keys WHERE id = CAST(:id AS uuid) AND user_id = CAST(:u AS uuid)""",
                     id=str(agent_id), u=ctx.user_id)
    if not rows:
        raise NotFound("agent not found")
    r = rows[0]
    att = None
    if r.get("attestation_sig") and r.get("attested_at"):
        att = AttestationOut(signature_b64=str(r["attestation_sig"]), key_version=str(r["attestation_key_version"]),
                             attested_at=r["attested_at"])
    return AgentAttestationOut(agent_id=r["id"], user_id=r["user_id"], agent_address=str(r["agent_address"]).lower(),
                               status=str(r["status"]), attestation=att)


_PAYOUT_TABLES = {"payout": "payouts", "withdrawal": "withdrawals"}


@router.get("/admin/payouts/{kind}/{payout_id}/wallet-proof", response_model=WalletProofOut)
def payout_wallet_proof(kind: Literal["payout", "withdrawal"], payout_id: UUID, ctx: AuthCtx = Depends(admin_user),
                        svc: Services = Depends(get_services)) -> WalletProofOut:
    table = _PAYOUT_TABLES[kind]   # literal allowlist, never user text
    with svc.db.begin() as conn:
        p = _rows(conn, f"""SELECT beneficiary_user_id::text AS user_id, to_address FROM {table}
                             WHERE id = CAST(:id AS uuid)""", id=str(payout_id))
        if not p:
            raise NotFound(f"{kind} not found")
        w = _rows(conn, """
            SELECT user_id::text AS user_id, master_address, proof_message, proof_signature, proof_recorded_at
              FROM wallets
             WHERE user_id = CAST(:u AS uuid) AND lower(master_address) = lower(:a)
               AND verified_at IS NOT NULL AND proof_message IS NOT NULL""", u=p[0]["user_id"], a=p[0]["to_address"])
    if not w:
        raise NotFound("no wallet-ownership proof on file for this destination")
    r = w[0]
    return WalletProofOut(user_id=r["user_id"], address=str(r["master_address"]).lower(), message=str(r["proof_message"]),
                          signature=str(r["proof_signature"]), recorded_at=r.get("proof_recorded_at"))


def record_wallet_proof(conn: Any, *, user_id: str, address: str, message: str, signature: str,
                        now: datetime) -> None:
    """Store the latest verified SIWE proof of (user, wallet). Called after POST /wallets/verify's checks passed
    (domain, URI, nonce, freshness, recovered signer == address)."""
    _rows(conn, """
        UPDATE wallets SET proof_message = :m, proof_signature = :s, proof_recorded_at = CAST(:t AS timestamptz)
         WHERE user_id = CAST(:u AS uuid) AND lower(master_address) = lower(:a)
        RETURNING id""", m=message[:2000], s=signature, t=now, u=user_id, a=address)
