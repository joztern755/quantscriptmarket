"""Pure domain logic (SPEC §3 `backend/app/domain/`).

Rules for everything in this package:
- stdlib only (plus the stdlib-only `app.money`, `app.config`, `app.errors`); no I/O, no DB, no network, no clock
  reads — callers inject `now`.
- money is integer micro-USD; rates are integer bps (1 bp = 0.01%) unless a name says otherwise.
- every function is deterministic for its inputs, so it can be re-run for idempotent settlement/replay.

Modules: fees, profit_share, referrals, billing, risk, jitter, track_record, alerts_rules.
"""
from __future__ import annotations
