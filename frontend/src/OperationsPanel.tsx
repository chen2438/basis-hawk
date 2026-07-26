import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  AccountSnapshot,
  AuditEvent,
  AutoStrategyConfig,
  AutomationStatus,
  BackupStatus,
  CredentialSummary,
  Environment,
  Exchange,
  ExecutionStatus,
  FillHistoryItem,
  InternalTransfer,
  LiveClosePreview,
  LiveOpenPreview,
  NotificationHistoryItem,
  Opportunity,
  OrderHistoryItem,
  PairedPosition,
  PnlRealization,
  TradeIntent,
} from "./types";

const exchangeNames: Record<Exchange, string> = {
  binance: "Binance",
  okx: "OKX",
  mexc: "MEXC",
  bybit: "Bybit",
  bitget: "Bitget",
  gate: "Gate",
};
const exchanges = Object.keys(exchangeNames) as Exchange[];
type Tab = "system" | "accounts" | "trades" | "positions" | "ledger" | "transfers" | "automation" | "history";

const time = (value: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN") : "—";
const amount = (value: string | null) =>
  value == null ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 8 });

export function OperationsPanel({
  onClose,
  opportunities,
}: {
  onClose: () => void;
  opportunities: Opportunity[];
}) {
  const [tab, setTab] = useState<Tab>("system");
  const [execution, setExecution] = useState<ExecutionStatus | null>(null);
  const [credentials, setCredentials] = useState<CredentialSummary[]>([]);
  const [positions, setPositions] = useState<PairedPosition[]>([]);
  const [tradeIntents, setTradeIntents] = useState<TradeIntent[]>([]);
  const [orders, setOrders] = useState<OrderHistoryItem[]>([]);
  const [fills, setFills] = useState<FillHistoryItem[]>([]);
  const [pnlRealizations, setPnlRealizations] = useState<PnlRealization[]>([]);
  const [transfers, setTransfers] = useState<InternalTransfer[]>([]);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [notifications, setNotifications] = useState<NotificationHistoryItem[]>([]);
  const [backup, setBackup] = useState<BackupStatus | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, AccountSnapshot>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [
      executionValue,
      credentialValue,
      positionValue,
      intentValue,
      orderValue,
      fillValue,
      pnlValue,
      transferValue,
      automationValue,
      auditValue,
      notificationValue,
      backupValue,
    ] =
      await Promise.all([
        api.execution(),
        api.credentials(),
        api.positions(),
        api.tradeIntents(),
        api.orders(),
        api.fills(),
        api.pnlRealizations(),
        api.transfers(),
        api.automation(),
        api.auditHistory(),
        api.notificationHistory(),
        api.backupStatus(),
      ]);
    setExecution(executionValue);
    setCredentials(credentialValue.items);
    setPositions(positionValue.items);
    setTradeIntents(intentValue.items);
    setOrders(orderValue.items);
    setFills(fillValue.items);
    setPnlRealizations(pnlValue.items);
    setTransfers(transferValue.items);
    setAutomation(automationValue);
    setAuditEvents(auditValue.items);
    setNotifications(notificationValue.items);
    setBackup(backupValue);
  }, []);

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message));
  }, [refresh]);

  const action = async (operation: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  return <div className="operations-backdrop">
    <section className="operations-panel" aria-label="运营控制台">
      <header className="operations-header">
        <div><p className="eyebrow">OPERATIONS CONSOLE</p><h2>实盘运营控制台</h2></div>
        <div className="operations-header-actions">
          {execution?.state === "paused" && <span className="live-badge danger">交易已暂停</span>}
          {execution?.state === "ready" && <span className="live-badge">执行就绪</span>}
          <button className="icon-button" onClick={onClose} aria-label="关闭">×</button>
        </div>
      </header>
      <nav className="operations-tabs">
        {([
          ["system", "执行状态"],
          ["accounts", "交易所账户"],
          ["trades", "手动交易"],
          ["positions", "配对持仓"],
          ["ledger", "交易账本"],
          ["transfers", "内部划转"],
          ["automation", "自动策略"],
          ["history", "审计与通知"],
        ] as [Tab, string][]).map(([key, label]) =>
          <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}</button>
        )}
      </nav>
      {error && <div className="error-banner">{error}<button onClick={() => setError(null)}>×</button></div>}
      <div className="operations-content">
        {tab === "system" && <SystemView
          execution={execution}
          backup={backup}
          busy={busy}
          action={action}
        />}
        {tab === "accounts" && <AccountsView
          credentials={credentials}
          snapshots={snapshots}
          setSnapshots={setSnapshots}
          busy={busy}
          action={action}
        />}
        {tab === "trades" && <TradesView
          opportunities={opportunities}
          positions={positions}
          execution={execution}
          busy={busy}
          action={action}
        />}
        {tab === "positions" && <PositionsView positions={positions} />}
        {tab === "ledger" && <TradeLedgerView
          intents={tradeIntents}
          orders={orders}
          fills={fills}
          pnlRealizations={pnlRealizations}
        />}
        {tab === "transfers" && <TransfersView
          transfers={transfers}
          execution={execution}
          busy={busy}
          action={action}
        />}
        {tab === "automation" && <AutomationView
          automation={automation}
          execution={execution}
          busy={busy}
          action={action}
        />}
        {tab === "history" && <HistoryView
          auditEvents={auditEvents}
          notifications={notifications}
          busy={busy}
          action={action}
        />}
      </div>
    </section>
  </div>;
}

