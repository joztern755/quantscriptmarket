-- =====================================================================================================
-- 0003_seed.sql — platform ledger accounts, system flag defaults, in-house strategies (SPEC §4, §7).
-- Idempotent (ON CONFLICT DO NOTHING) so it is also safe to re-apply by hand on a fresh environment.
--
-- DECISION (in-house pricing): SPEC §7 launches SILVER, but its monthly price and profit share are an open
-- owner decision [CONFIRM]. The DB refuses status 'listed' without price_monthly_micro and
-- profit_share_bps (CHECK strategies_listed_requires_terms), so nothing can be sold or executed at an
-- implicit $0. SILVER is therefore seeded in 'review' with NULL prices; an admin sets the price and
-- profit share (Admin → "prices for in-house strategies", audit-logged) and then flips it to 'listed'.
-- config.in_house_listed = ("silver",) says WHICH in-house strategies may be listed, not that they are.
-- BTC, SOL, HYPE, GOLD, OIL, RUNNERS stay 'draft' (not visible) until their scripts pass walk-forward.
-- OIL maps to xyz:CL (WTI) pending [CONFIRM WTI vs Brent]. RUNNERS' member coins are not known here:
-- markets left empty (allowed for drafts only).
-- No strategy_versions are seeded: the registry creates version 1 from the terminal feed's engine_hash.
-- =====================================================================================================

-- ---------------------------------------------------------------- platform ledger accounts
-- Per-user / per-creator accounts (user:{id}:fee_balance, creator:{id}:payable, referrer:{id}:payable)
-- are created on demand by app.ledger.service.ensure_account.
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES
    ('platform:revenue:builder',       'revenue', NULL, false),
    ('platform:revenue:profit_share',  'revenue', NULL, false),
    ('platform:revenue:subscription',  'revenue', NULL, false),
    ('platform:revenue:posts',         'revenue', NULL, false),
    ('platform:revenue:plans',         'revenue', NULL, false),
    ('treasury:hl_usdc',               'asset',   NULL, false),
    ('stripe:clearing',                'asset',   NULL, false),
    ('builder:hl_receivable',          'asset',   NULL, false)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------- system flags (all switches OFF)
-- Values are JSON booleans. Lifting a switch is maker-checker (pending_value/pending_by, a second admin
-- applies). Per-market keys (kill_switch_market:{coin}, new_entries_paused:{coin}) are created on demand.
INSERT INTO system_flags (key, value, updated_by) VALUES
    ('kill_switch_global',  'false'::jsonb, 'migration:0003_seed'),
    ('new_entries_paused',  'false'::jsonb, 'migration:0003_seed')
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------- in-house strategies (CREST, §7)
INSERT INTO strategies (slug, name, owner_user_id, in_house, markets, timeframe, price_monthly_micro,
                        profit_share_bps, status, description) VALUES
    ('silver',  'CREST Silver',  NULL, true, ARRAY['xyz:SILVER'], '1d', NULL, NULL, 'review',
     'In-house CREST long-or-cash strategy on xyz:SILVER (daily bars, weight 0/1/2). Launch strategy; price pending owner confirmation.'),
    ('btc',     'CREST BTC',     NULL, true, ARRAY['BTC'],        '1d', NULL, NULL, 'draft',
     'In-house CREST strategy on BTC. Unlisted until its script passes walk-forward.'),
    ('sol',     'CREST SOL',     NULL, true, ARRAY['SOL'],        '1d', NULL, NULL, 'draft',
     'In-house CREST strategy on SOL. Unlisted until its script passes walk-forward.'),
    ('hype',    'CREST HYPE',    NULL, true, ARRAY['HYPE'],       '1d', NULL, NULL, 'draft',
     'In-house CREST strategy on HYPE. Unlisted until its script passes walk-forward.'),
    ('gold',    'CREST Gold',    NULL, true, ARRAY['xyz:GOLD'],   '1d', NULL, NULL, 'draft',
     'In-house CREST strategy on xyz:GOLD. Unlisted until its script passes walk-forward.'),
    ('oil',     'CREST Oil',     NULL, true, ARRAY['xyz:CL'],     '1d', NULL, NULL, 'draft',
     'In-house CREST strategy on oil (xyz:CL = WTI, [CONFIRM] vs xyz:BRENTOIL). Unlisted until its script passes walk-forward.'),
    ('runners', 'CREST Runners', NULL, true, ARRAY[]::text[],     '1d', NULL, NULL, 'draft',
     'In-house CREST multi-market strategy (member coins TBD from the terminal). Unlisted until its script passes walk-forward.')
ON CONFLICT (slug) DO NOTHING;
