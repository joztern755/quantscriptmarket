// Response shapes of the /v1 API, mirroring backend/app/api/schemas.py EXACTLY (the backend is the source of
// truth; see docs/API_CONTRACT.md). Money is integer micro-USD (`*_micro`), rates are integer bps, timestamps are
// ISO-8601 strings, Hyperliquid sizes/prices are decimal strings. List endpoints return Page<T>.

export type Micro = number; // integer micro-USD

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

// ------------------------------------------------------------------------------------------ public
export type SignalState = "trades" | "holds" | "unknown";

/** StrategyStats — hidden (nulls + hidden_reason) below the k-anonymity floor or before going live. */
export interface StrategyStats {
  subscribers: number | null;
  roi_bps: number | null;
  pnl_micro: Micro | null;
  since: string | null;
  hidden_reason: "not_live" | "too_few_subscribers" | string | null;
}

/** GET /v1/public/strategies → Page<StrategySummary>. */
export interface StrategySummary {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  in_house: boolean;
  markets: string[];
  timeframe: string;
  status: string; // listed | paused
  price_monthly_micro: Micro | null;
  profit_share_bps: number;
  platform_profit_share_bps: number;
  platform_profit_share_mode: "on_top" | "carved_out" | string;
  holds: boolean | null;
  signal_state: SignalState;
  current_version: number | null;
  live_since: string | null;
  live_days: number | null;
  not_live_proven: boolean;
  max_leverage: number | null;
  history_days: number | null;
  /** Set when the current version has < short_history_warning_days (365) of history → "Short history (N days)". */
  short_history_days: number | null;
  /** SPEC §12: in-house, $0 and 0% profit share (SILVER). `showcase_text` must be shown on card and page. */
  free_showcase: boolean;
  showcase_text: string | null;
  stats: StrategyStats;
}

export interface StrategyVersionPublic {
  version: number;
  published_at: string | null;
  live_since: string | null;
  is_current: boolean;
}

/** Sandbox backtest segment metrics (app/sandbox/backtest.py compute_metrics); returns are fractions (0.12 = 12%). */
export interface BacktestMetrics {
  bars?: number;
  start_t?: number | null;
  end_t?: number | null;
  years?: number;
  total_return?: number;
  cagr?: number | null;
  max_drawdown?: number;
  sharpe?: number | null;
  exposure?: number;
  avg_gross_leverage?: number;
  final_equity?: number;
}

/** Public backtest = sandbox report minus trades / latest_signal / data_notes. */
export interface BacktestReport {
  meta?: { markets?: string[]; timeframe?: string; lookback?: number; max_leverage?: number };
  period?: { first_bar_t?: number; last_bar_t?: number; aligned_bars?: number; sim_start_t?: number; sim_end_t?: number; sim_days?: number };
  history_days?: number;
  equity_curve?: [number, number][];
  buy_and_hold_curve?: [number, number][];
  trade_count?: number;
  fees_paid?: number;
  funding_paid?: number;
  liquidated?: boolean;
  metrics?: { full?: BacktestMetrics; in_sample?: BacktestMetrics; out_of_sample?: BacktestMetrics; split_t?: number | null };
  buy_and_hold?: { full?: BacktestMetrics; in_sample?: BacktestMetrics; out_of_sample?: BacktestMetrics; split_t?: number | null };
  listing_eligible_history?: boolean;
  warnings?: string[];
}

/** GET /v1/public/strategies/{slug}. */
export interface StrategyDetail extends StrategySummary {
  versions: StrategyVersionPublic[];
  backtest: BacktestReport | null;
  backtest_warning: string | null;
  risk_ack_text: string;
  rating_avg_x100: number | null;
  rating_count: number;
}

/** GET /v1/public/strategies/{slug}/equity (EquitySeriesOut) — daily cumulative live record of the CURRENT version.
 *  Wire `t` is a UTC day ("2026-09-20") or ms epoch; parsed to ms. When hidden (k-anonymity / not live yet) `points`
 *  is empty and `hidden_reason` says why. */
export interface EquityPoint {
  t: number;
  pnl_micro: Micro | null;
  roi_bps: number | null;
}
export interface EquityOut {
  points: EquityPoint[];
  hidden_reason: "not_live" | "too_few_subscribers" | string | null;
  version?: number | null;
  since?: string | null;
}

/** POST /v1/me/plan {plan} → PlanChangeOut (step-up; 402 insufficient_balance; 409 already on plan / too many strategies). */
export type PlanKey = "free" | "pro" | "max";
export interface PlanChangeOut {
  plan: PlanKey | string;
  charged_micro: Micro;
  period_end: string | null;
  fee_balance_micro: Micro;
}

