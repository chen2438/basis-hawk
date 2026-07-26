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
