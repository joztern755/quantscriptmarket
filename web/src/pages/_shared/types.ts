// Response shapes the pages expect from the /v1 API (SPEC §8). The backend routers were not yet
// written when these pages were built, so every field the UI does not strictly need is optional
// and pages read them through the tolerant accessors in ./data.ts.

export type Micro = number; // integer micro-USD

export type SignalState = "trades" | "holds" | "unknown";

export interface StrategySummary {
  id: string;
  slug: string;
  name: string;
  description?: string;
  in_house?: boolean;
  featured?: boolean;
  markets: string[];
  timeframe?: string;
  price_monthly_micro: Micro;
  profit_share_bps: number;
  status?: string; // listed | paused | ...
  subscribers?: number | null;
  roi_pct?: number | null; // live ROI since current version (null when hidden)
  pnl_micro?: Micro | null; // aggregate $ made for users (null when < 5 subscribers)
  signal_state?: SignalState; // "trades" = has an active signal, "holds" = flat / no active signals
  live_days?: number | null; // days since current version went live
  live_since?: string | null;
  max_leverage?: number;
  creator_name?: string;
  rating_avg?: number | null;
  rating_count?: number;
}

export interface EquityPoint {
  t: number | string;
  v: number;
}

export interface StrategyVersion {
  id?: string;
  version: number;
  published_at?: string | null;
  live_since?: string | null;
  note?: string | null;
  reset?: boolean;
}

export interface BacktestPart {
  roi_pct?: number | null;
  max_drawdown_pct?: number | null;
  sharpe?: number | null;
  trades?: number | null;
  equity?: EquityPoint[];
  start?: string;
  end?: string;
}

export interface Backtest {
  in_sample?: BacktestPart;
  out_of_sample?: BacktestPart;
  oos_start?: string | number | null;
  equity?: EquityPoint[];
  generated_at?: string;
  fees_note?: string;
  status?: string; // pending | done | failed
  error?: string | null;
}

export interface StrategyDetail extends StrategySummary {
  current_version?: StrategyVersion | null;
  versions?: StrategyVersion[];
  equity?: EquityPoint[]; // live on-chain equity index (current version)
  stats_hidden?: boolean;
  backtest?: Backtest | null;
  risk_ack_text?: string | null; // strategy-specific risk acknowledgement
  showcase?: ShowcaseWallet[];
}

export interface ShowcaseWallet {
  address: string;
  period_month: string; // YYYY-MM
  revealed_at?: string | null;
  roi_pct?: number | null;
}

export interface Review {
  id?: string;
  rating: number;
  body: string;
  author?: string | null;
  created_at?: string;
}

export interface Post {
  id: string;
  title: string;
  excerpt?: string | null;
  body?: string | null; // present when free or purchased
  price_micro: Micro;
  purchased?: boolean;
  published_at?: string | null;
  strategy_slug?: string | null;
  strategy_name?: string | null;
  creator_name?: string | null;
}

export interface LeaderRow {
  slug: string;
  name: string;
  roi_pct?: number | null;
  pnl_micro?: Micro | null;
  subscribers?: number | null;
  markets?: string[];
  live_days?: number | null;
}

export type SubStatus = "pending" | "active" | "past_due" | "reduce_only" | "paused_user" | "cancelled";

export interface Subscription {
  id: string;
  strategy_slug?: string;
  strategy_name?: string;
  strategy_id?: string;
  trading_address: string;
  allocation_micro: Micro;
  max_leverage_x100: number;
  status: SubStatus;
  current_period_end?: string | null;
  cum_pnl_micro?: Micro;
  hwm_micro?: Micro;
  strategy_max_leverage?: number;
  created_at?: string;
}

export interface Position {
  trading_address: string;
  coin: string;
  szi: string | number; // signed size (HL string)
  entry_px?: string | number | null;
  position_value?: string | number | null; // USD (HL string)
  unrealized_pnl?: string | number | null;
  leverage?: number | string | null;
  subscription_id?: string | null;
  liquidation_px?: string | number | null;
}

export interface LedgerRow {
  id?: string;
  created_at: string;
  kind: string;
  memo?: string | null;
  amount_micro: Micro; // + credit to user balance, − debit (display convention)
}

export interface Balance {
  balance_micro: Micro;
  estimated_monthly_need_micro?: Micro | null;
  history?: LedgerRow[];
  ledger?: LedgerRow[];
}

export interface Alert {
  id: string;
  severity: "info" | "warn" | "critical";
  kind: string;
  message?: string | null;
  payload?: Record<string, unknown> | null;
  created_at: string;
  acked_at?: string | null;
}

export interface ReferralInfo {
  code: string;
  tier: string;
  active_users_30d?: number;
  notional_30d_micro?: Micro;
  earnings_total_micro?: Micro;
  earnings_30d_micro?: Micro;
  payable_micro?: Micro;
  referred_users?: number;
}

/** No-code builder JSON spec (SPEC §10) — compiled server-side by sandbox/nocode.py. */
export type IndicatorKind = "sma" | "ema" | "rsi" | "atr" | "highest" | "lowest" | "roc";
export type PriceSource = "o" | "h" | "l" | "c" | "v";

export interface NoCodeIndicator {
  id: string; // referenced by rules
  kind: IndicatorKind;
  coin: string;
  source: PriceSource; // ignored for atr (uses h/l/c)
  period: number;
}

export type Operand = { ref: string } | { price: PriceSource; coin: string } | { const: number };
export type Comparator = ">" | ">=" | "<" | "<=";

export interface NoCodeCondition {
  left: Operand;
  op: Comparator;
  right: Operand;
}

export interface NoCodeRule {
  coin: string;
  combine: "all" | "any";
  conditions: NoCodeCondition[];
  weight: number; // target weight when the rule matches; first matching rule per coin wins
}

export interface NoCodeSpec {
  version: 1;
  markets: string[];
  timeframe: "1h" | "4h" | "1d";
  lookback: number;
  max_leverage: number;
  indicators: NoCodeIndicator[];
  rules: NoCodeRule[];
  default_weight: number; // weight per coin when no rule matches
}