/** GET /v1/public/showcase/{slug} → ShowcaseWallet[] (plain list). */
export interface ShowcaseWallet {
  address: string;
  period_month: string; // YYYY-MM-DD (first day of the month)
  revealed_at: string;
}

/** GET /v1/public/strategies/{slug}/reviews → Page<Review>; POST /v1/reviews → Review. */
export interface Review {
  id: string;
  rating: number;
  body: string | null;
  author: string;
  created_at: string;
}

/** GET /v1/public/posts → Page<PostSummary>. */
export interface PostSummary {
  id: string;
  title: string;
  price_micro: Micro;
  strategy_slug: string | null;
  creator_display_name: string | null;
  published_at: string | null;
  preview: string | null; // first 280 chars, free posts only
}

/** GET /v1/posts/{id} (auth) and GET /v1/public/posts/{id} (free body only). */
export interface Post {
  id: string;
  title: string;
  price_micro: Micro;
  strategy_slug: string | null;
  published_at: string | null;
  body: string | null;
  purchased: boolean;
}

export interface PurchaseOut {
  post_id: string;
  charged_micro: Micro;
  fee_balance_micro: Micro;
}

/** GET /v1/public/leaderboard → LeaderboardOut. */
export interface LeaderRow {
  rank: number;
  slug: string;
  name: string;
  roi_bps: number | null;
  pnl_micro: Micro | null;
  subscribers: number | null;
}
export interface LeaderboardOut {
  by: string;
  period: string;
  entries: LeaderRow[];
}

// ------------------------------------------------------------------------------------------ user
export type SubStatus = "pending" | "active" | "past_due" | "reduce_only" | "paused_user" | "closing" | "cancelled";

export interface Subscription {
  id: string;
  strategy_id: string;
  strategy_slug: string | null;
  strategy_name: string | null;
  strategy_markets: string[];
  trading_address: string;
  allocation_micro: Micro;
  max_leverage_x100: number;
  status: SubStatus;
  cancel_positions: "close" | "leave" | null;
  cancelled_at: string | null;
  current_period_end: string | null;
  cum_pnl_micro: Micro;
  hwm_micro: Micro;
  created_at: string;
}

export interface SubscriptionCreateOut {
  subscription: Subscription;
  charged_micro: Micro;
  fee_balance_micro: Micro;
}

/** GET /v1/positions → PositionsOut. */
export interface Position {
  trading_address: string;
  coin: string;
  size: string; // signed (HL szi)
  entry_px: string | null;
  position_value: string | null;
  unrealized_pnl: string | null;
  leverage: string | null;
  liquidation_px: string | null;
}
export interface PositionsOut {
  positions: Position[];
  unavailable: string[];
}

/** GET /v1/balance/ledger → Page<LedgerRow>; EarningsOut.recent. amount: + = balance increased. */
export interface LedgerRow {
  tx_id: string;
  kind: string;
  memo: string | null;
  amount_micro: Micro;
  created_at: string;
}

/** GET /v1/balance. */
export interface Balance {
  fee_balance_micro: Micro;
  withdrawable_micro: Micro;
  withdrawals_pending_micro: Micro;
  estimated_monthly_need_micro: Micro;
  reserve_required_micro: Micro;
  min_topup_micro: Micro;
}

export interface UsdcTypedDataOut {
  from_address: string;
  destination: string;
  amount_micro: Micro;
  time_ms: number;
  payload: { typed_data: unknown; action: unknown; nonce: number };
  exchange_url: string;
}

export interface DepositOut {
  id: string;
  method: string;
  amount_micro: Micro;
  status: string;
  external_ref: string | null;
  created_at: string;
}

export interface UsdcConfirmOut {
  credited: DepositOut[];
  fee_balance_micro: Micro;
}

export interface StripeDepositOut {
  payment_intent_id: string;
  client_secret: string;
  amount_micro: Micro;
}

/** POST /v1/withdrawals, POST /v1/payouts, GET /v1/withdrawals (Page). */
export interface PayoutOut {
  id: string;
  kind: "withdrawal" | "payout";
  amount_micro: Micro;
  to_address: string;
  status: string; // requested | approved_1 | approved_2 | sent | rejected
  tx_hash: string | null;
  created_at: string;
}

export interface Alert {
  id: string;
  severity: "info" | "warn" | "critical";
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
  acked_at: string | null;
}

export interface AgentOut {
  id: string;
  master_address: string;
  /** null while status = "requested" (the executor generates the key, migrations/0016) */
  agent_address: string | null;
  agent_name: string;
  status: "requested" | "pending_approval" | "active" | "revoked" | "rotated" | "expired" | string;
  approved_at: string | null;
  created_at: string;
}

/** POST /v1/agents (201): an agent REQUEST (status "requested", no address). Poll GET /v1/agents/{id}
 *  (core/agentprep.ts AgentDetail) until the executor has generated and attested the key. approve_agent is always
 *  null now (the browser builds ApproveAgent locally); approve_builder_fee = {typed_data, action, nonce}. */
