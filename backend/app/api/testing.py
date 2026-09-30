"""In-memory fakes of the API ports for tests (TEST-ONLY; never imported by production code).

`FakeWorld` holds every table as plain dicts. `FakeDatabase.begin()` snapshots the world and restores it when the
block raises — so transaction semantics (e.g. an Idempotency-Key claim rolled back together with a failed money
movement) behave like Postgres. Only the store methods the API tests exercise are implemented; any other method
raises AttributeError loudly.

    world = FakeWorld()
    svc = make_services(world, settings=make_settings())
    app = create_app(svc)
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator, Optional

from app.api.deps import Services
from app.config import Settings, get_settings
from app.errors import AppError, Conflict, InsufficientBalance, Unauthorized, ValidationFailed

UTC = timezone.utc
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _id() -> str:
    return str(uuid.uuid4())


def _seed_trusted_dexes() -> dict[str, dict]:
    from app.strategies.dexes import LAUNCH_TRUSTED_DEXES
    return {d: {"dex": d, "created_at": T0, "updated_at": T0, "added_by": "system:migration_0012", "reason": "seed",
                "removed_at": None, "removed_by": None, "removal_reason": None, "active": True}
            for d in ("",) + LAUNCH_TRUSTED_DEXES}


@dataclass
class FakeWorld:
    now: datetime = T0
    users: dict[str, dict] = field(default_factory=dict)
    consents: list[dict] = field(default_factory=list)
    wallets: dict[str, dict] = field(default_factory=dict)          # address -> row
    nonces: dict[str, dict] = field(default_factory=dict)
    agents: dict[str, dict] = field(default_factory=dict)
    keygen_calls: int = 0
    strategies: dict[str, dict] = field(default_factory=dict)
    versions: dict[str, dict] = field(default_factory=dict)
    subscriptions: dict[str, dict] = field(default_factory=dict)
    idem: dict[tuple[str, str], dict] = field(default_factory=dict)
    ledger_tx: dict[str, dict] = field(default_factory=dict)        # key -> {id, kind, entries}
    balances: dict[str, int] = field(default_factory=dict)          # code -> raw Σ amount
    accounts: dict[str, str] = field(default_factory=dict)          # code -> id
    deposits: dict[str, dict] = field(default_factory=dict)         # external_ref -> row
    withdrawals: dict[str, dict] = field(default_factory=dict)
    payouts: dict[str, dict] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    flags: dict[str, dict] = field(default_factory=dict)
    changes: dict[str, dict] = field(default_factory=dict)
    login_countries: set[tuple[str, str]] = field(default_factory=set)
    devices: set[tuple[str, str]] = field(default_factory=set)        # (user_id, device_hash) — 0008 user_devices
    kyc: dict[str, dict] = field(default_factory=dict)
    jobs_run: list[tuple[str, dict]] = field(default_factory=list)
    alert_contacts_missing: set[str] = field(default_factory=set)   # user ids WITHOUT Telegram+email (0007)
    trusted_dexes: dict[str, dict] = field(default_factory=lambda: _seed_trusted_dexes())   # 0012 allowlist
    deposit_scan_requests: dict[str, dict] = field(default_factory=dict)                     # 0012 user_id -> row

    # ------------------------------------------------------------------ seeding helpers
    def add_user(self, uid: str = "fb-user", *, role: str = "user", plan: str = "free", email: str = "u@example.com",
                 status: str = "active", referral_code: Optional[str] = None, consents: bool = True,
                 legal_versions: Optional[dict[str, str]] = None) -> dict:
        row = {"id": _id(), "created_at": self.now, "firebase_uid": uid, "email": email, "display_name": None,
               "role": role, "plan": plan, "plan_period_end": None, "country_attested": None,
               "referral_code": referral_code or uid.upper().replace("-", "")[:8].ljust(8, "X"), "referred_by": None,
               "referral_tier": "starter", "status": status, "mfa_enrolled": True, "device_fp_hash": None}
        self.users[row["id"]] = row
        if consents:
            from app.api.deps import DEFAULT_LEGAL_VERSIONS, SITE_DOCS
            versions = legal_versions or DEFAULT_LEGAL_VERSIONS
            for d in SITE_DOCS:
                self.consents.append({"user_id": row["id"], "doc": d, "doc_version": versions[d],
                                      "doc_text_sha256": "0" * 64, "context": "site_entry", "strategy_id": None,
                                      "accepted_at": self.now})
        return row

    def add_wallet(self, user_id: str, address: str, *, verified_at: Optional[datetime] = None) -> None:
        """Verified a week ago by default (older than the 48 h payout-address hold, REVIEW_AUTH_API F5)."""
        at = verified_at or (self.now - timedelta(days=7))
        self.wallets[address] = {"user_id": user_id, "address": address, "verified_at": at, "created_at": at}

    def add_agent(self, user_id: str, master: str, *, status: str = "active") -> dict:
        row = {"id": _id(), "created_at": self.now, "user_id": user_id, "master_address": master,
               "agent_address": "0x" + hashlib.sha256(master.encode()).hexdigest()[:40], "agent_name": "aijalon",
               "status": status, "approved_at": self.now if status == "active" else None, "revoked_at": None,
               "keygen_at": self.now, "attestation_sig": None, "attestation_key_version": None, "attested_at": None,
               "attestation_failed_at": None}
        self.agents[row["id"]] = row
        return row

    def executor_keygen(self, agent_id: str, *, attestation_sig: Optional[str] = "QUJD" * 24) -> dict:
        """What the EXECUTOR does for a request (app.execution.trust_jobs.generate_agents): address + attestation,
        status requested → pending_approval. The api has no code path that can do this."""
        a = self.agents[agent_id]
        assert a["status"] == "requested" and a["agent_address"] is None
        self.keygen_calls += 1
        a.update(agent_address="0x" + hashlib.sha256(f"agent{agent_id}".encode()).hexdigest()[:40],
                 status="pending_approval", keygen_at=self.now, attestation_sig=attestation_sig,
                 attestation_key_version="local-dev:test" if attestation_sig else None,
                 attested_at=self.now if attestation_sig else None)
        return a

    def add_strategy(self, slug: str = "silver", *, price: int = 0, profit_share_bps: int = 0, status: str = "listed",
                     in_house: bool = True, owner: Optional[str] = None, markets: tuple[str, ...] = ("xyz:SILVER",),
                     max_leverage: Optional[int] = 2) -> tuple[dict, dict]:
        st = {"id": _id(), "created_at": self.now, "slug": slug, "name": slug.title(), "owner_user_id": owner,
              "in_house": in_house, "markets": list(markets), "timeframe": "1d", "price_monthly_micro": price,
              "profit_share_bps": profit_share_bps, "status": status, "description": None}
        self.strategies[st["id"]] = st
        ver = {"id": _id(), "created_at": self.now, "strategy_id": st["id"], "version": 1, "code_hash": "ab" * 32,
               "params": {}, "markets": list(markets), "timeframe": "1d", "lookback": 300, "max_leverage": max_leverage,
               "published_at": self.now, "backtest": None, "live_since": self.now}
        self.versions[ver["id"]] = ver
        return st, ver

    def credit(self, user_id: str, micro: int, *, withdrawable: bool = True) -> None:
        """Seed a fee-balance credit (as if a deposit was credited)."""
        code = f"user:{user_id}:fee_balance"
        self.balances[code] = self.balances.get(code, 0) - micro
        ref = "0x" + hashlib.sha256(f"{user_id}{len(self.deposits)}".encode()).hexdigest()
        self.deposits[ref] = {"id": _id(), "created_at": self.now, "user_id": user_id,
                              "method": "usdc_hl" if withdrawable else "stripe", "external_ref": ref,
                              "amount_micro": micro, "status": "credited", "withdrawable": withdrawable}

    def fee_balance(self, user_id: str) -> int:
        return -self.balances.get(f"user:{user_id}:fee_balance", 0)


# ============================================================================================ database
class FakeConn:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world


class FakeDatabase:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    @contextmanager
    def begin(self) -> Iterator[FakeConn]:
        snapshot = copy.deepcopy(self.world.__dict__)
        try:
            yield FakeConn(self.world)
        except BaseException:
            self.world.__dict__.clear()
            self.world.__dict__.update(snapshot)
            raise


# ============================================================================================ store
_LIVE = ("pending", "active", "past_due", "reduce_only", "paused_user", "closing")


from app.api.testing_security import FakeSecurityStoreMixin  # noqa: E402  (security-fix round store fakes)
from app.api.testing_cleanup import FakeCleanupStoreMixin  # noqa: E402  (clean-up round store fakes)


class FakeStore(FakeSecurityStoreMixin, FakeCleanupStoreMixin):
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    @contextmanager
    def savepoint(self, conn: Any) -> Iterator[None]:
        snapshot = copy.deepcopy(self.w.__dict__)
        try:
            yield
        except BaseException:
            self.w.__dict__.clear()
            self.w.__dict__.update(snapshot)
            raise

    # idempotency
    def idem_claim(self, conn, *, user_id, key, scope, fingerprint):
        k = (user_id, key)
        if k in self.w.idem:
            return dict(self.w.idem[k])
        self.w.idem[k] = {"scope": scope, "fingerprint": fingerprint, "status_code": None, "response": None}
        return None

    def idem_complete(self, conn, *, user_id, key, status_code, response):
        self.w.idem[(user_id, key)].update(status_code=status_code, response=json.loads(json.dumps(response)))

    # users
    def get_user_by_firebase_uid(self, conn, uid):
        return next((dict(u) for u in self.w.users.values() if u["firebase_uid"] == uid), None)

    def get_user(self, conn, user_id, *, for_update=False):
        u = self.w.users.get(str(user_id))
        return dict(u) if u else None

    def lock_user(self, conn, user_id):
        return None

    def get_user_by_referral_code(self, conn, code):
        return next((dict(u) for u in self.w.users.values() if u["referral_code"] == code), None)

    def create_user(self, conn, *, firebase_uid, email, display_name, referral_code, referred_by, mfa_enrolled):
        existing = self.get_user_by_firebase_uid(conn, firebase_uid)
        if existing:
            existing["_created"] = False
            return existing
        if any(u["referral_code"] == referral_code for u in self.w.users.values()):
            return None
        row = {"id": _id(), "created_at": self.w.now, "firebase_uid": firebase_uid, "email": email,
               "display_name": display_name, "role": "user", "plan": "free", "plan_period_end": None,
               "country_attested": None, "referral_code": referral_code, "referred_by": referred_by,
               "referral_tier": "starter", "status": "active", "mfa_enrolled": mfa_enrolled, "device_fp_hash": None}
        self.w.users[row["id"]] = row
        return {**row, "_created": True}

    def update_display_name(self, conn, user_id, name):
        self.w.users[user_id]["display_name"] = name

    def set_country_attested(self, conn, user_id, country):
        self.w.users[user_id]["country_attested"] = country

    def set_role(self, conn, user_id, role):
        self.w.users[user_id]["role"] = role

    def set_plan(self, conn, user_id, plan, period_end):
        self.w.users[user_id].update(plan=plan, plan_period_end=period_end)

    def set_user_status(self, conn, user_id, status):
        if user_id not in self.w.users:
            return 0
        self.w.users[user_id]["status"] = status
        return 1

    def record_login_country(self, conn, user_id, country):
        seen = {c for (u, c) in self.w.login_countries if u == user_id}
        new = country not in seen
        self.w.login_countries.add((user_id, country))
        return new and bool(seen)

    def record_device(self, conn, user_id, device_hash, label):
        seen = {d for (u, d) in self.w.devices if u == user_id}
        new = device_hash not in seen
        self.w.devices.add((user_id, device_hash))
        return new and bool(seen)

    def set_mfa_factor_hash(self, conn, user_id, factor_hash):
        self.w.users[user_id]["mfa_factor_hash"] = factor_hash

    def bind_referrer(self, conn, *, user_id, referrer_id):
        u = self.w.users[user_id]
        if u["referred_by"] or user_id == referrer_id:
            return False
        u["referred_by"] = referrer_id
        return True

    # consents
    def accepted_consents(self, conn, user_id):
        out: dict[str, str] = {}
        for c in sorted(self.w.consents, key=lambda c: c["accepted_at"]):
            if c["user_id"] == user_id and c["strategy_id"] is None:
                out[c["doc"]] = c["doc_version"]
        return out

    def insert_consent(self, conn, *, user_id, doc, version, doc_text_sha256, context, strategy_id, ip_hash, ua_hash):
        self.w.consents.append({"user_id": user_id, "doc": doc, "doc_version": version,
                                "doc_text_sha256": doc_text_sha256, "context": context, "strategy_id": strategy_id,
                                "accepted_at": self.w.now, "ip_hash": ip_hash})

    def recent_subscription_ack(self, conn, *, user_id, strategy_id, version, since):
        return next((c for c in reversed(self.w.consents) if c["user_id"] == user_id and c["doc"] == "subscription_ack"
                     and c["strategy_id"] == strategy_id and c["doc_version"] == version and c["accepted_at"] >= since),
                    None)

    # wallets
    def list_wallets(self, conn, user_id):
        return [{"address": w["address"], "verified_at": w["verified_at"]} for w in self.w.wallets.values()
                if w["user_id"] == user_id]

    def verified_wallet(self, conn, user_id, address):
        w = self.w.wallets.get(address)
        return dict(w) if w and w["user_id"] == user_id and w["verified_at"] else None

    def upsert_verified_wallet(self, conn, user_id, address, now):
        w = self.w.wallets.get(address)
        if w and w["user_id"] != user_id:
            return dict(w)
        self.w.wallets[address] = {"user_id": user_id, "address": address, "verified_at": now, "created_at": now}
        return dict(self.w.wallets[address])

    def user_for_verified_wallet(self, conn, address):
        w = self.w.wallets.get(address)
        return w["user_id"] if w and w["verified_at"] else None

    def create_wallet_nonce(self, conn, *, user_id, nonce, expires_at):
        self.w.nonces[nonce] = {"user_id": user_id, "expires_at": expires_at, "used_at": None}

    def consume_wallet_nonce(self, conn, *, user_id, nonce, now):
        n = self.w.nonces.get(nonce)
        if not n or n["user_id"] != user_id or n["used_at"] or n["expires_at"] <= now:
            return False
        n["used_at"] = now
        return True

    # agents
    def list_agents(self, conn, user_id):
        return [dict(a) for a in self.w.agents.values() if a["user_id"] == user_id]

    def get_agent(self, conn, agent_id, user_id, *, for_update=False):
        a = self.w.agents.get(agent_id)
        return dict(a) if a and a["user_id"] == user_id else None

    def live_agents_for_master(self, conn, master):
        return [dict(a) for a in self.w.agents.values()
                if a["master_address"] == master and a["status"] in ("requested", "pending_approval", "active")]

    def get_agent_detail(self, conn, agent_id, user_id):
        return self.get_agent(conn, agent_id, user_id)

    def active_agent_for_master(self, conn, user_id, master):
        return next((dict(a) for a in self.w.agents.values() if a["master_address"] == master
                     and a["user_id"] == user_id and a["status"] == "active"), None)

    def set_agent_status(self, conn, agent_id, status, now):
        self.w.agents[agent_id]["status"] = status

    def insert_agent_request(self, conn, *, user_id, master, agent_name):
        if self.live_agents_for_master(conn, master):
            raise AssertionError("unique index agent_keys_one_live_per_master violated")
        row = {"id": _id(), "created_at": self.w.now, "user_id": user_id, "master_address": master,
               "agent_address": None, "agent_name": agent_name, "status": "requested",
               "approved_at": None, "revoked_at": None, "keygen_at": None, "attestation_sig": None,
               "attestation_key_version": None, "attested_at": None, "attestation_failed_at": None}
        self.w.agents[row["id"]] = row
        return dict(row)

    def get_kyc(self, conn, user_id):
        k = self.w.kyc.get(str(user_id))
        return dict(k) if k else None

    def set_kyc_status(self, conn, user_id, status):
        k = self.w.kyc.get(str(user_id))
        if k is None:
            return 0
        k["status"] = status
        return 1

    # strategies
    def get_strategy(self, conn, strategy_id, *, for_update=False):
        st = self.w.strategies.get(str(strategy_id))
        return dict(st) if st else None

    def current_versions(self, conn, strategy_ids):
        out = {}
        for v in self.w.versions.values():
            sid = str(v["strategy_id"])
            if sid in strategy_ids and v["published_at"] and (sid not in out or v["version"] > out[sid]["version"]):
                out[sid] = dict(v)
        return out

    def get_version(self, conn, version_id):
        v = self.w.versions.get(version_id)
        return dict(v) if v else None

    # subscriptions
    def _subs(self, user_id=None):
        return [s for s in self.w.subscriptions.values() if user_id is None or s["user_id"] == user_id]

    def count_live_subscriptions(self, conn, user_id):
        return sum(1 for s in self._subs(user_id) if s["status"] in _LIVE)

    def live_subscription_prices(self, conn, user_id):
        return [int(self.w.strategies[s["strategy_id"]]["price_monthly_micro"] or 0) for s in self._subs(user_id)
                if s["status"] in ("pending", "active", "past_due", "reduce_only")]

    def total_live_allocation(self, conn, user_id=None, *, exclude_subscription_id=None):
        return sum(s["allocation_micro"] for s in self._subs(user_id)
                   if s["status"] in _LIVE and s["id"] != exclude_subscription_id)

    def live_subscription_on_address(self, conn, address):
        return next((dict(s) for s in self._subs() if s["trading_address"] == address
                     and s["status"] in ("pending", "active", "past_due", "reduce_only", "closing")), None)

    def trading_addresses(self, conn, user_id):
        subs = {s["trading_address"] for s in self._subs(user_id) if s["status"] in _LIVE}
        wallets = {a for a, w in self.w.wallets.items() if w["user_id"] == user_id and w["verified_at"]}
        return sorted(subs | wallets)

    def _with_strategy(self, s):
        st = self.w.strategies[s["strategy_id"]]
        return {**s, "strategy_slug": st["slug"], "strategy_name": st["name"], "strategy_markets": st["markets"]}

    def insert_subscription(self, conn, *, user_id, strategy_id, version_id, trading_address, master_address,
                            allocation_micro, max_leverage_x100, status, current_period_end, price_monthly_micro=None,
                            profit_share_bps=None):
        if self.live_subscription_on_address(conn, trading_address):
            raise Conflict("already exists")
        row = {"id": _id(), "created_at": self.w.now, "user_id": user_id, "strategy_id": strategy_id,
               "strategy_version_id": version_id, "trading_address": trading_address, "master_address": master_address,
               "allocation_micro": allocation_micro, "max_leverage_x100": max_leverage_x100, "status": status,
               "current_period_end": current_period_end, "hwm_micro": 0, "cum_pnl_micro": 0,
               "cancel_positions": None, "cancelled_at": None, "past_due_since": None,
               "price_monthly_micro": price_monthly_micro, "profit_share_bps": profit_share_bps}
        self.w.subscriptions[row["id"]] = row
        return self._with_strategy(row)

    def get_subscription(self, conn, sub_id, user_id, *, for_update=False):
        s = self.w.subscriptions.get(sub_id)
        return self._with_strategy(s) if s and s["user_id"] == user_id else None

    def update_subscription(self, conn, sub_id, *, allocation_micro, max_leverage_x100, status):
        s = self.w.subscriptions[sub_id]
        if allocation_micro is not None:
            s["allocation_micro"] = allocation_micro
        if max_leverage_x100 is not None:
            s["max_leverage_x100"] = max_leverage_x100
        if status is not None:
            s["status"] = status

    def end_subscription(self, conn, sub_id, *, positions, now):
        s = self.w.subscriptions[sub_id]
        if s["status"] == "cancelled":
            return None
        s["cancel_positions"] = positions
        s["status"] = "closing" if positions == "close" else "cancelled"
        if positions == "leave":
            s["cancelled_at"] = now
        return {"id": sub_id, "status": s["status"]}

    def list_subscriptions(self, conn, user_id, limit, cursor):
        rows = sorted(self._subs(user_id), key=lambda s: (s["created_at"], s["id"]), reverse=True)
        return [self._with_strategy(s) for s in rows[: limit + 1]]

    # ledger reads
    def account_id(self, conn, code):
        return self.w.accounts.get(code)

    def account_code_by_id(self, conn, account_id):
        return next((c for c, i in self.w.accounts.items() if i == account_id), None)

    def total_credited(self, conn, code):
        return sum(-a for tx in self.w.ledger_tx.values() for c, a in tx["entries"] if c == code and a < 0)

    # deposits
    def insert_pending_deposit(self, conn, *, user_id, method, external_ref, amount_micro, currency="USD",
                               amount_minor=None):
        self.w.deposits.setdefault(external_ref, {"id": _id(), "created_at": self.w.now, "user_id": user_id,
                                                  "method": method, "external_ref": external_ref,
                                                  "amount_micro": amount_micro, "status": "pending",
                                                  "withdrawable": False})

    def mark_deposit_credited(self, conn, *, user_id, method, external_ref, amount_micro, tx_id, withdrawable,
                              fee_micro=0, currency=None, amount_minor=None, meta=None):
        row = self.w.deposits.get(external_ref)
        if row is None or row["status"] != "credited":
            row = {"id": row["id"] if row else _id(), "created_at": self.w.now, "user_id": user_id, "method": method,
                   "external_ref": external_ref, "amount_micro": amount_micro, "status": "credited",
                   "withdrawable": withdrawable}
            self.w.deposits[external_ref] = row
        return dict(row)

    def withdrawable_usdc(self, conn, user_id):
        inflow = sum(d["amount_micro"] for d in self.w.deposits.values()
                     if d["user_id"] == user_id and d["withdrawable"] and d["status"] == "credited")
        out = sum(w["amount_micro"] for w in self.w.withdrawals.values()
                  if w["beneficiary"] == user_id and w["status"] != "rejected")
        return max(0, inflow - out)

    # withdrawals / payouts
    def _table(self, kind):
        return self.w.withdrawals if kind == "withdrawal" else self.w.payouts

    def pending_withdrawals_total(self, conn, user_id):
        return sum(w["amount_micro"] for w in self.w.withdrawals.values()
                   if w["beneficiary"] == user_id and w["status"] in ("requested", "approved_1", "approved_2"))

    def insert_withdrawal(self, conn, *, user_id, amount_micro, to_address):
        row = {"id": _id(), "created_at": self.w.now, "kind": "withdrawal", "beneficiary": user_id,
               "amount_micro": amount_micro, "to_address": to_address, "status": "requested", "maker_admin": None,
               "checker_admin": None, "tx_hash": None, "ledger_account_id": None}
        self.w.withdrawals[row["id"]] = row
        return dict(row)

    def list_user_payouts(self, conn, user_id, limit, cursor):
        rows = [r for t in (self.w.withdrawals, self.w.payouts) for r in t.values() if r["beneficiary"] == user_id]
        return sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)[: limit + 1]

    def get_payout(self, conn, kind, payout_id, *, for_update=True):
        r = self._table(kind).get(payout_id)
        return dict(r) if r else None

    def payout_approve_1(self, conn, kind, payout_id, admin_id, now):
        r = self._table(kind)[payout_id]
        if r["status"] != "requested":
            return 0
        r.update(status="approved_1", maker_admin=admin_id)
        return 1

    def payout_approve_2(self, conn, kind, payout_id, admin_id, now):
        r = self._table(kind)[payout_id]
        if r["status"] != "approved_1" or r["maker_admin"] == admin_id:
            return 0
        r.update(status="approved_2", checker_admin=admin_id)
        return 1

    def payout_reject(self, conn, kind, payout_id, admin_id, reason):
        r = self._table(kind)[payout_id]
        if r["status"] not in ("requested", "approved_1", "approved_2"):
            return 0
        r["status"] = "rejected"
        return 1

    # alerts / audit
    def alert_contacts_ready(self, conn, user_id):
        """app.alerts.user_sinks.require_alert_contacts uses this in tests (default: contacts set up)."""
        return str(user_id) not in self.w.alert_contacts_missing

    def insert_alert(self, conn, *, user_id, severity, kind, payload, dedup_key=None):
        if dedup_key and any(a.get("dedup_key") == dedup_key for a in self.w.alerts):
            return
        self.w.alerts.append({"user_id": user_id, "severity": severity, "kind": kind, "payload": payload,
                              "dedup_key": dedup_key})

    # flags
    def get_flag(self, conn, key, *, for_update=False):
        f = self.w.flags.get(key)
        return dict(f) if f else None

    def list_flags(self, conn):
        return [dict(f) for f in self.w.flags.values()]

    def set_flag(self, conn, key, value, by):
        self.w.flags[key] = {"key": key, "value": value, "updated_by": by, "updated_at": self.w.now,
                             "pending_value": None, "pending_by": None, "pending_at": None}

    def propose_flag(self, conn, key, value, by, now):
        f = self.w.flags.get(key)
        if f is None or f["pending_value"] is not None:
            return False
        f.update(pending_value=value, pending_by=by, pending_at=now)
        return True

    def clear_flag_proposal(self, conn, key):
        f = self.w.flags.get(key)
        if not f or f["pending_value"] is None:
            return 0
        f.update(pending_value=None, pending_by=None, pending_at=None)
        return 1

    # trusted dexes (0012; SPEC §12)
    def trusted_dexes(self, conn):
        return frozenset(d for d, r in self.w.trusted_dexes.items() if r["removed_at"] is None) | {""}

    def list_trusted_dexes(self, conn):
        return sorted((dict(r, active=r["removed_at"] is None) for r in self.w.trusted_dexes.values()),
                      key=lambda r: (not r["active"], r["dex"]))

    def add_trusted_dex(self, conn, dex, *, by, reason):
        r = self.w.trusted_dexes.get(dex)
        if r is not None and r["removed_at"] is None:
            return None
        row = {"dex": dex, "created_at": self.w.now, "updated_at": self.w.now, "added_by": by, "reason": reason,
               "removed_at": None, "removed_by": None, "removal_reason": None, "active": True}
        self.w.trusted_dexes[dex] = row
        return dict(row)

    def remove_trusted_dex(self, conn, dex, *, by, reason):
        if dex == "":
            raise ValidationFailed("the validator dex cannot be removed")
        r = self.w.trusted_dexes.get(dex)
        if r is None or r["removed_at"] is not None:
            return None
        r.update(removed_at=self.w.now, removed_by=by, removal_reason=reason, active=False, updated_at=self.w.now)
        return dict(r)

    def strategies_on_dex(self, conn, dex):
        return [{"id": st["id"], "slug": st["slug"], "status": st["status"], "markets": list(st["markets"] or [])}
                for st in sorted(self.w.strategies.values(), key=lambda x: x["slug"])
                if st["status"] != "delisted" and any(":" in m and m.split(":", 1)[0] == dex for m in st["markets"] or [])]

    def list_deposits(self, conn, user_id, limit, cursor):
        rows = sorted((dict(d) for d in self.w.deposits.values() if d["user_id"] == user_id),
                      key=lambda d: (d["created_at"], d["id"]), reverse=True)
        return rows[:limit + 1]

    # deposit scan requests (0012; AUTH F1)
    def request_deposit_scan(self, conn, user_id, *, since, now):
        prev = self.w.deposit_scan_requests.get(user_id)
        if prev is not None and prev.get("served_at") is None:
            since = min(since, prev["since"])
        self.w.deposit_scan_requests[user_id] = {"user_id": user_id, "requested_at": now, "since": since,
                                                 "served_at": None}


# ============================================================================================ other ports
class FakeAuth:
    """Tokens are registered claims: world tokens "tok-<name>" → claims dict."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict] = {}

    def add(self, token: str, uid: str, *, email: str = "u@example.com", mfa: bool = True,
            auth_time: Optional[datetime] = None, provider: str = "google.com",
            second_factor_identifier: Optional[str] = None, **extra: Any) -> str:
        at = int((auth_time or T0).timestamp())
        fb = {"sign_in_provider": provider}
        if mfa:
            fb["sign_in_second_factor"] = "totp"
        if second_factor_identifier:
            fb["second_factor_identifier"] = second_factor_identifier
        self.tokens[token] = {"sub": uid, "uid": uid, "email": email, "email_verified": True, "auth_time": at,
                              "firebase": fb, **extra}
        return token

    def verify(self, token: str) -> dict:
        if token not in self.tokens:
            raise Unauthorized("invalid token")
        return dict(self.tokens[token])

    def require_mfa(self, claims: dict) -> None:
        if (claims.get("firebase") or {}).get("sign_in_second_factor") != "totp":
            raise Unauthorized("mfa_required")

    def require_step_up(self, claims: dict, max_age_seconds: int) -> None:
        return None  # deps.check_step_up_claims enforces freshness against svc.now()


