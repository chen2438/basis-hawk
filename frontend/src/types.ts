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
  history_progress_percent: number;
  history_download_rate_per_minute: number | null;
  history_syncing: boolean;
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
  notional_usdt: string;
  leverage: number;
  spot_entry_price: string;
  perp_entry_price: string;
  spot_fee_rate: string;
  perp_fee_rate: string;
  opening_fees_usdt: string;
  remaining_opening_fees_usdt: string;
  closing_fees_usdt: string | null;
  realized_pnl_usdt: string | null;
  funding_income_usdt: string;
  spot_exit_price: string | null;
  perp_exit_price: string | null;
  unrealized_pnl_usdt: string | null;
  estimated_closing_fees_usdt: string | null;
  estimated_final_pnl_usdt: string | null;
  valuation_observed_at: string | null;
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
  failure_code: string | null;
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
  minimum_opening_basis: string;
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

export interface NotificationSettings {
  email: {
    configured: boolean;
    security: "starttls" | "smtps";
    port: number;
    authentication_configured: boolean;
    sender_configured: boolean;
    recipient_configured: boolean;
  };
  telegram: {
    configured: boolean;
  };
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

export type V2OpportunityType = "funding" | "cross_funding" | "basis";

export interface V2OpportunityLeg {
  account_id: string | null;
  exchange: Exchange;
  market_type: "spot" | "perpetual";
  side: "buy" | "sell";
  symbol: string;
  price: string;
  quote_volume_24h: string;
  funding_rate: string | null;
  funding_interval_hours: string | null;
  annualized_funding: string | null;
  fee_rate: string;
  fee_source: "actual" | "manual" | "default";
}

export interface V2Opportunity {
  id: string;
  opportunity_type: V2OpportunityType;
  base_asset: string;
  observed_at: string;
  entry_spread: string;
  annualized_return: string;
  projected_return: string;
  estimated_round_trip_fees: string;
  holding_days: number;
  executable_notional_usdt: string;
  legs: V2OpportunityLeg[];
}

export interface V2Account {
  id: string;
  exchange: Exchange;
  environment: Environment;
  label: string;
  masked_api_key: string;
  position_mode: "one_way" | "hedge" | null;
  trading_default: boolean;
  scanner_default: boolean;
  capabilities: Record<string, boolean | string | null>;
  fees: {
    spot_maker: string | null;
    spot_taker: string | null;
    perpetual_maker: string | null;
    perpetual_taker: string | null;
    source: "actual" | "manual" | "default";
    checked_at: string | null;
  };
  updated_at: string;
}

export interface ExecutionTaskLeg {
  id: string;
  ordinal: number;
  account_id: string | null;
  exchange: Exchange;
  role: "anchor" | "hedge";
  market_type: "spot" | "perpetual";
  side: "buy" | "sell";
  base_asset: string;
  symbol: string;
  target_quantity: string;
  resolved_base_quantity: string | null;
  signed_base_ratio: string | null;
  per_order_quantity: string | null;
  order_mode: "maker" | "protected_ioc" | "market";
  maximum_slippage: string;
  maker_policy: {
    book_level: number;
    maximum_chases: number;
    fallback_mode: "protected_ioc" | "market" | "fail";
  } | null;
  margin_mode: "isolated" | "cross" | null;
  leverage: number | null;
  reduce_only: boolean;
}

export interface ExecutionTask {
  id: string;
  name: string;
  display_symbol: string;
  environment: "paper" | Environment;
  base_asset: string;
  quantity_mode: "base" | "usdt";
  source_opportunity_id: string | null;
  create_strategy: boolean;
  hedge_trigger: string;
  hedge_threshold: string | null;
  maximum_base_exposure: string;
  maximum_notional_exposure_usdt: string;
  maximum_retries: number;
  status: string;
  failure_code: string | null;
  preflight: Record<string, unknown> | null;
  preflight_expires_at: string | null;
  created_by: string;
  version: number;
  created_at: string;
  updated_at: string;
  legs: ExecutionTaskLeg[];
}

export interface ExecutionActivity {
  runs: Array<Record<string, string | number | null>>;
  orders: Array<Record<string, string | number | boolean | null>>;
  fills: Array<Record<string, string | number | null>>;
}

export interface StrategyLeg {
  id: string;
  ordinal: number;
  opening_task_leg_id: string;
  account_id: string | null;
  exchange: Exchange;
  role: "anchor" | "hedge";
  market_type: "spot" | "perpetual";
  side: "buy" | "sell";
  symbol: string;
  initial_base_quantity: string;
  remaining_base_quantity: string;
  entry_price: string;
  exit_price: string | null;
  fees_usdt: string;
  realized_pnl_usdt: string;
}

export interface Strategy {
  id: string;
  name: string;
  environment: string;
  base_asset: string;
  opening_task_id: string;
  closing_task_id: string | null;
  status: "running" | "closing" | "ended" | "manual_review";
  realized_pnl_usdt: string;
  funding_income_usdt: string;
  fees_usdt: string;
  net_pnl_usdt: string;
  opened_at: string;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
  legs: StrategyLeg[];
}

export interface AdlPosition {
  account_id: string;
  account_label: string;
  exchange: Exchange;
  environment: Environment;
  symbol: string;
  position_side: "long" | "short" | "net";
  risk_level: number | null;
  native_value: string | null;
  event_only: boolean;
  observed_at: string;
}
