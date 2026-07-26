import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { LoginPage } from "./LoginPage";
import { SettingsPanel } from "./SettingsPanel";
import type { Exchange, ExchangeStatus, Opportunity, Quality, Settings } from "./types";

const exchangeNames: Record<Exchange, string> = { binance: "Binance", okx: "OKX", mexc: "MEXC", bybit: "Bybit", bitget: "Bitget", gate: "Gate" };
const exchanges = Object.keys(exchangeNames) as Exchange[];
const qualityNames: Record<Quality, string> = { healthy: "有效", warming: "预热中", stale: "已陈旧" };
const percent = (value: string | null, digits = 2) => value == null ? "—" : `${(Number(value) * 100).toFixed(digits)}%`;
const money = (value: string) => Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value));
const price = (value: string) => Number(value).toLocaleString("en-US", { maximumFractionDigits: 8 });

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return <div className="empty-chart">历史快照正在积累</div>;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const points = values.map((value, index) => `${(index / (values.length - 1)) * 100},${42 - ((value - min) / span) * 36}`).join(" ");
  return <svg className="sparkline" viewBox="0 0 100 48" preserveAspectRatio="none" aria-label="历史收益趋势"><polyline points={points} /></svg>;
}

export default function App() {
  const [username, setUsername] = useState<string | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  useEffect(() => {
    api.session()
      .then((session) => setUsername(session.username))
      .catch(() => setUsername(null))
      .finally(() => setCheckingSession(false));
  }, []);

  if (checkingSession) return <main className="login-shell"><p>正在验证会话…</p></main>;
  if (!username) return <LoginPage onAuthenticated={setUsername} />;
  return <Dashboard username={username} onLogout={() => setUsername(null)} />;
}