function SystemView({
  execution,
  backup,
  busy,
  action,
}: {
  execution: ExecutionStatus | null;
  backup: BackupStatus | null;
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  if (!execution) return <p className="loading-note">正在读取 worker 状态…</p>;
  return <>
    <div className="ops-summary">
      <div><span>全局执行</span><strong className={`state-text ${execution.state}`}>{execution.state}</strong></div>
      <div><span>账户数</span><strong>{execution.accounts.length}</strong></div>
      <div><span>最近更新</span><strong>{time(execution.updated_at)}</strong></div>
    </div>
    <div className="ops-summary">
      <div><span>加密备份数</span><strong>{backup?.archive_count ?? "—"}</strong></div>
      <div><span>最近备份</span><strong>{time(backup?.latest?.modified_at ?? null)}</strong></div>
      <div><span>校验文件</span><strong>{backup?.latest ? (backup.latest.checksum_present ? "存在" : "缺失") : "—"}</strong></div>
    </div>
    <div className="safety-callout"><strong>当前原因</strong><p>{execution.reason}</p>
      <div className="inline-actions">
        <button className="button danger" disabled={busy || execution.state === "paused"} onClick={() => {
          if (window.confirm("确认暂停新交易并让 worker 撤销所有远端活动订单？")) {
            void action(() => api.pauseExecution("execution paused from web console"));
          }
        }}>暂停执行</button>
        <button className="button primary" disabled={busy || execution.state !== "paused"} onClick={() => {
          if (window.confirm("确认请求全量安全对账？该操作不会直接把状态改为 ready。")) {
            void action(api.resumeExecution);
          }
        }}>重新对账</button>
      </div>
    </div>
    <div className="ops-grid">
      {execution.accounts.map((item) => <article className="ops-card" key={`${item.exchange}:${item.environment}`}>
        <header><strong>{exchangeNames[item.exchange]}</strong><span className={`status-pill ${item.status}`}>{item.status}</span></header>
        <p>{item.environment.toUpperCase()} · {item.reason}</p>
        <dl>
          <div><dt>私有流</dt><dd>{item.private_stream_ready ? "正常" : "未就绪"}</dd></div>
          <div><dt>远端订单</dt><dd>{item.open_order_count}</dd></div>
          <div><dt>远端仓位</dt><dd>{item.position_count}</dd></div>
          <div><dt>已核成交</dt><dd>{item.fill_count}</dd></div>
        </dl>
      </article>)}
      {!execution.accounts.length && <div className="empty">尚未配置交易所账户</div>}
    </div>
  </>;
}

function AccountsView({
  credentials,
  snapshots,
  setSnapshots,
  busy,
  action,
}: {
  credentials: CredentialSummary[];
  snapshots: Record<string, AccountSnapshot>;
  setSnapshots: (value: Record<string, AccountSnapshot>) => void;
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  const [form, setForm] = useState({
    exchange: "binance" as Exchange,
    environment: "live" as Environment,
    label: "primary",
    apiKey: "",
    apiSecret: "",
    passphrase: "",
  });
  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!window.confirm(`确认保存 ${exchangeNames[form.exchange]} ${form.environment} 凭据？`)) return;
    void action(async () => {
      await api.saveCredential(form.exchange, form.environment, {
        label: form.label,
        api_key: form.apiKey,
        api_secret: form.apiSecret,
        ...(form.passphrase ? { passphrase: form.passphrase } : {}),
      });
      setForm((current) => ({ ...current, apiKey: "", apiSecret: "", passphrase: "" }));
    });
  };
  return <>
    <div className="ops-grid">
      {credentials.map((item) => {
        const key = `${item.exchange}:${item.environment}`;
        const snapshot = snapshots[key];
        return <article className="ops-card account-card" key={key}>
          <header><strong>{exchangeNames[item.exchange]}</strong><span className={`live-badge ${item.environment === "live" ? "danger" : ""}`}>{item.environment}</span></header>
          <p>{item.label} · {item.masked_api_key}</p>
          {snapshot && <dl>
            <div><dt>现货可用</dt><dd>{amount(snapshot.spot_usdt_available)} USDT</dd></div>
            <div><dt>永续可用</dt><dd>{amount(snapshot.perp_usdt_available)} USDT</dd></div>
            <div><dt>账户模式</dt><dd>{snapshot.account_mode}</dd></div>
            <div><dt>交易权限</dt><dd>{snapshot.trade_permission === true ? "已确认" : "未确认"}</dd></div>
          </dl>}
          <div className="inline-actions">
            <button className="button secondary" disabled={busy} onClick={() => void action(async () => {
              const value = await api.accountSnapshot(item.exchange, item.environment);
              setSnapshots({ ...snapshots, [key]: value });
            })}>测试并读取余额</button>
            <button className="button danger ghost" disabled={busy} onClick={() => {
              if (window.confirm(`确认删除 ${exchangeNames[item.exchange]} ${item.environment} 凭据？`)) {
                void action(() => api.deleteCredential(item.exchange, item.environment));
              }
            }}>删除</button>
          </div>
        </article>;
      })}
    </div>
    <form className="ops-form" onSubmit={save}>
      <h3>保存或替换凭据</h3>
      <p>仅填写读取、交易和内部划转权限的 Key；禁止提现，并绑定 VPS 出口 IP。</p>
      <div className="form-grid compact">
        <label>交易所<select value={form.exchange} onChange={(event) => setForm({ ...form, exchange: event.target.value as Exchange })}>{exchanges.map((item) => <option key={item} value={item}>{exchangeNames[item]}</option>)}</select></label>
        <label>环境<select value={form.environment} onChange={(event) => setForm({ ...form, environment: event.target.value as Environment })}><option value="live">LIVE 实盘</option><option value="sandbox">SANDBOX</option></select></label>
        <label>标签<input required value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} /></label>
        <label>API Key<input required autoComplete="off" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} /></label>
        <label>API Secret<input required type="password" autoComplete="new-password" value={form.apiSecret} onChange={(event) => setForm({ ...form, apiSecret: event.target.value })} /></label>
        <label>Passphrase（OKX/Bitget）<input type="password" autoComplete="new-password" value={form.passphrase} onChange={(event) => setForm({ ...form, passphrase: event.target.value })} /></label>
      </div>
      <button className="button primary" disabled={busy}>加密保存凭据</button>
    </form>
  </>;
}