class FakeAudit:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def write(self, conn, *, actor, action, target, payload, ip_hash):
        json.dumps(payload)  # must be JSON-safe
        self.w.audit.append({"actor": actor, "action": action, "target": target, "payload": payload})


class FakeLedger:
    """Same sign rules as the real ledger: + debit / − credit; fee balances and payables never go below zero
    except for OVERDRAFT kinds; idempotent on key (same content → same id; different → Conflict)."""

    OVERDRAFT = {"profit_share", "stripe_refund", "stripe_dispute"}

    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def ensure_account(self, conn, code):
        self.w.accounts.setdefault(code, _id())

    def post(self, conn, *, idempotency_key, kind, memo, entries, created_by):
        canon = sorted((c, int(a)) for c, a in entries)
        if sum(a for _, a in canon) != 0:
            raise ValidationFailed("unbalanced")
        prev = self.w.ledger_tx.get(idempotency_key)
        if prev:
            if prev["kind"] == kind and prev["entries"] == canon:
                return prev["id"]
            raise Conflict("idempotency key reused")
        for code, amt in canon:
            nonneg = code.startswith(("user:", "creator:", "referrer:"))
            after = self.w.balances.get(code, 0) + amt
            if nonneg and after > 0 and amt > 0 and kind not in self.OVERDRAFT:
                raise InsufficientBalance("insufficient balance", account=code)
        for code, amt in canon:
            self.w.balances[code] = self.w.balances.get(code, 0) + amt
            self.w.accounts.setdefault(code, _id())
        tx = {"id": _id(), "kind": kind, "entries": canon, "memo": memo}
        self.w.ledger_tx[idempotency_key] = tx
        return tx["id"]

    def balance(self, conn, account_code):
        return self.w.balances.get(account_code, 0)


