import { useState } from "react";
import type { Exchange, Settings } from "./types";

const names: Record<Exchange, string> = { binance: "Binance", okx: "OKX", mexc: "MEXC", bybit: "Bybit" };

export function SettingsPanel({ value, onSave, onClose }: { value: Settings; onSave: (value: Settings) => Promise<void>; onClose: () => void }) {
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const updateFee = (exchange: Exchange, side: "spot_taker" | "perp_taker", percent: string) => {
    const decimal = String(Number(percent || 0) / 100);
    setDraft({ ...draft, fees: { ...draft.fees, [exchange]: { ...draft.fees[exchange], [side]: decimal } } });
  };
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
    <section className="settings-panel" role="dialog" aria-modal="true" aria-label="扫描设置" onMouseDown={(event) => event.stopPropagation()}>
      <header><div><p className="eyebrow">SCANNER CONFIG</p><h2>扫描设置</h2></div><button className="icon-button" onClick={onClose}>×</button></header>
      <div className="form-grid">
        <label>每所候选数<input type="number" min="10" max="500" value={draft.universe_size} onChange={(e) => setDraft({ ...draft, universe_size: Number(e.target.value) })} /></label>
        <label>最低两腿成交额（USDT）<input type="number" min="0" value={draft.minimum_quote_volume} onChange={(e) => setDraft({ ...draft, minimum_quote_volume: e.target.value })} /></label>
        <label>收益估算持有期（天）<input type="number" min="1" max="365" value={draft.holding_period_days} onChange={(e) => setDraft({ ...draft, holding_period_days: Number(e.target.value) })} /></label>
        <label>历史保留（天）<input type="number" min="1" max="365" value={draft.retention_days} onChange={(e) => setDraft({ ...draft, retention_days: Number(e.target.value) })} /></label>
      </div>
      <h3>Taker 费率估算</h3>
      <div className="fee-table">
        <div className="fee-row heading"><span>交易所</span><span>现货 %</span><span>永续 %</span></div>
        {(Object.keys(names) as Exchange[]).map((exchange) => <div className="fee-row" key={exchange}>
          <strong>{names[exchange]}</strong>
          <input aria-label={`${names[exchange]} 现货费率`} value={Number(draft.fees[exchange].spot_taker) * 100} onChange={(e) => updateFee(exchange, "spot_taker", e.target.value)} />
          <input aria-label={`${names[exchange]} 永续费率`} value={Number(draft.fees[exchange].perp_taker) * 100} onChange={(e) => updateFee(exchange, "perp_taker", e.target.value)} />
        </div>)}
      </div>
      <p className="notice">基础档费率核对于 {draft.fee_checked_at}。地区、账户等级及活动可能改变实际费率，请按账户实际费率覆盖。</p>
      <footer><button className="button secondary" onClick={onClose}>取消</button><button className="button primary" disabled={saving} onClick={async () => { setSaving(true); try { await onSave(draft); onClose(); } finally { setSaving(false); } }}>{saving ? "保存中…" : "保存并重算"}</button></footer>
    </section>
  </div>;
}
