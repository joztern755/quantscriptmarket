"""Creator earnings per strategy (GET /v1/creator/earnings). No FastAPI imports (unit-testable).

Input rows: ``store.active_subscribers_by_strategy`` ({strategy_id, slug, active_subscribers}) and
``store.creator_earnings_by_strategy`` ({strategy_id|None, cat, micro}) — credits to ``creator:{id}:payable``
attributed to a strategy through their ledger idempotency keys (sub:/ps:/post:) or the fill that carried the builder
fee. Σ over strategies + general posts + other = lifetime credits (``total_earned_micro``).
"""
from __future__ import annotations

__all__ = ["earnings_breakdown"]

_EARNING_FIELDS = {"builder": "builder_share_micro", "subscription": "subscription_share_micro",
                   "profit_share": "profit_share_micro", "posts": "posts_micro"}


def earnings_breakdown(strategies: list[dict], credits: list[dict]) -> tuple[list[dict], int, int]:
    """(per-strategy rows, general_posts_micro, other_micro) from store.active_subscribers_by_strategy and
    store.creator_earnings_by_strategy rows. Credits of a strategy the creator no longer owns count as other."""
    by_id = {str(r["strategy_id"]): {"strategy_id": r["strategy_id"], "slug": r["slug"],
                                     "active_subscribers": int(r["active_subscribers"] or 0), "earned_micro": 0,
                                     **{f: 0 for f in _EARNING_FIELDS.values()}} for r in strategies}
    general_posts = other = 0
    for c in credits:
        amt, cat = int(c["micro"] or 0), str(c["cat"])
        row = by_id.get(str(c["strategy_id"])) if c.get("strategy_id") is not None else None
        if row is not None and cat in _EARNING_FIELDS:
            row[_EARNING_FIELDS[cat]] += amt
            row["earned_micro"] += amt
        elif row is None and cat == "posts" and c.get("strategy_id") is None:
            general_posts += amt
        else:
            other += amt
    return list(by_id.values()), general_posts, other