class FakeTypedData:
    def approve_agent(self, **kw):
        return {"typed_data": {"primaryType": "HyperliquidTransaction:ApproveAgent", "message": kw}, "action": kw,
                "nonce": kw["nonce"]}

    def approve_builder_fee(self, **kw):
        return {"typed_data": {"primaryType": "HyperliquidTransaction:ApproveBuilderFee", "message": kw}, "action": kw,
                "nonce": kw["nonce"]}

    def usd_send(self, **kw):
        return {"typed_data": {"primaryType": "HyperliquidTransaction:UsdSend", "message": kw}, "action": kw,
                "nonce": kw["time_ms"]}


class FakeHl:
    def __init__(self) -> None:
        self.builder_fee = 100
        self.agents: dict[str, list[dict]] = {}
        self.masters: dict[str, str] = {}
        self.sent_ok = True

    def extra_agents(self, user):
        return self.agents.get(user, [])

    def max_builder_fee(self, user, builder):
        return self.builder_fee

    def clearinghouse_state(self, user, dex=""):
        return {"assetPositions": []}

    def master_of(self, address):
        return self.masters.get(address)

    def find_usd_send(self, *, sender, destination, amount_micro, tx_hash):
        return self.sent_ok

    relayed: list = []

    def relay_exchange(self, body):
        self.relayed = [*self.relayed, body]
        return 200, {"status": "ok", "response": {"type": "default"}}

    def unknown_coins(self, coins):
        return []