export interface AgentCreateOut {
  agent: AgentOut;
  approve_agent: { typed_data: unknown; action: unknown; nonce: number } | null;
  approve_builder_fee: { typed_data: unknown; action: unknown; nonce: number } | null;
  exchange_url: string;
  required_builder_fee_tenths_bp: number;
}

export interface BuilderApprovalOut {
  master_address: string;
  max_fee_rate_tenths_bp: number;
  required_tenths_bp: number;
  sufficient: boolean;
  verified_on_chain_at: string | null;
}

/** GET /v1/referrals. */
export interface ReferralInfo {
  code: string;
  link: string;
  tier: string;
  share_of_pool_bps: number;
  active_referred_users_30d: number;
  referred_notional_30d_micro: Micro;
  referred_users_total: number;
  earnings_payable_micro: Micro;
  earnings_total_micro: Micro;
  next_tier: { name: string; min_active_users: number; min_notional_30d_micro: Micro; share_of_pool_bps: number } | null;
}

// ------------------------------------------------------------------------------------------ creator
export interface CreatorStrategy {
  id: string;
  slug: string;
  name: string;
  status: string; // draft | review | listed | paused | delisted
  markets: string[];
  timeframe: string;
  price_monthly_micro: Micro | null;
  profit_share_bps: number;
  description: string | null;
  created_at: string;
}

export interface CreatorVersion {
  id: string;
  version: number;
  code_hash: string;
  published_at: string | null;
  live_since: string | null;
  params: Record<string, unknown>;
  backtest: BacktestReport | null;
  created_at: string;
  warning: string | null;
}

export interface Earnings {
  payable_micro: Micro;
  payouts_pending_micro: Micro;
  total_earned_micro: Micro;
  by_strategy: StrategyEarnings[];
  /** Paid posts not linked to a strategy, and any other credit; Σ by_strategy + these = total_earned_micro. */
  general_posts_micro?: Micro;
  other_micro?: Micro;
  recent: LedgerRow[];
}

/** EarningsOut.by_strategy row: per-strategy breakdown by source (creator's share, micro-USD, all time).
 *  Backend field names: builder_share_micro / subscription_share_micro / profit_share_micro / posts_micro, total =
 *  earned_micro. The shorter builder_micro / subscription_micro / total_micro / name are accepted too. */
export interface StrategyEarnings {
  strategy_id: string | null;
  slug?: string | null;
  name?: string | null;
  active_subscribers?: number | null;
  earned_micro?: Micro | null;
  builder_share_micro?: Micro | null;
  subscription_share_micro?: Micro | null;
  profit_share_micro?: Micro | null;
  posts_micro?: Micro | null;
  builder_micro?: Micro | null;
  subscription_micro?: Micro | null;
  total_micro?: Micro | null;
}

/** GET /v1/creator/posts → Page<CreatorPostOut> (the creator's own posts, newest first). */
export interface CreatorPost {
  id: string;
  title: string;
  price_micro: Micro;
  strategy_slug?: string | null;
  strategy_id?: string | null;
  published_at: string | null;
  created_at?: string;
  body?: string | null;
  sales?: number | null;
  sales_count?: number | null;
  gross_sales_micro?: Micro | null;
}

/** No-code builder JSON spec — EXACTLY what backend/app/sandbox/nocode.py validate_spec/compile_spec accept (SPEC §10). */
export type IndicatorKind = "sma" | "ema" | "rsi" | "atr" | "highest" | "lowest" | "roc";
export type NcSource = "close" | "high" | "low" | "open";
export type NcPriceField = "close" | "open" | "high" | "low" | "volume";
export type NcOp = ">" | "<" | ">=" | "<=" | "crosses_above" | "crosses_below";

export interface NcIndicator {
  type: IndicatorKind;
  source?: NcSource; // omitted for atr (uses high/low/close); default close
  period: number; // 1–500
  shift?: number; // 0–50 bars ago
}

/** An indicator id, a price field of the last closed bar, or a finite number. */
export type NcOperand = string | number;

export interface NcCondition {
  left: NcOperand;
  op: NcOp;
  right: NcOperand;
}

export type NcWhen = { all: (NcCondition | NcWhen)[] } | { any: (NcCondition | NcWhen)[] };

export interface NcRule {
  when: NcWhen;
  weight: number; // applied to EACH market independently; first matching rule wins
}

export interface NoCodeSpec {
  version: 1;
  markets: string[];
  timeframe: "1h" | "4h" | "1d";
  lookback: number;
  max_leverage: number;
  indicators: Record<string, NcIndicator>;
  rules: NcRule[];
  default_weight: number;
}