function Dashboard({ username, onLogout }: { username: string; onLogout: () => void }) {
  const [items, setItems] = useState<Opportunity[]>([]);
  const [statuses, setStatuses] = useState<ExchangeStatus[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [selected, setSelected] = useState<Opportunity | null>(null);
  const [history, setHistory] = useState<Opportunity[]>([]);
  const [range, setRange] = useState("24h");
  const [exchange, setExchange] = useState<Exchange | "all">("all");
  const [quality, setQuality] = useState<Quality | "all">("healthy");
  const [search, setSearch] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lastSequence = useRef<number | null>(null);

  useEffect(() => {
    Promise.all([api.opportunities(), api.statuses(), api.settings()])
      .then(([opportunities, state, config]) => { setItems(opportunities.items); setStatuses(state.items); setSettings(config); })
      .catch((reason: Error) => setError(reason.message));
    const timer = window.setInterval(() => api.statuses().then((value) => setStatuses(value.items)).catch(() => undefined), 5000);
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer = 0;
    const restore = () => api.opportunities().then((value) => {
      setItems(value.items);
      lastSequence.current = value.sequence;
    }).catch((reason: Error) => setError(reason.message));
    const connect = () => {
      socket = new WebSocket(`${protocol}://${location.host}/api/ws/opportunities`);
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data) as { type: string; sequence: number; items: Opportunity[] };
        if (message.type === "snapshot") {
          setItems(message.items);
          lastSequence.current = message.sequence;
        } else if (lastSequence.current !== null && message.sequence !== lastSequence.current + 1) {
          void restore();
        } else {
          lastSequence.current = message.sequence;
          setItems((current) => {
            const merged = new Map(current.map((item) => [`${item.exchange}:${item.base_asset}`, item]));
            message.items.forEach((item) => merged.set(`${item.exchange}:${item.base_asset}`, item));
            return [...merged.values()];
          });
        }
      };
      socket.onclose = () => {
        if (!stopped) {
          void restore();
          reconnectTimer = window.setTimeout(connect, 2000);
        }
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  useEffect(() => {
    if (!selected) return;
    api.history(selected, range).then((value) => setHistory(value.items)).catch((reason: Error) => setError(reason.message));
  }, [selected, range]);

  const filtered = useMemo(() => items
    .filter((item) => exchange === "all" || item.exchange === exchange)
    .filter((item) => quality === "all" || item.quality === quality)
    .filter((item) => item.base_asset.includes(search.trim().toUpperCase()))
    .sort((a, b) => Number(b.net_return ?? b.current_apr) - Number(a.net_return ?? a.current_apr)), [items, exchange, quality, search]);

  const healthy = items.filter((item) => item.quality === "healthy");
  const best = healthy.reduce<Opportunity | null>((current, item) => !current || Number(item.net_return ?? -999) > Number(current.net_return ?? -999) ? item : current, null);
  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><div className="mark"><span /></div><div><strong>BASIS HAWK</strong><small>FUNDING ARBITRAGE RADAR</small></div></div>
      <div className="top-actions"><span className="read-only"><i /> {username}</span><button className="button secondary" onClick={() => setShowSettings(true)}>扫描设置</button><button className="button secondary" onClick={() => void api.logout().finally(onLogout)}>退出</button></div>
    </header>

    <main>
      <section className="hero">
        <div><p className="eyebrow">LIVE MARKET OVERVIEW</p><h1>资金费机会，一眼看清。</h1><p>同所现货多头 × USDT 永续空头。基差与资金费分开衡量，收益估算透明可核。</p></div>
        <div className="hero-grid">
          <div className="metric"><span>有效机会</span><strong>{healthy.length}</strong><small>六所共同交易对</small></div>
          <div className="metric accent"><span>最佳 30 天估算</span><strong>{percent(best?.net_return ?? null)}</strong><small>{best ? `${exchangeNames[best.exchange]} · ${best.base_asset}` : "等待历史预热"}</small></div>
          <div className="metric"><span>扫描标的</span><strong>{items.length}</strong><small>5 秒价格刷新</small></div>
        </div>
      </section>

      <section className="status-strip">
        {exchanges.map((name) => {
          const status = statuses.find((item) => item.exchange === name);
          return <div className="exchange-status" key={name}><span className={`status-dot ${status?.state ?? "starting"}`} /><div><strong>{exchangeNames[name]}</strong><small>{status ? `${status.instruments} 标的 · ${status.latency_ms ?? "—"}ms` : "正在连接"}</small></div></div>;
        })}
      </section>

      {error && <div className="error-banner">{error}<button onClick={() => setError(null)}>×</button></div>}

      <section className="workspace">
        <div className="table-card">
          <header className="section-header"><div><p className="eyebrow">OPPORTUNITY RANKING</p><h2>机会排行榜</h2></div><span className="count">{filtered.length} 项</span></header>
          <div className="filters">
            <input className="search" placeholder="搜索币种，例如 BTC" value={search} onChange={(e) => setSearch(e.target.value)} />
            <select value={exchange} onChange={(e) => setExchange(e.target.value as Exchange | "all")}><option value="all">全部交易所</option>{Object.entries(exchangeNames).map(([key, value]) => <option value={key} key={key}>{value}</option>)}</select>
            <select value={quality} onChange={(e) => setQuality(e.target.value as Quality | "all")}><option value="healthy">仅有效</option><option value="all">全部状态</option><option value="warming">预热中</option><option value="stale">已陈旧</option></select>
          </div>
          <div className="table-wrap"><table><thead><tr><th>标的</th><th>当前费率 / 周期</th><th>当前年化</th><th>24h 年化</th><th>7d 年化</th><th>30d 净收益</th><th>可执行基差</th><th>两腿成交额</th><th>状态</th></tr></thead>
            <tbody>{filtered.map((item) => <tr key={`${item.exchange}:${item.base_asset}`} className={selected?.exchange === item.exchange && selected.base_asset === item.base_asset ? "selected" : ""} onClick={() => setSelected(item)}>
              <td><div className="asset"><strong>{item.base_asset}</strong><span>{exchangeNames[item.exchange]}</span></div></td>
              <td><strong className={Number(item.current_funding_rate) >= 0 ? "positive" : "negative"}>{percent(item.current_funding_rate, 4)}</strong><small className="cell-note">每 {item.funding_interval_hours}h</small></td>
              <td>{percent(item.current_apr)}</td><td>{percent(item.apr_24h)}</td><td>{percent(item.apr_7d)}</td>
              <td><strong className={Number(item.net_return ?? 0) >= 0 ? "positive" : "negative"}>{percent(item.net_return)}</strong></td>
              <td>{percent(item.executable_basis, 3)}</td><td><span className="volume">{money(String(Math.min(Number(item.spot_quote_volume_24h), Number(item.perp_quote_volume_24h))))}</span></td>
              <td><span className={`quality ${item.quality}`}>{qualityNames[item.quality]}</span></td>
            </tr>)}</tbody>
          </table>{!filtered.length && <div className="empty">当前筛选条件下没有机会</div>}</div>
        </div>

        <aside className={`detail-card ${selected ? "open" : ""}`}>
          {selected ? <>
            <header><div><p className="eyebrow">OPPORTUNITY DETAIL</p><h2>{selected.base_asset} <span>{exchangeNames[selected.exchange]}</span></h2></div><button className="icon-button" onClick={() => setSelected(null)}>×</button></header>
            <div className="range-tabs">{["24h", "7d", "30d"].map((value) => <button className={range === value ? "active" : ""} onClick={() => setRange(value)} key={value}>{value}</button>)}</div>
            <Sparkline values={history.map((item) => Number(item.net_return ?? item.current_apr))} />
            <div className="detail-metrics"><div><span>现货买一</span><strong>{price(selected.spot_ask)}</strong></div><div><span>永续卖一</span><strong>{price(selected.perp_bid)}</strong></div><div><span>最优档容量</span><strong>{Number(selected.top_book_notional) ? `${money(selected.top_book_notional)} USDT` : "—"}</strong></div><div><span>下次结算</span><strong>{selected.next_funding_at ? new Date(selected.next_funding_at).toLocaleString("zh-CN") : "交易所未批量提供"}</strong></div></div>
            <div className="formula"><span>30 天净收益估算</span><code>7d 日均资金费 × {settings?.holding_period_days ?? 30} 天 − 开平双边 Taker 费</code><p>不包含基差收敛、滑点、税费、借贷与保证金机会成本。</p></div>
          </> : <div className="detail-placeholder"><div className="radar-icon" /><h3>选择一个机会</h3><p>查看价格、费用假设和历史趋势。</p></div>}
        </aside>
      </section>
    </main>
    <footer className="page-footer"><span>Basis Hawk · Local-first & read-only</span><span>收益估算不构成投资建议</span></footer>
    {showSettings && settings && <SettingsPanel value={settings} onClose={() => setShowSettings(false)} onSave={async (value) => setSettings(await api.saveSettings(value))} />}
  </div>;
}