class FakeStripe:
    """verify_webhook accepts signature "good"; handle_event turns {"type": "credit", "user_id", "amount_micro",
    "pi"} into one CreditInstruction-shaped object."""

    def create_topup_intent(self, *, user_id, amount_micro, token):
        return {"payment_intent_id": "pi_" + token[:16], "client_secret": "secret_" + token[:8],
                "credit_micro": amount_micro, "currency": "usd", "amount_minor": amount_micro // 10_000}

    def verify_webhook(self, payload: bytes, sig_header: str) -> dict:
        if sig_header != "good":
            class WebhookVerificationError(AppError):
                http_status, code = 400, "invalid_webhook_signature"
            raise WebhookVerificationError("signature mismatch")
        return json.loads(payload)

    def handle_event(self, event: dict) -> Any:
        credits = []
        if event.get("type") == "credit":
            credits.append(SimpleNamespace(
                user_id=event["user_id"], amount_micro=int(event["amount_micro"]), external_ref=event["pi"],
                idempotency_key=f"stripe:{event['pi']}", method="stripe", debit_account="stripe:clearing",
                credit_account=f"user:{event['user_id']}:fee_balance", kind="deposit", memo="test",
                withdrawable=False, meta={}))
        return SimpleNamespace(event_id=event.get("id", "evt"), event_type=event.get("type"), credits=credits,
                               debits=[], alerts=[], ignored=None, manual_review=None)


class FakeNotifier:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def notify(self, conn, *, user_id, severity, kind, payload, dedup_key=None):
        if dedup_key and any(a.get("dedup_key") == dedup_key for a in self.w.alerts):
            return
        self.w.alerts.append({"user_id": user_id, "severity": severity, "kind": kind, "payload": payload,
                              "dedup_key": dedup_key})

    def notify_alert(self, conn, alert):
        self.notify(conn, user_id=alert.user_id, severity=str(alert.severity), kind=alert.kind,
                    payload=dict(alert.data))


class FakeJobs:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def run(self, job, *, db, now, params):
        self.w.jobs_run.append((job, dict(params)))
        return {"job": job}

    def latest_reconciliation(self, conn):
        return None


class AllowAllRateLimit:
    def hit(self, key, limit, window_seconds):
        return True


class FakeOidc:
    def __init__(self) -> None:
        self.tokens: dict[str, dict] = {}

    def verify(self, token, audience):
        claims = self.tokens.get(token)
        if claims is None or claims.get("aud") != audience:
            raise Unauthorized("invalid identity token")
        return claims


class _Unused:
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"fake port method {name} not implemented for this test")


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests: dev env, a builder + treasury address, creator uploads on. Extra (future-config)
    attributes like edge_auth_secret / launch_phase are provided through a dynamic subclass."""
    import dataclasses
    base = dataclasses.replace(get_settings(), env=overrides.pop("env", "test"),
                               builder_address="0x" + "b" * 40, treasury_address="0x" + "7" * 40,
                               web_origin="https://aijalon.trade", api_origin="https://api.aijalon.trade",
                               stripe_webhook_secret="whsec_test", feature_creator_uploads=True)
    known = {f.name for f in dataclasses.fields(Settings)}
    direct = {k: val for k, val in overrides.items() if k in known}
    extra = {k: val for k, val in overrides.items() if k not in known}
    base = dataclasses.replace(base, **direct)
    if not extra:
        return base
    ns = {"__annotations__": {k: Any for k in extra}, **extra}
    Sub = dataclasses.dataclass(frozen=True)(type("TestSettings", (Settings,), ns))
    return Sub(**{f.name: getattr(base, f.name) for f in dataclasses.fields(Settings)})


def make_services(world: FakeWorld, settings: Optional[Settings] = None, **overrides: Any) -> Services:
    from app.api.adapters import DomainAdapter
    s = settings or make_settings(launch_phase="public", payouts_enabled=True)
    svc = Services(
        settings=s, db=FakeDatabase(world), store=FakeStore(world), auth=FakeAuth(), audit=FakeAudit(world),
        ledger=FakeLedger(world), typed_data=FakeTypedData(), hl=FakeHl(),
        stripe=FakeStripe(), usdc=_Unused(), notifier=FakeNotifier(world), sandbox=_Unused(), code_vault=_Unused(),
        kyc=_Unused(), jobs=FakeJobs(world), ratelimit=AllowAllRateLimit(), oidc=FakeOidc(), wallet_sig=_Unused(),
        domain=DomainAdapter(s), clock=lambda: world.now,
    )
    for k, val in overrides.items():
        setattr(svc, k, val)
    return svc


def add_hours(dt: datetime, hours: float) -> datetime:
    return dt + timedelta(hours=hours)

