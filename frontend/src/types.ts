export type Exchange = "binance" | "okx" | "mexc" | "bybit" | "bitget" | "gate";
export type Quality = "healthy" | "warming" | "stale";

export interface Opportunity {
  exchange: Exchange;
  base_asset: string;
  spot_symbol: string;
  perp_symbol: string;
  observed_at: string;
  spot_ask: string;
  perp_bid: string;
  executable_basis: string;
  top_book_notional: string;
  current_funding_rate: string;
  funding_interval_hours: string;
  next_funding_at: string | null;
  current_apr: string;
  apr_24h: string | null;
  apr_7d: string | null;
  net_return: string | null;
  spot_quote_volume_24h: string;
  perp_quote_volume_24h: string;
  spot_taker_fee: string;
  perp_taker_fee: string;
  quality: Quality;
}
export interface ExchangeStatus {
  exchange: Exchange;
  state: string;
  last_quote_at: string | null;
  latency_ms: number | null;
  error: string | null;
  instruments: number;
  history_ready: number;
}

export interface FeeRate { spot_taker: string; perp_taker: string }
export interface Settings {
  universe_size: number;
  minimum_quote_volume: string;
  holding_period_days: number;
  retention_days: number;
  fees: Record<Exchange, FeeRate>;
  fee_checked_at: string;
}

export type Environment = "sandbox" | "live";

export interface CredentialSummary {
  exchange: Exchange;
  environment: Environment;
  label: string;
  masked_api_key: string;
  updated_at: string;
}

export interface AccountSnapshot {
  exchange: Exchange;
  environment: Environment;
  observed_at: string;
  spot_usdt_available: string;
  perp_usdt_available: string;
  perp_usdt_equity: string;
  shared_balance: boolean;
  account_mode: string;
  position_mode: string;
  trade_permission: boolean | null;
}

export interface ReconciliationAccount {
  exchange: Exchange;
  environment: Environment;
  status: string;
  reason: string;
  trading_state_complete: boolean;
  order_reconciliation_complete: boolean;
  fill_reconciliation_complete: boolean;
  private_stream_ready: boolean;
  open_order_count: number;
  position_count: number;
  fill_count: number;
  recovered_order_count: number;
  checked_at: string;
}

export interface ExecutionStatus {
  state: string;
  reason: string;
  updated_at: string | null;
  accounts: ReconciliationAccount[];
}

export interface PairedPosition {
  id: string;
  opening_intent_id: string;
  closing_intent_id: string | null;
  exchange: Exchange;
  environment: string;
  base_asset: string;
  initial_quantity: string;
  quantity: string;
  spot_entry_price: string;
  perp_entry_price: string;
  opening_fees_usdt: string;
  remaining_opening_fees_usdt: string;
  closing_fees_usdt: string | null;
  realized_pnl_usdt: string | null;
  status: string;
  opened_at: string;
  closed_at: string | null;
}

export interface InternalTransfer {
  id: string;
  exchange: Exchange;
  environment: Environment;
  asset: "USDT";
  direction: "spot_to_perp" | "perp_to_spot";
  amount_usdt: string;
  status: string;
  exchange_transfer_id: string | null;
  source_balance_before: string | null;
  target_balance_before: string | null;
  expected_target_balance: string | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
  completed_at: string | null;
}

export interface StrategySummary {
  id: string;
  version: number;
  environment: Environment;
  config: Record<string, unknown>;
  created_by: string;
  created_at: string;
}

export interface AutomationStatus {
  state: string;
  reason: string;
  updated_by: string;
  updated_at: string;
  active_strategy: StrategySummary | null;
  latest_strategy: StrategySummary | null;
}
