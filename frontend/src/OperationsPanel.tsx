import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  AccountSnapshot,
  AutomationStatus,
  CredentialSummary,
  Environment,
  Exchange,
  ExecutionStatus,
  InternalTransfer,
  PairedPosition,
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
type Tab = "system" | "accounts" | "positions" | "transfers" | "automation";

const time = (value: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN") : "—";
const amount = (value: string | null) =>
  value == null ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 8 });

export function OperationsPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("system");
  const [execution, setExecution] = useState<ExecutionStatus | null>(null);
  const [credentials, setCredentials] = useState<CredentialSummary[]>([]);
  const [positions, setPositions] = useState<PairedPosition[]>([]);
  const [transfers, setTransfers] = useState<InternalTransfer[]>([]);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, AccountSnapshot>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [executionValue, credentialValue, positionValue, transferValue, automationValue] =
      await Promise.all([
        api.execution(),
        api.credentials(),
        api.positions(),
        api.transfers(),
        api.automation(),
      ]);
    setExecution(executionValue);
    setCredentials(credentialValue.items);
    setPositions(positionValue.items);
    setTransfers(transferValue.items);
    setAutomation(automationValue);
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
          ["positions", "配对持仓"],
          ["transfers", "内部划转"],
          ["automation", "自动策略"],
        ] as [Tab, string][]).map(([key, label]) =>
          <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}</button>
        )}
      </nav>
      {error && <div className="error-banner">{error}<button onClick={() => setError(null)}>×</button></div>}
      <div className="operations-content">
        {tab === "system" && <SystemView execution={execution} busy={busy} action={action} />}
        {tab === "accounts" && <AccountsView
          credentials={credentials}
          snapshots={snapshots}
          setSnapshots={setSnapshots}
          busy={busy}
          action={action}
        />}
        {tab === "positions" && <PositionsView positions={positions} />}
        {tab === "transfers" && <TransfersView
          transfers={transfers}
          execution={execution}
          busy={busy}
          action={action}
        />}
        {tab === "automation" && <AutomationView automation={automation} busy={busy} action={action} />}
      </div>
    </section>
  </div>;
}

function SystemView({
  execution,
  busy,
  action,
}: {
  execution: ExecutionStatus | null;
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
  busy,
  action,
}: {
  automation: AutomationStatus | null;
  busy: boolean;
  action: (operation: () => Promise<unknown>) => Promise<void>;
}) {
  if (!automation) return <p className="loading-note">正在读取自动策略…</p>;
  const strategy = automation.active_strategy ?? automation.latest_strategy;
  return <>
    <div className="ops-summary">
      <div><span>自动状态</span><strong className={`state-text ${automation.state}`}>{automation.state}</strong></div>
      <div><span>生效版本</span><strong>{automation.active_strategy?.version ?? "—"}</strong></div>
      <div><span>最近更新</span><strong>{time(automation.updated_at)}</strong></div>
    </div>
    <div className="safety-callout"><strong>状态说明</strong><p>{automation.reason}</p>
      <div className="inline-actions">
        {automation.state === "enabled" ? <button className="button danger" disabled={busy} onClick={() => void action(() => api.pauseAutomation("paused from web console"))}>暂停自动交易</button>
          : automation.active_strategy ? <button className="button primary" disabled={busy} onClick={() => void action(api.resumeAutomation)}>恢复自动交易</button>
            : <button className="button primary" disabled={busy || !strategy} onClick={() => {
              if (strategy && window.confirm(`确认启用策略版本 ${strategy.version}？`)) void action(() => api.enableAutomation(strategy.id));
            }}>启用最新策略</button>}
        <button className="button secondary" disabled={busy || automation.state === "disabled"} onClick={() => {
          if (window.confirm("确认禁用自动交易？既有对冲仓位不会被自动清仓。")) void action(api.disableAutomation);
        }}>禁用</button>
      </div>
    </div>
    {strategy ? <article className="strategy-json"><header><strong>策略版本 {strategy.version}</strong><span>{strategy.environment.toUpperCase()}</span></header><pre>{JSON.stringify(strategy.config, null, 2)}</pre></article>
      : <div className="empty">尚未创建自动策略版本</div>}
  </>;
}
