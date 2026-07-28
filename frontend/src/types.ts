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
  spot_ask_notional: string;
  perp_bid_notional: string;
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
  position_mode: "one_way" | "hedge" | null;
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
  perp_margin_mode: "isolated" | "cross";
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
  funding_income_complete: boolean;
  private_stream_ready: boolean;
  open_order_count: number;
  position_count: number;
  fill_count: number;
  funding_income_count: number;
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

export interface TradeIntent {
  id: string;
  paired_position_id: string | null;
  exchange: Exchange;
  environment: string;
  base_asset: string;
  action: string;
  emergency: boolean;
  status: string;
  failure_code: string | null;
  leverage: number;
  requested_notional: string;
  base_quantity: string;
  created_at: string;
  updated_at: string;
  activity_at: string;
}

export interface OrderHistoryItem {
  id: string;
  trade_intent_id: string;
  exchange: Exchange;
  environment: string;
  base_asset: string;
  action: string;
  emergency: boolean;
  leg: string;
  market: string;
  symbol: string;
  side: string;
  status: string;
  quantity: string;
  filled_quantity: string;
  average_price: string | null;
  reduce_only: boolean;
  created_at: string;
  updated_at: string;
}

export interface FillHistoryItem {
  id: string;
  trade_intent_id: string;
  exchange: Exchange;
  environment: string;
  base_asset: string;
  action: string;
  leg: string;
  symbol: string;
  side: string;
  quantity: string;
  price: string;
  fee_amount: string;
  fee_asset: string;
  liquidity: string;
  occurred_at: string;
}

export interface PnlRealization {
  id: string;
  paired_position_id: string;
  closing_intent_id: string;
  exchange: Exchange;
  environment: string;
  base_asset: string;
  quantity: string;
  gross_pnl_usdt: string;
  opening_fee_allocated_usdt: string;
  closing_fees_usdt: string;
  net_pnl_usdt: string;
  realized_at: string;
}

export interface FundingIncome {
  id: string;
  exchange_record_id: string;
  exchange: Exchange;
  environment: Environment;
  symbol: string;
  base_asset: string;
  asset: "USDT";
  amount: string;
  rate: string | null;
  position_value: string | null;
  occurred_at: string;
  observed_at: string;
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

export interface TransferLimits {
  per_request_limit_usdt: string;
  daily_limit_usdt: string;
  enabled: boolean;
  updated_by: string;
  updated_at: string;
}

export interface AutoStrategyConfig {
  environment: Environment;
  enabled_exchanges: Exchange[];
  leverage: number;
  notional_per_trade: string;
  per_exchange_max_exposure: string;
  global_max_exposure: string;
  max_concurrent_positions: number;
  minimum_current_apr: string;
  minimum_apr_24h: string;
  minimum_apr_7d: string;
  minimum_net_return: string;
  maximum_opening_basis: string;
  minimum_two_leg_notional: string;
  book_capacity_multiple: string;
  normal_max_slippage: string;
  emergency_max_slippage: string;
  daily_max_loss: string;
  minimum_reentry_minutes: number;
  maximum_holding_hours: number;
  minimum_liquidation_buffer: string;
  close_funding_rate_below: string;
  close_net_return_below: string;
  close_basis_above: string;
  take_profit_usdt: string;
  stop_loss_usdt: string;
}

export interface StrategySummary {
  id: string;
  version: number;
  environment: Environment;
  config: AutoStrategyConfig;
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

export interface AuditEvent {
  id: string;
  occurred_at: string;
  event_type: string;
  actor: string;
  details: Record<string, unknown>;
}

export interface NotificationHistoryItem {
  id: string;
  event_type: string;
  severity: "info" | "warning" | "critical";
  channel: "telegram" | "email";
  subject: string;
  status: "pending" | "sending" | "retry" | "sent" | "dead";
  attempts: number;
  next_attempt_at: string;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
  sent_at: string | null;
}

export interface BackupStatus {
  directory_available: boolean;
  archive_count: number;
  latest: BackupArchive | null;
  archives: BackupArchive[];
}

export interface BackupArchive {
  name: string;
  size_bytes: number;
  modified_at: string;
  checksum_present: boolean;
  latest?: boolean;
}

export interface UpdateStatus {
  enabled: boolean;
  state: "unavailable" | "idle" | "queued" | "checking" | "up_to_date" | "update_available" | "updating" | "succeeded" | "failed";
  current_commit: string | null;
  available_commit: string | null;
  request_id: string | null;
  checked_at: string | null;
  completed_at: string | null;
  error_code: string | null;
}

export interface LiveOpenPreview {
  exchange: Exchange;
  environment: Environment;
  base_asset: string;
  requested_notional: string;
  leverage: number;
  market_observed_at: string;
  expires_at: string;
  maximum_slippage: string;
  base_quantity: string;
  spot_symbol: string;
  spot_reference_price: string;
  spot_limit_price: string;
  spot_quantity: string;
  spot_usdt_required: string;
  perp_symbol: string;
  perp_reference_price: string;
  perp_limit_price: string;
  perp_quantity: string;
  perp_base_multiplier: string;
  perp_usdt_margin_required: string;
  estimated_total_fees_usdt: string;
  worst_case_basis: string;
}

export interface LiveClosePreview {
  position_id: string;
  exchange: Exchange;
  environment: Environment;
  base_asset: string;
  emergency: boolean;
  leverage: number;
  market_observed_at: string;
  expires_at: string;
  maximum_slippage: string;
  base_quantity: string;
  spot_symbol: string;
  spot_reference_price: string;
  spot_limit_price: string;
  spot_quantity: string;
  spot_usdt_proceeds_before_fee: string;
  perp_symbol: string;
  perp_reference_price: string;
  perp_limit_price: string;
  perp_quantity: string;
  perp_base_multiplier: string;
  estimated_total_fees_usdt: string;
  estimated_gross_pnl_usdt: string;
  estimated_net_pnl_usdt: string;
  worst_case_basis: string;
}