function TradesView({
  opportunities,
  positions,
  execution,
  busy,
  action,
}: {
  opportunities: Opportunity[];
  positions: PairedPosition[];
  execution: ExecutionStatus | null;
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  const healthy = opportunities.filter((item) => item.quality === "healthy");
  const initial = healthy[0];
  const [openForm, setOpenForm] = useState({
    exchange: initial?.exchange ?? "binance" as Exchange,
    environment: "live" as Environment,
    baseAsset: initial?.base_asset ?? "",
    notional: "100",
    leverage: 1,
    slippage: "0.001",
  });
  const [openTicket, setOpenTicket] = useState<{ id: string; preview: LiveOpenPreview } | null>(null);
  const [closeTicket, setCloseTicket] = useState<{ id: string; preview: LiveClosePreview } | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const exchangeOpportunities = healthy.filter((item) => item.exchange === openForm.exchange);
  const openPositions = positions.filter((item) => item.status === "open" && item.environment !== "paper");

  const previewOpen = (event: FormEvent) => {
    event.preventDefault();
    setResult(null);
    void action(async () => {
      const value = await api.previewOpen({
        exchange: openForm.exchange,
        environment: openForm.environment,
        base_asset: openForm.baseAsset,
        notional_usdt: openForm.notional,
        leverage: openForm.leverage,
        maximum_slippage: openForm.slippage,
      });
      setOpenTicket({ id: value.preview_id, preview: value.preview });
      setCloseTicket(null);
    });
  };
  const confirmOpen = () => {
    if (!openTicket || !window.confirm(
      `确认提交 ${exchangeNames[openTicket.preview.exchange]} ${openTicket.preview.base_asset} 配对开仓？`,
    )) return;
    void action(async () => {
      const value = await api.confirmOpen(openTicket.id, crypto.randomUUID());
      setResult(`开仓意图 ${value.intent.id} 已持久化，状态 ${value.intent.status}`);
      setOpenTicket(null);
    });
  };
  const previewClose = (
    position: PairedPosition,
    emergency: boolean,
  ) => {
    setResult(null);
    void action(async () => {
      const value = await api.previewClose(
        position.id,
        emergency,
        emergency ? "0.05" : "0.001",
      );
      setCloseTicket({ id: value.preview_id, preview: value.preview });
      setOpenTicket(null);
    });
  };
  const confirmClose = () => {
    if (!closeTicket || !window.confirm(
      `确认提交 ${closeTicket.preview.base_asset} ${closeTicket.preview.emergency ? "紧急" : "普通"}配对平仓？`,
    )) return;
    void action(async () => {
      const value = await api.confirmClose(
        closeTicket.preview.position_id,
        closeTicket.id,
        crypto.randomUUID(),
      );
      setResult(`平仓意图 ${value.intent.id} 已持久化，状态 ${value.intent.status}`);
      setCloseTicket(null);
    });
  };

  return <>
    {execution?.state !== "ready" && <div className="safety-callout warning"><strong>当前不可普通开仓</strong><p>全局执行状态为 {execution?.state ?? "unknown"}。紧急平仓仍由后端按独立安全规则判断。</p></div>}
    {result && <div className="success-banner">{result}</div>}
    <form className="ops-form trade-form" onSubmit={previewOpen}>
      <div><h3>手动配对开仓</h3><p>先生成 15 秒预览票据；确认只持久化意图，由唯一 worker 提交双腿 IOC。</p></div>
      <label>交易所<select value={openForm.exchange} onChange={(event) => {
        const next = event.target.value as Exchange;
        setOpenForm({ ...openForm, exchange: next, baseAsset: healthy.find((item) => item.exchange === next)?.base_asset ?? "" });
        setOpenTicket(null);
      }}>{exchanges.map((item) => <option key={item} value={item}>{exchangeNames[item]}</option>)}</select></label>
      <label>环境<select value={openForm.environment} onChange={(event) => setOpenForm({ ...openForm, environment: event.target.value as Environment })}><option value="live">LIVE 实盘</option><option value="sandbox">SANDBOX</option></select></label>
      <label>标的<select required value={openForm.baseAsset} onChange={(event) => setOpenForm({ ...openForm, baseAsset: event.target.value })}><option value="">请选择</option>{exchangeOpportunities.map((item) => <option key={item.base_asset} value={item.base_asset}>{item.base_asset}</option>)}</select></label>
      <label>名义金额<input required min="0" step="any" value={openForm.notional} onChange={(event) => setOpenForm({ ...openForm, notional: event.target.value })} /></label>
      <label>杠杆<input required type="number" min="1" max="10" value={openForm.leverage} onChange={(event) => setOpenForm({ ...openForm, leverage: Number(event.target.value) })} /></label>
      <label>最大滑点<input required min="0" max="0.1" step="0.0001" value={openForm.slippage} onChange={(event) => setOpenForm({ ...openForm, slippage: event.target.value })} /></label>
      <button className="button primary" disabled={busy || execution?.state !== "ready" || !openForm.baseAsset}>生成开仓预览</button>
    </form>
    {openTicket && <TradePreview title="开仓预览" expiresAt={openTicket.preview.expires_at} rows={[
      ["现货腿", `${openTicket.preview.spot_quantity} ${openTicket.preview.spot_symbol} @ ${openTicket.preview.spot_limit_price}`],
      ["永续腿", `${openTicket.preview.perp_quantity} ${openTicket.preview.perp_symbol} @ ${openTicket.preview.perp_limit_price}`],
      ["共同数量", openTicket.preview.base_quantity],
      ["现货余额需求", `${openTicket.preview.spot_usdt_required} USDT`],
      ["永续保证金需求", `${openTicket.preview.perp_usdt_margin_required} USDT`],
      ["预计总费用", `${openTicket.preview.estimated_total_fees_usdt} USDT`],
      ["最坏基差", `${(Number(openTicket.preview.worst_case_basis) * 100).toFixed(4)}%`],
    ]} danger={openTicket.preview.environment === "live"} onConfirm={confirmOpen} busy={busy} />}
    <section className="close-position-list">
      <header><div><h3>真实配对持仓平仓</h3><p>普通平仓要求 ready；紧急平仓独立标记并保持全局暂停。</p></div></header>
      {openPositions.map((position) => <article key={position.id}>
        <div><strong>{position.base_asset}</strong><span>{exchangeNames[position.exchange]} · {position.environment} · {position.quantity}</span></div>
        <div className="inline-actions">
          <button className="button secondary" disabled={busy || execution?.state !== "ready"} onClick={() => previewClose(position, false)}>普通平仓预览</button>
          <button className="button danger ghost" disabled={busy} onClick={() => previewClose(position, true)}>紧急平仓预览</button>
        </div>
      </article>)}
      {!openPositions.length && <div className="empty">当前没有可平的真实配对仓位</div>}
    </section>
    {closeTicket && <TradePreview title={closeTicket.preview.emergency ? "紧急平仓预览" : "平仓预览"} expiresAt={closeTicket.preview.expires_at} rows={[
      ["现货腿", `${closeTicket.preview.spot_quantity} ${closeTicket.preview.spot_symbol} @ ${closeTicket.preview.spot_limit_price}`],
      ["永续腿", `${closeTicket.preview.perp_quantity} ${closeTicket.preview.perp_symbol} @ ${closeTicket.preview.perp_limit_price} · reduce-only`],
      ["共同数量", closeTicket.preview.base_quantity],
      ["预计毛盈亏", `${closeTicket.preview.estimated_gross_pnl_usdt} USDT`],
      ["预计净盈亏", `${closeTicket.preview.estimated_net_pnl_usdt} USDT`],
      ["预计总费用", `${closeTicket.preview.estimated_total_fees_usdt} USDT`],
      ["最坏基差", `${(Number(closeTicket.preview.worst_case_basis) * 100).toFixed(4)}%`],
    ]} danger={closeTicket.preview.environment === "live"} onConfirm={confirmClose} busy={busy} />}
  </>;
}

function TradePreview({
  title,
  expiresAt,
  rows,
  danger,
  onConfirm,
  busy,
}: {
  title: string;
  expiresAt: string;
  rows: [string, string][];
  danger: boolean;
  onConfirm: () => void;
  busy: boolean;
}) {
  return <section className={`trade-preview ${danger ? "live" : ""}`}>
    <header><div><h3>{title}</h3><p>票据到期：{time(expiresAt)}</p></div>{danger && <span className="live-badge danger">LIVE 实盘</span>}</header>
    <dl>{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <div className="preview-warning">确认后不会在浏览器直接发单；唯一 worker 会再次预检账户、余额、仓位模式和远端状态。</div>
    <button className="button danger" disabled={busy} onClick={onConfirm}>普通确认并持久化意图</button>
  </section>;
}

function PositionsView({ positions }: { positions: PairedPosition[] }) {
  return <div className="ops-table-wrap"><table><thead><tr><th>标的</th><th>环境</th><th>数量</th><th>现货开仓</th><th>永续开仓</th><th>已实现 PnL</th><th>状态</th><th>开仓时间</th></tr></thead>
    <tbody>{positions.map((item) => <tr key={item.id}>
      <td><div className="asset"><strong>{item.base_asset}</strong><span>{exchangeNames[item.exchange]}</span></div></td>
      <td>{item.environment}</td><td>{amount(item.quantity)}</td><td>{amount(item.spot_entry_price)}</td><td>{amount(item.perp_entry_price)}</td>
      <td className={Number(item.realized_pnl_usdt ?? 0) >= 0 ? "positive" : "negative"}>{amount(item.realized_pnl_usdt)}</td>
      <td><span className={`status-pill ${item.status}`}>{item.status}</span></td><td>{time(item.opened_at)}</td>
    </tr>)}</tbody>
  </table>{!positions.length && <div className="empty">当前没有配对持仓</div>}</div>;
}

function TradeLedgerView({
  intents,
  orders,
  fills,
  pnlRealizations,
}: {
  intents: TradeIntent[];
  orders: OrderHistoryItem[];
  fills: FillHistoryItem[];
  pnlRealizations: PnlRealization[];
}) {
  return <div className="history-grid trade-ledger">
    <section>
      <header><div><h3>交易意图</h3><p>最近 100 条持久化开平仓请求。</p></div><strong>{intents.length}</strong></header>
      <div className="ops-table-wrap"><table><thead><tr><th>时间</th><th>交易所</th><th>环境</th><th>标的</th><th>动作</th><th>名义额</th><th>状态</th></tr></thead>
        <tbody>{intents.map((item) => <tr key={item.id}>
          <td>{time(item.created_at)}</td><td>{exchangeNames[item.exchange]}</td><td>{item.environment}</td>
          <td>{item.base_asset}</td><td>{item.emergency ? `紧急${item.action}` : item.action}</td>
          <td>{amount(item.requested_notional)} USDT</td><td><span className={`status-pill ${item.status}`}>{item.status}</span></td>
        </tr>)}</tbody>
      </table>{!intents.length && <div className="empty">尚无交易意图</div>}</div>
    </section>
    <section>
      <header><div><h3>订单腿</h3><p>最近 100 条现货、永续及补偿订单状态。</p></div><strong>{orders.length}</strong></header>
      <div className="ops-table-wrap"><table><thead><tr><th>更新时间</th><th>交易所</th><th>标的</th><th>订单腿</th><th>方向</th><th>数量</th><th>成交</th><th>均价</th><th>状态</th></tr></thead>
        <tbody>{orders.map((item) => <tr key={item.id}>
          <td>{time(item.updated_at)}</td><td>{exchangeNames[item.exchange]}</td><td>{item.base_asset}</td>
          <td>{item.leg} · {item.symbol}</td><td>{item.side}{item.reduce_only ? " · reduce-only" : ""}</td>
          <td>{amount(item.quantity)}</td><td>{amount(item.filled_quantity)}</td><td>{amount(item.average_price)}</td>
          <td><span className={`status-pill ${item.status}`}>{item.status}</span></td>
        </tr>)}</tbody>
      </table>{!orders.length && <div className="empty">尚无订单记录</div>}</div>
    </section>
    <section>
      <header><div><h3>成交明细</h3><p>最近 100 条按交易所成交 ID 去重的实际或纸面成交。</p></div><strong>{fills.length}</strong></header>
      <div className="ops-table-wrap"><table><thead><tr><th>时间</th><th>交易所</th><th>标的</th><th>订单腿</th><th>方向</th><th>数量</th><th>价格</th><th>费用</th><th>流动性</th></tr></thead>
        <tbody>{fills.map((item) => <tr key={item.id}>
          <td>{time(item.occurred_at)}</td><td>{exchangeNames[item.exchange]}</td><td>{item.base_asset}</td>
          <td>{item.leg} · {item.symbol}</td><td>{item.side}</td><td>{amount(item.quantity)}</td>
          <td>{amount(item.price)}</td><td>{amount(item.fee_amount)} {item.fee_asset}</td><td>{item.liquidity}</td>
        </tr>)}</tbody>
      </table>{!fills.length && <div className="empty">尚无成交记录</div>}</div>
    </section>
    <section>
      <header><div><h3>已实现盈亏</h3><p>最近 100 条平仓结算；费用与毛盈亏分别可审计。</p></div><strong>{pnlRealizations.length}</strong></header>
      <div className="ops-table-wrap"><table><thead><tr><th>时间</th><th>交易所</th><th>环境</th><th>标的</th><th>数量</th><th>毛盈亏</th><th>开仓费</th><th>平仓费</th><th>净盈亏</th></tr></thead>
        <tbody>{pnlRealizations.map((item) => <tr key={item.id}>
          <td>{time(item.realized_at)}</td><td>{exchangeNames[item.exchange]}</td><td>{item.environment}</td>
          <td>{item.base_asset}</td><td>{amount(item.quantity)}</td><td>{amount(item.gross_pnl_usdt)}</td>
          <td>{amount(item.opening_fee_allocated_usdt)}</td><td>{amount(item.closing_fees_usdt)}</td>
          <td className={Number(item.net_pnl_usdt) >= 0 ? "positive" : "negative"}>{amount(item.net_pnl_usdt)} USDT</td>
        </tr>)}</tbody>
      </table>{!pnlRealizations.length && <div className="empty">尚无已实现盈亏</div>}</div>
    </section>
  </div>;
}

function TransfersView({
  transfers,
  execution,
  busy,
  action,
}: {
  transfers: InternalTransfer[];
  execution: ExecutionStatus | null;
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  const [form, setForm] = useState({
    exchange: "binance" as Exchange,
    environment: "live" as Environment,
    direction: "spot_to_perp" as "spot_to_perp" | "perp_to_spot",
    amount: "",
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!window.confirm(`确认在 ${exchangeNames[form.exchange]} 内部划转 ${form.amount} USDT？提交后全局交易会暂停。`)) return;
    void action(() => api.createTransfer({
      exchange: form.exchange,
      environment: form.environment,
      direction: form.direction,
      amount_usdt: form.amount,
      confirmed: true,
    }, crypto.randomUUID()));
  };
  return <>
    <form className="ops-form transfer-form" onSubmit={submit}>
      <div><h3>新建 USDT 内部划转</h3><p>仅同所现货与 USDT 永续；不支持提现、地址、链或跨账户目标。</p></div>
      <select value={form.exchange} onChange={(event) => setForm({ ...form, exchange: event.target.value as Exchange })}>{exchanges.map((item) => <option key={item} value={item}>{exchangeNames[item]}</option>)}</select>
      <select value={form.environment} onChange={(event) => setForm({ ...form, environment: event.target.value as Environment })}><option value="live">LIVE</option><option value="sandbox">SANDBOX</option></select>
      <select value={form.direction} onChange={(event) => setForm({ ...form, direction: event.target.value as typeof form.direction })}><option value="spot_to_perp">现货 → 永续</option><option value="perp_to_spot">永续 → 现货</option></select>
      <input required min="0" step="any" placeholder="USDT 金额" value={form.amount} onChange={(event) => setForm({ ...form, amount: event.target.value })} />
      <button className="button primary" disabled={busy || execution?.state !== "ready"}>确认并暂停执行</button>
    </form>
    {execution?.state !== "ready" && <p className="loading-note">只有全局执行为 ready 时才能新建划转；同一幂等请求仍可由 API 安全重试。</p>}
    <div className="ops-table-wrap"><table><thead><tr><th>交易所</th><th>方向</th><th>金额</th><th>状态</th><th>目标余额</th><th>错误码</th><th>更新时间</th></tr></thead>
      <tbody>{transfers.map((item) => <tr key={item.id}>
        <td><div className="asset"><strong>{exchangeNames[item.exchange]}</strong><span>{item.environment}</span></div></td>
        <td>{item.direction === "spot_to_perp" ? "现货 → 永续" : "永续 → 现货"}</td><td>{amount(item.amount_usdt)} USDT</td>
        <td><span className={`status-pill ${item.status}`}>{item.status}</span></td><td>{amount(item.expected_target_balance)}</td>
        <td>{item.error_code ?? "—"}</td><td>{time(item.updated_at)}</td>
      </tr>)}</tbody>
    </table>{!transfers.length && <div className="empty">尚无内部划转记录</div>}</div>
  </>;
}

function AutomationView({
  automation,
  execution,
  busy,
  action,
}: {
  automation: AutomationStatus | null;
  execution: ExecutionStatus | null;
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  const [form, setForm] = useState<AutoStrategyConfig>(() => strategyForm());
  useEffect(() => {
    setForm(strategyForm(automation?.latest_strategy?.config));
  }, [automation?.latest_strategy?.id]);
  if (!automation) return <p className="loading-note">正在读取自动策略…</p>;
  const strategy = automation.latest_strategy;
  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!window.confirm("确认保存为新的不可变策略版本？保存不会自动启用交易。")) return;
    void action(() => api.saveAutomationConfig(form));
  };
  const setDecimal = (name: keyof AutoStrategyConfig, value: string) =>
    setForm({ ...form, [name]: value });
  const setInteger = (name: keyof AutoStrategyConfig, value: string) =>
    setForm({ ...form, [name]: Number(value) });
  const toggleExchange = (exchange: Exchange) => {
    const selected = form.enabled_exchanges.includes(exchange);
    setForm({
      ...form,
      enabled_exchanges: selected
        ? form.enabled_exchanges.filter((item) => item !== exchange)
        : [...form.enabled_exchanges, exchange],
    });
  };
  return <>
    <div className="ops-summary">
      <div><span>自动状态</span><strong className={`state-text ${automation.state}`}>{automation.state}</strong></div>
      <div><span>生效版本</span><strong>{automation.active_strategy?.version ?? "—"}</strong></div>
      <div><span>最近更新</span><strong>{time(automation.updated_at)}</strong></div>
    </div>
    <div className="safety-callout"><strong>状态说明</strong><p>{automation.reason}</p>
      <div className="inline-actions">
        {automation.state === "enabled" ? <button className="button danger" disabled={busy} onClick={() => void action(() => api.pauseAutomation("paused from web console"))}>暂停自动交易</button>
          : automation.state === "paused" && automation.active_strategy ? <button className="button primary" disabled={busy || execution?.state !== "ready"} onClick={() => {
            if (window.confirm(`确认恢复策略版本 ${automation.active_strategy?.version}？`)) void action(api.resumeAutomation);
          }}>恢复自动交易</button>
            : <button className="button primary" disabled={busy || !strategy || execution?.state !== "ready"} onClick={() => {
              if (strategy && window.confirm(`确认启用策略版本 ${strategy.version}？`)) void action(() => api.enableAutomation(strategy.id));
            }}>启用最新策略</button>}
        <button className="button secondary" disabled={busy || automation.state === "disabled"} onClick={() => {
          if (window.confirm("确认禁用自动交易？既有对冲仓位不会被自动清仓。")) void action(api.disableAutomation);
        }}>禁用</button>
      </div>
    </div>
    {execution?.state !== "ready" && <p className="loading-note">当前全局执行不是 ready：可以保存新版本，但不能启用或恢复自动交易。</p>}
    <form className="strategy-editor" onSubmit={save}>
      <header><div><h3>自动策略完整配置</h3><p>比例均填写小数，例如 0.10 表示 10%。每次保存都会创建新版本，不修改历史版本。</p></div>
        <button className="button secondary" disabled={busy || !form.enabled_exchanges.length}>保存新版本</button>
      </header>
      <section>
        <h4>环境与交易所</h4>
        <div className="strategy-field-grid">
          <label>环境<select value={form.environment} onChange={(event) => setForm({ ...form, environment: event.target.value as Environment })}><option value="live">LIVE 实盘</option><option value="sandbox">SANDBOX</option></select></label>
          <div className="exchange-checks">{exchanges.map((exchange) => <label key={exchange}><input type="checkbox" checked={form.enabled_exchanges.includes(exchange)} onChange={() => toggleExchange(exchange)} />{exchangeNames[exchange]}</label>)}</div>
        </div>
      </section>
      <StrategyFields title="资金与仓位" fields={[
        ["leverage", "杠杆", "number"], ["notional_per_trade", "每笔名义 USDT"], ["per_exchange_max_exposure", "单所最大敞口"],
        ["global_max_exposure", "全局最大敞口"], ["max_concurrent_positions", "最大并发仓位", "number"],
        ["minimum_two_leg_notional", "两腿最低成交额"], ["book_capacity_multiple", "盘口容量倍数"], ["daily_max_loss", "UTC 日最大亏损"],
      ]} form={form} setDecimal={setDecimal} setInteger={setInteger} />
      <StrategyFields title="开仓门槛" fields={[
        ["minimum_current_apr", "当前最低 APR"], ["minimum_apr_24h", "24h 最低 APR"], ["minimum_apr_7d", "7d 最低 APR"],
        ["minimum_net_return", "最低净收益"], ["maximum_opening_basis", "最大开仓基差"], ["normal_max_slippage", "普通最大滑点"],
        ["minimum_liquidation_buffer", "最低清算缓冲"],
      ]} form={form} setDecimal={setDecimal} setInteger={setInteger} />
      <StrategyFields title="退出与时间" fields={[
        ["minimum_reentry_minutes", "最短重入分钟", "number"], ["maximum_holding_hours", "最长持有小时", "number"],
        ["close_funding_rate_below", "费率低于此值平仓"], ["close_net_return_below", "净收益低于此值平仓"],
        ["close_basis_above", "平仓基差高于此值"], ["take_profit_usdt", "止盈 USDT"], ["stop_loss_usdt", "止损 USDT"],
        ["emergency_max_slippage", "紧急最大滑点"],
      ]} form={form} setDecimal={setDecimal} setInteger={setInteger} />
    </form>
    {strategy ? <article className="strategy-json"><header><strong>最新策略版本 {strategy.version}</strong><span>{strategy.environment.toUpperCase()} · {strategy.created_by}</span></header><pre>{JSON.stringify(strategy.config, null, 2)}</pre></article>
      : <div className="empty">尚未创建自动策略版本；自动交易保持 disabled。</div>}
  </>;
}

type StrategyField = [keyof AutoStrategyConfig, string, "number"?];

function StrategyFields({
  title,
  fields,
  form,
  setDecimal,
  setInteger,
}: {
  title: string;
  fields: StrategyField[];
  form: AutoStrategyConfig;
  setDecimal: (name: keyof AutoStrategyConfig, value: string) => void;
  setInteger: (name: keyof AutoStrategyConfig, value: string) => void;
}) {
  return <section><h4>{title}</h4><div className="strategy-field-grid">{fields.map(([name, label, type]) =>
    <label key={name}>{label}<input required type={type ?? "text"} step="any" value={String(form[name])}
      onChange={(event) => type === "number" ? setInteger(name, event.target.value) : setDecimal(name, event.target.value)} /></label>,
  )}</div></section>;
}

function strategyForm(value?: AutoStrategyConfig): AutoStrategyConfig {
  return value ? { ...value, enabled_exchanges: [...value.enabled_exchanges] } : {
    environment: "live",
    enabled_exchanges: ["binance"],
    leverage: 1,
    notional_per_trade: "100",
    per_exchange_max_exposure: "500",
    global_max_exposure: "1000",
    max_concurrent_positions: 5,
    minimum_current_apr: "0.10",
    minimum_apr_24h: "0.08",
    minimum_apr_7d: "0.05",
    minimum_net_return: "0.005",
    maximum_opening_basis: "0.02",
    minimum_two_leg_notional: "50",
    book_capacity_multiple: "2",
    normal_max_slippage: "0.001",
    emergency_max_slippage: "0.01",
    daily_max_loss: "50",
    minimum_reentry_minutes: 60,
    maximum_holding_hours: 720,
    minimum_liquidation_buffer: "0.20",
    close_funding_rate_below: "0",
    close_net_return_below: "0.001",
    close_basis_above: "0.03",
    take_profit_usdt: "25",
    stop_loss_usdt: "20",
  };
}

function HistoryView({
  auditEvents,
  notifications,
  busy,
  action,
}: {
  auditEvents: AuditEvent[];
  notifications: NotificationHistoryItem[];
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  return <div className="history-grid">
    <section>
      <header><div><h3>管理员审计</h3><p>最新 100 条；敏感键由服务端递归脱敏。</p></div><strong>{auditEvents.length}</strong></header>
      <div className="ops-table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>操作者</th><th>安全详情</th></tr></thead>
        <tbody>{auditEvents.map((item) => <tr key={item.id}>
          <td>{time(item.occurred_at)}</td><td><code>{item.event_type}</code></td><td>{item.actor}</td>
          <td><code className="details-json">{JSON.stringify(item.details)}</code></td>
        </tr>)}</tbody>
      </table>{!auditEvents.length && <div className="empty">尚无审计事件</div>}</div>
    </section>
    <section>
      <header><div><h3>通知投递</h3><p>只展示投递元数据，不返回消息正文或去重键。</p></div>
        <div className="inline-actions">
          <button className="button secondary" disabled={busy} onClick={() => {
            if (window.confirm("确认向已配置的 Telegram 发送测试通知？")) {
              void action(() => api.testNotifications(["telegram"]));
            }
          }}>测试 Telegram</button>
          <button className="button secondary" disabled={busy} onClick={() => {
            if (window.confirm("确认向已配置的邮箱发送测试通知？")) {
              void action(() => api.testNotifications(["email"]));
            }
          }}>测试邮件</button>
          <strong>{notifications.length}</strong>
        </div>
      </header>
      <div className="ops-table-wrap"><table><thead><tr><th>时间</th><th>通道</th><th>主题</th><th>级别</th><th>状态</th><th>尝试</th><th>错误码</th></tr></thead>
        <tbody>{notifications.map((item) => <tr key={item.id}>
          <td>{time(item.created_at)}</td><td>{item.channel}</td><td>{item.subject}</td>
          <td><span className={`status-pill ${item.severity}`}>{item.severity}</span></td>
          <td><span className={`status-pill ${item.status}`}>{item.status}</span></td><td>{item.attempts}</td>
          <td>{item.last_error_code ?? "—"}</td>
        </tr>)}</tbody>
      </table>{!notifications.length && <div className="empty">尚无通知投递记录</div>}</div>
    </section>
  </div>;
}
