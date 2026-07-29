import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { LoginPage } from "./LoginPage";
import { OperationsPanel } from "./OperationsPanel";
import type { OperationsTab } from "./OperationsPanel";
import type {
  Environment,
  Exchange,
  ExecutionActivity,
  ExecutionTask,
  Strategy,
  V2Account,
  V2Opportunity,
  V2OpportunityType,
  Opportunity,
  AdlPosition,
  NotificationHistoryItem,
  NotificationSettings,
} from "./types";

type Page =
  | "accounts"
  | "tasks"
  | "funds"
  | "positions"
  | "opportunities"
  | "dashboard"
  | "strategies"
  | "trades"
  | "alerts"
  | "adl"
  | "email"
  | "profile"
  | "system";

const exchangeNames: Record<Exchange, string> = {
  binance: "Binance",
  okx: "OKX",
  mexc: "MEXC",
  bybit: "Bybit",
  bitget: "Bitget",
  gate: "Gate",
};
const exchanges = Object.keys(exchangeNames) as Exchange[];
const navigation: Array<{ page: Page; label: string; icon: string }> = [
  { page: "accounts", label: "API 密钥", icon: "⌘" },
  { page: "tasks", label: "自动下单", icon: "☷" },
  { page: "funds", label: "资金统计", icon: "▣" },
  { page: "positions", label: "持仓总览", icon: "◉" },
  { page: "opportunities", label: "套利机会", icon: "◈" },
  { page: "dashboard", label: "总览看板", icon: "▤" },
  { page: "strategies", label: "策略列表", icon: "▥" },
  { page: "trades", label: "成交选择", icon: "▧" },
  { page: "alerts", label: "预警监控", icon: "!" },
  { page: "adl", label: "ADL 监控", icon: "▲" },
  { page: "email", label: "邮件推送", icon: "✉" },
  { page: "profile", label: "账户设置", icon: "⚙" },
  { page: "system", label: "系统管理", icon: "⌁" },
];

const pct = (value: string, digits = 2) =>
  `${(Number(value) * 100).toFixed(digits)}%`;
const number = (value: string | number, digits = 4) =>
  Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
const time = (value: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN") : "—";

export function exchangeMarketUrl(
  item: Opportunity,
  environment: Environment,
): string {
  const symbol = encodeURIComponent(item.perp_symbol);
  const hosts: Record<Exchange, string> = {
    binance: environment === "sandbox" ? "https://demo.binance.com" : "https://www.binance.com",
    okx: "https://www.okx.com",
    mexc: "https://www.mexc.com",
    bybit: environment === "sandbox" ? "https://testnet.bybit.com" : "https://www.bybit.com",
    bitget: "https://www.bitget.com",
    gate: environment === "sandbox" ? "https://testnet.gate.com" : "https://www.gate.com",
  };
  return `${hosts[item.exchange]}/trade/${symbol}`;
}

export default function App() {
  const [username, setUsername] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  useEffect(() => {
    api.session()
      .then((value) => setUsername(value.username))
      .catch(() => setUsername(null))
      .finally(() => setChecking(false));
  }, []);
  if (checking) return <main className="login-shell"><p>正在验证会话…</p></main>;
  if (!username) return <LoginPage onAuthenticated={setUsername} />;
  return <Console username={username} onLogout={() => setUsername(null)} />;
}

function Console({ username, onLogout }: { username: string; onLogout: () => void }) {
  const [page, setPage] = useState<Page>("opportunities");
  const [seed, setSeed] = useState<V2Opportunity | null>(null);
  const current = navigation.find((item) => item.page === page)!;
  const openTask = (item: V2Opportunity) => {
    setSeed(item);
    setPage("tasks");
  };
  return <div className="bh-shell">
    <aside className="bh-sidebar">
      <div className="bh-brand">
        <div className="bh-logo">BH</div>
        <div><strong>BASIS HAWK</strong><small>FUNDING STRATEGY</small></div>
      </div>
      <span className="bh-nav-label">MAIN</span>
      <nav aria-label="主菜单">
        {navigation.map((item) => <button
          key={item.page}
          aria-label={item.label}
          className={page === item.page ? "active" : ""}
          onClick={() => setPage(item.page)}
        ><i>{item.icon}</i>{item.label}<b /></button>)}
      </nav>
      <div className="bh-sidebar-foot">
        <span className="bh-avatar">{username.slice(0, 1).toUpperCase()}</span>
        <div><strong>{username}</strong><small>ADMIN</small></div>
        <button aria-label="退出登录" onClick={() => void api.logout().finally(onLogout)}>↪</button>
      </div>
    </aside>
    <div className="bh-main">
      <header className="bh-topbar">
        <div><span>{page.toUpperCase()}</span><b>/</b><strong>{current.label}</strong></div>
        <div><span className="bh-live"><i /> LIVE</span><span>{username}</span></div>
      </header>
      {page === "opportunities" && <OpportunityPage onCreateTask={openTask} />}
      {page === "tasks" && <TasksPage seed={seed} clearSeed={() => setSeed(null)} />}
      {page === "strategies" && <StrategiesPage />}
      {page === "positions" && <PositionsPage />}
      {page === "funds" && <FundsPage />}
      {page === "dashboard" && <DashboardPage />}
      {page === "trades" && <TradesPage />}
      {page === "accounts" && <AccountsPage />}
      {page === "alerts" && <AlertsPage />}
      {page === "adl" && <AdlPage />}
      {page === "email" && <EmailPage />}
      {page === "profile" && <ProfilePage username={username} onSignedOut={onLogout} />}
      {page === "system" && <SystemPage />}
    </div>
  </div>;
}

function PageIntro({ eyebrow, title, copy, actions }: {
  eyebrow: string;
  title: string;
  copy: string;
  actions?: React.ReactNode;
}) {
  return <header className="bh-page-intro">
    <div><p>{eyebrow}</p><h1>{title}</h1><span>{copy}</span></div>
    {actions && <div className="bh-page-actions">{actions}</div>}
  </header>;
}

function OpportunityPage({ onCreateTask }: { onCreateTask: (item: V2Opportunity) => void }) {
  const [type, setType] = useState<V2OpportunityType>("funding");
  const [items, setItems] = useState<V2Opportunity[]>([]);
  const [holdingDays, setHoldingDays] = useState(7);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    setLoading(true);
    api.v2Opportunities(type, holdingDays, search)
      .then((value) => setItems(value.items))
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
  }, [type, holdingDays, search]);
  useEffect(load, [load]);
  const tabs: Array<[V2OpportunityType, string]> = [
    ["funding", "资金费率套利"],
    ["cross_funding", "跨所费率套利"],
    ["basis", "基差交易"],
  ];
  return <main className="bh-page">
    <PageIntro eyebrow="OPPORTUNITIES" title="套利机会" copy="实时跨所价差 + 资金费率套利，按预计收益排序。"
      actions={<button className="bh-button" onClick={load}>刷新</button>} />
    <section className="bh-toolbar">
      <input aria-label="搜索币种" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索币种" />
      <label>计入资金费 <input aria-label="持有天数" type="number" min="1" max="365" value={holdingDays} onChange={(event) => setHoldingDays(Number(event.target.value))} /> 天</label>
      <span>{loading ? "更新中…" : `${items.length} 个候选`}</span>
    </section>
    <div className="bh-tabs">
      {tabs.map(([value, label]) => <button key={value} className={type === value ? "active" : ""} onClick={() => setType(value)}>
        {label}<em>{type === value ? items.length : "·"}</em>
      </button>)}
    </div>
    {error && <div className="bh-error">{error}<button onClick={() => setError(null)}>×</button></div>}
    <div className="bh-table-wrap">
      <table className="bh-table">
        <thead><tr><th>币种</th><th>推荐方向</th><th>入场价差</th><th>年化收益</th><th>{holdingDays} 天预计净收益</th><th>可执行额</th><th>费率来源</th><th /></tr></thead>
        <tbody>
          {items.map((item) => <tr key={item.id}>
            <td><strong>{item.base_asset}</strong><small>{time(item.observed_at)}</small></td>
            <td><div className="bh-legs">{item.legs.map((leg) => <span key={`${leg.exchange}-${leg.market_type}`}>
              <b className={leg.side === "buy" ? "long" : "short"}>{leg.side === "buy" ? "多" : "空"}</b>
              {exchangeNames[leg.exchange]} {leg.market_type === "spot" ? "现货" : "合约"}
              <small>{number(leg.price, 8)}</small>
            </span>)}</div></td>
            <td className={Number(item.entry_spread) >= 0 ? "positive" : "negative"}>{pct(item.entry_spread, 3)}</td>
            <td className="positive"><strong>{pct(item.annualized_return, 1)}</strong></td>
            <td className={Number(item.projected_return) >= 0 ? "positive" : "negative"}><strong>{pct(item.projected_return, 3)}</strong></td>
            <td>{number(item.executable_notional_usdt, 0)} USDT</td>
            <td>{item.legs.map((leg) => leg.fee_source).join(" / ")}</td>
            <td><button className="bh-button primary" onClick={() => onCreateTask(item)}>创建任务</button></td>
          </tr>)}
          {!loading && items.length === 0 && <tr><td colSpan={8} className="bh-empty">暂无满足条件的新鲜机会</td></tr>}
        </tbody>
      </table>
    </div>
    <p className="bh-footnote">预计收益已扣除开平仓往返 Taker 费；实际 Maker 成交会按账户实际费率结算。</p>
  </main>;
}

type DraftLeg = {
  exchange: Exchange;
  account_id: string;
  role: "anchor" | "hedge";
  market_type: "spot" | "perpetual";
  side: "buy" | "sell";
  symbol: string;
  quantity: string;
  order_mode: "maker" | "protected_ioc" | "market";
  book_level: number;
  maximum_chases: number;
  fallback_mode: "protected_ioc" | "market" | "fail";
  margin_mode: "isolated" | "cross";
  leverage: number;
};

const blankLeg = (role: "anchor" | "hedge"): DraftLeg => ({
  exchange: "binance",
  account_id: "",
  role,
  market_type: role === "anchor" ? "spot" : "perpetual",
  side: role === "anchor" ? "buy" : "sell",
  symbol: "BTCUSDT",
  quantity: "0.001",
  order_mode: role === "anchor" ? "maker" : "protected_ioc",
  book_level: 3,
  maximum_chases: 50,
  fallback_mode: "protected_ioc",
  margin_mode: "isolated",
  leverage: 1,
});

function TasksPage({ seed, clearSeed }: { seed: V2Opportunity | null; clearSeed: () => void }) {
  const [tasks, setTasks] = useState<ExecutionTask[]>([]);
  const [accounts, setAccounts] = useState<V2Account[]>([]);
  const [editing, setEditing] = useState(Boolean(seed));
  const [name, setName] = useState(seed ? `${seed.base_asset} 跨所套利` : "");
  const [base, setBase] = useState(seed?.base_asset ?? "BTC");
  const [environment, setEnvironment] = useState<"paper" | Environment>("paper");
  const [legs, setLegs] = useState<DraftLeg[]>(() => seed
    ? seed.legs.map((item, index) => ({
      ...blankLeg(index === 0 ? "anchor" : "hedge"),
      exchange: item.exchange,
      account_id: item.account_id ?? "",
      role: index === 0 ? "anchor" : "hedge",
      market_type: item.market_type,
      side: item.side,
      symbol: item.symbol,
    }))
    : [blankLeg("anchor"), blankLeg("hedge")]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => Promise.all([api.v2ExecutionTasks(), api.v2Accounts()])
    .then(([taskValue, accountValue]) => {
      setTasks(taskValue.items);
      setAccounts(accountValue.items);
    })
    .catch((reason: Error) => setError(reason.message)), []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!seed) return;
    setEditing(true);
    setName(`${seed.base_asset} 跨所套利`);
    setBase(seed.base_asset);
    setLegs(seed.legs.map((item, index) => ({
      ...blankLeg(index === 0 ? "anchor" : "hedge"),
      exchange: item.exchange,
      account_id: item.account_id ?? "",
      role: index === 0 ? "anchor" : "hedge",
      market_type: item.market_type,
      side: item.side,
      symbol: item.symbol,
    })));
    clearSeed();
  }, [seed, clearSeed]);
  const updateLeg = (index: number, value: Partial<DraftLeg>) =>
    setLegs((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...value } : item));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const task = await api.createV2ExecutionTask({
        name,
        display_symbol: `${base}/USDT`,
        environment,
        base_asset: base,
        quantity_mode: "base",
        create_strategy: true,
        hedge_trigger: "immediate",
        maximum_base_exposure: "0.000001",
        maximum_notional_exposure_usdt: "1000000",
        maximum_retries: 3,
        legs: legs.map((leg) => ({
          account_id: environment === "paper" ? null : leg.account_id,
          exchange: leg.exchange,
          role: leg.role,
          market_type: leg.market_type,
          side: leg.side,
          base_asset: base,
          symbol: leg.symbol,
          target_quantity: leg.quantity,
          order_mode: leg.order_mode,
          maximum_slippage: "0.002",
          maker_policy: leg.order_mode === "maker" ? {
            book_level: leg.book_level,
            maximum_chases: leg.maximum_chases,
            fallback_mode: leg.fallback_mode,
          } : null,
          margin_mode: leg.market_type === "perpetual" ? leg.margin_mode : null,
          leverage: leg.market_type === "perpetual" ? leg.leverage : null,
          reduce_only: false,
        })),
      }, crypto.randomUUID());
      setTasks((current) => [task.task, ...current]);
      setEditing(false);
    } catch (reason) {
      setError((reason as Error).message);
    }
  };
  const action = async (task: ExecutionTask, kind: "preflight" | "start" | "cancel") => {
    try {
      const value = kind === "preflight"
        ? await api.preflightV2ExecutionTask(task.id)
        : kind === "start"
          ? await api.startV2ExecutionTask(task.id, task.version)
          : await api.cancelV2ExecutionTask(task.id, task.version);
      setTasks((current) => current.map((item) => item.id === task.id ? value.task : item));
    } catch (reason) {
      setError((reason as Error).message);
    }
  };
  return <main className="bh-page">
    <PageIntro eyebrow="EXECUTION" title={editing ? "新建自动下单任务" : "自动下单"} copy="任务驱动的 2–16 腿执行；每条腿独立选择交易所、账户、方向与成交方式。"
      actions={<button className="bh-button primary" onClick={() => setEditing(!editing)}>{editing ? "返回列表" : "新建任务"}</button>} />
    {error && <div className="bh-error">{error}<button onClick={() => setError(null)}>×</button></div>}
    {editing ? <form onSubmit={(event) => void submit(event)} className="bh-builder">
      <section className="bh-card">
        <header><h2>基础配置</h2></header>
        <div className="bh-form-grid">
          <label>任务名称<input required value={name} onChange={(event) => setName(event.target.value)} placeholder="BTC 费率套利 Binance–OKX" /></label>
          <label>基础币<input required value={base} onChange={(event) => setBase(event.target.value.toUpperCase())} /></label>
          <label>环境<select value={environment} onChange={(event) => setEnvironment(event.target.value as "paper" | Environment)}><option value="paper">Paper</option><option value="sandbox">Sandbox</option><option value="live">Live</option></select></label>
        </div>
      </section>
      {legs.map((leg, index) => <section className="bh-card bh-leg-card" key={index}>
        <header><div><h2>{index === 0 ? "主腿配置" : `对冲腿 ${index}`}</h2><span>{leg.role === "anchor" ? "优先腿" : "对冲腿"}</span></div>{index > 1 && <button type="button" onClick={() => setLegs((current) => current.filter((_, i) => i !== index))}>移除</button>}</header>
        <div className="bh-form-grid four">
          <label>交易所<select value={leg.exchange} onChange={(event) => updateLeg(index, { exchange: event.target.value as Exchange, account_id: "" })}>{exchanges.map((item) => <option key={item} value={item}>{exchangeNames[item]}</option>)}</select></label>
          <label>API Key<select disabled={environment === "paper"} value={leg.account_id} onChange={(event) => updateLeg(index, { account_id: event.target.value })}><option value="">{environment === "paper" ? "Paper 无需账户" : "选择账户"}</option>{accounts.filter((item) => item.exchange === leg.exchange && item.environment === environment).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
          <label>市场类型<select value={leg.market_type} onChange={(event) => updateLeg(index, { market_type: event.target.value as DraftLeg["market_type"] })}><option value="spot">现货</option><option value="perpetual">合约</option></select></label>
          <label>方向<select value={leg.side} onChange={(event) => updateLeg(index, { side: event.target.value as DraftLeg["side"] })}><option value="buy">多 / 买</option><option value="sell">空 / 卖</option></select></label>
          <label>交易对<input value={leg.symbol} onChange={(event) => updateLeg(index, { symbol: event.target.value.toUpperCase() })} /></label>
          <label>总数量<input type="number" step="any" min="0" value={leg.quantity} onChange={(event) => updateLeg(index, { quantity: event.target.value })} /></label>
          <label>下单方式<select value={leg.order_mode} onChange={(event) => updateLeg(index, { order_mode: event.target.value as DraftLeg["order_mode"] })}><option value="maker">Maker</option><option value="protected_ioc">保护 IOC</option><option value="market">Market</option></select></label>
          {leg.market_type === "perpetual" && <><label>保证金模式<select value={leg.margin_mode} onChange={(event) => updateLeg(index, { margin_mode: event.target.value as DraftLeg["margin_mode"] })}><option value="isolated">逐仓</option><option value="cross">全仓</option></select></label><label>杠杆<input type="number" min="1" max="10" value={leg.leverage} onChange={(event) => updateLeg(index, { leverage: Number(event.target.value) })} /></label></>}
          {leg.order_mode === "maker" && <><label>盘口档位<input type="number" min="1" max="50" value={leg.book_level} onChange={(event) => updateLeg(index, { book_level: Number(event.target.value) })} /><small>掉出前 N 档自动追价</small></label><label>最大追价次数<input type="number" min="0" max="50" value={leg.maximum_chases} onChange={(event) => updateLeg(index, { maximum_chases: Number(event.target.value) })} /></label><label>超限回退<select value={leg.fallback_mode} onChange={(event) => updateLeg(index, { fallback_mode: event.target.value as DraftLeg["fallback_mode"] })}><option value="protected_ioc">保护 IOC</option><option value="market">Market</option><option value="fail">停止任务</option></select></label></>}
        </div>
      </section>)}
      <div className="bh-builder-actions"><button type="button" className="bh-button" disabled={legs.length >= 16} onClick={() => setLegs((current) => [...current, blankLeg("hedge")])}>＋ 添加对冲腿</button><button className="bh-button primary">保存任务</button></div>
    </form> : <TaskTable tasks={tasks} action={action} />}
  </main>;
}

function TaskTable({ tasks, action }: {
  tasks: ExecutionTask[];
  action: (task: ExecutionTask, kind: "preflight" | "start" | "cancel") => Promise<void>;
}) {
  return <div className="bh-table-wrap"><table className="bh-table"><thead><tr><th>任务</th><th>环境</th><th>腿</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>
    {tasks.map((task) => <tr key={task.id}><td><strong>{task.name}</strong><small>{task.display_symbol}</small></td><td>{task.environment.toUpperCase()}</td><td><div className="bh-leg-stack">{task.legs.map((leg) => <span key={leg.id}>{exchangeNames[leg.exchange]} · {leg.market_type === "spot" ? "现货" : "合约"} · {leg.side === "buy" ? "多" : "空"} · {leg.order_mode}</span>)}</div></td><td><span className={`bh-status ${task.status}`}>{task.status}</span></td><td>{time(task.updated_at)}</td><td><div className="bh-row-actions">{["draft", "preflight_ready"].includes(task.status) && <button className="bh-button" onClick={() => void action(task, "preflight")}>预检</button>}{task.status === "preflight_ready" && <button className="bh-button primary" onClick={() => void action(task, "start")}>确认启动</button>}{["draft", "preflight_ready", "queued"].includes(task.status) && <button className="bh-button danger" onClick={() => void action(task, "cancel")}>停止</button>}</div></td></tr>)}
    {tasks.length === 0 && <tr><td className="bh-empty" colSpan={6}>还没有自动下单任务</td></tr>}
  </tbody></table></div>;
}

function useStrategies(status = "") {
  const [items, setItems] = useState<Strategy[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api.v2Strategies(status).then((value) => setItems(value.items)).catch((reason: Error) => setError(reason.message)), [status]);
  useEffect(() => { void load(); }, [load]);
  return { items, error, load };
}

function StrategiesPage() {
  const [status, setStatus] = useState("");
  const { items, error, load } = useStrategies(status);
  const total = items.reduce((sum, item) => sum + Number(item.net_pnl_usdt), 0);
  const funding = items.reduce((sum, item) => sum + Number(item.funding_income_usdt), 0);
  return <main className="bh-page"><PageIntro eyebrow="STRATEGIES" title="我的策略" copy="任务完成后生成组合，在这里查看持仓、累计盈亏与运行时长。" actions={<><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部</option><option value="running">运行中</option><option value="ended">已结束</option><option value="manual_review">人工复核</option></select><button className="bh-button" onClick={() => void load()}>刷新</button></>} />
    {error && <div className="bh-error">{error}</div>}
    <div className="bh-metrics"><Metric label="运行中" value={items.filter((item) => item.status === "running").length} /><Metric label="累计净 PNL" value={`${total >= 0 ? "+" : ""}${number(total)} USDT`} tone={total >= 0 ? "green" : "red"} /><Metric label="累计资金费" value={`${funding >= 0 ? "+" : ""}${number(funding)} USDT`} tone="green" /></div>
    <StrategyTable items={items} />
  </main>;
}

function StrategyTable({ items }: { items: Strategy[] }) {
  return <div className="bh-table-wrap"><table className="bh-table"><thead><tr><th>名称</th><th>交易腿</th><th>状态</th><th>净 PNL</th><th>资金费累计</th><th>手续费</th><th>创建时间</th></tr></thead><tbody>
    {items.map((item) => <tr key={item.id}><td><strong>{item.name}</strong><small>{item.base_asset} · {item.environment.toUpperCase()}</small></td><td><div className="bh-leg-stack">{item.legs.map((leg) => <span key={leg.id}>{exchangeNames[leg.exchange]} {leg.market_type === "spot" ? "现货" : "合约"} {leg.side === "buy" ? "多" : "空"} · {number(leg.remaining_base_quantity, 8)}</span>)}</div></td><td><span className={`bh-status ${item.status}`}>{item.status}</span></td><td className={Number(item.net_pnl_usdt) >= 0 ? "positive" : "negative"}>{number(item.net_pnl_usdt)} USDT</td><td>{number(item.funding_income_usdt)} USDT</td><td>{number(item.fees_usdt)} USDT</td><td>{time(item.created_at)}</td></tr>)}
    {items.length === 0 && <tr><td className="bh-empty" colSpan={7}>还没有策略</td></tr>}
  </tbody></table></div>;
}

function PositionsPage() {
  const { items, error } = useStrategies("running,closing,manual_review");
  return <main className="bh-page"><PageIntro eyebrow="PORTFOLIO" title="持仓总览" copy="按策略聚合跨所现货与永续腿，数量统一换算为基础币。" />
    {error && <div className="bh-error">{error}</div>}
    <StrategyTable items={items} />
  </main>;
}

function FundsPage() {
  const { items } = useStrategies();
  const realized = items.reduce((sum, item) => sum + Number(item.realized_pnl_usdt), 0);
  const funding = items.reduce((sum, item) => sum + Number(item.funding_income_usdt), 0);
  const fees = items.reduce((sum, item) => sum + Number(item.fees_usdt), 0);
  return <main className="bh-page"><PageIntro eyebrow="TREASURY" title="资金统计" copy="按组合汇总已实现盈亏、资金费收入与手续费。" />
    <div className="bh-metrics four"><Metric label="已实现盈亏" value={`${number(realized)} USDT`} tone={realized >= 0 ? "green" : "red"} /><Metric label="资金费累计" value={`${number(funding)} USDT`} tone="green" /><Metric label="手续费累计" value={`${number(fees)} USDT`} tone="red" /><Metric label="组合净收益" value={`${number(realized + funding - fees)} USDT`} tone={realized + funding - fees >= 0 ? "green" : "red"} /></div>
    <div className="bh-chart-empty"><div className="bh-chart-bars"><i /><i /><i /><i /><i /><i /><i /></div><p>收益时间序列将在策略结算后显示</p></div>
  </main>;
}

function DashboardPage() {
  const { items } = useStrategies();
  const running = items.filter((item) => item.status === "running").length;
  const ended = items.filter((item) => item.status === "ended").length;
  const net = items.reduce((sum, item) => sum + Number(item.net_pnl_usdt), 0);
  const funding = items.reduce((sum, item) => sum + Number(item.funding_income_usdt), 0);
  const fees = items.reduce((sum, item) => sum + Number(item.fees_usdt), 0);
  return <main className="bh-page"><PageIntro eyebrow="DASHBOARD" title="总览看板" copy="跨所策略、资金费与执行任务的统一运营视图。" />
    <div className="bh-metrics six"><Metric label="运行中策略" value={running} /><Metric label="已结束策略" value={ended} /><Metric label="总净收益" value={`${net >= 0 ? "+" : ""}${number(net)}`} tone={net >= 0 ? "green" : "red"} /><Metric label="资金费率累计" value={`+${number(funding)}`} tone="green" /><Metric label="手续费累计" value={`-${number(fees)}`} tone="red" /><Metric label="预警策略数" value={items.filter((item) => item.status === "manual_review").length} /></div>
    <div className="bh-chart-empty large"><header><strong>资金费率 & 费率累计走势</strong><span>7 天　30 天　90 天　全部</span></header><div className="bh-chart-bars"><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /></div><p>暂无资金费率数据</p></div>
  </main>;
}

function TradesPage() {
  const [tasks, setTasks] = useState<ExecutionTask[]>([]);
  const [selected, setSelected] = useState("");
  const [activity, setActivity] = useState<ExecutionActivity | null>(null);
  useEffect(() => { void api.v2ExecutionTasks().then((value) => setTasks(value.items)); }, []);
  useEffect(() => {
    if (!selected) { setActivity(null); return; }
    void api.v2ExecutionTaskActivity(selected).then((value) => setActivity(value.activity));
  }, [selected]);
  const task = tasks.find((item) => item.id === selected);
  return <main className="bh-page"><PageIntro eyebrow="TRADES" title="成交记录选择器" copy="先选择执行任务，再审阅对应的腿、订单尝试、Maker 追价与成交。" />
    <section className="bh-card"><header><h2>1. 选择任务和腿</h2></header><div className="bh-selector"><select aria-label="选择任务" value={selected} onChange={(event) => setSelected(event.target.value)}><option value="">搜索或选择任务</option>{tasks.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.status}</option>)}</select><select aria-label="选择腿" disabled={!task}><option>全部腿</option>{task?.legs.map((leg) => <option key={leg.id}>{exchangeNames[leg.exchange]} · {leg.symbol} · {leg.side}</option>)}</select></div></section>
    {activity && <div className="bh-metrics"><Metric label="执行轮次" value={activity.runs.length} /><Metric label="订单尝试" value={activity.orders.length} /><Metric label="成交记录" value={activity.fills.length} /></div>}
  </main>;
}

function AccountsPage() {
  const [items, setItems] = useState<V2Account[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api.v2Accounts().then((value) => setItems(value.items)).catch((reason: Error) => setError(reason.message)), []);
  useEffect(() => { void load(); }, [load]);
  return <main className="bh-page"><PageIntro eyebrow="API KEYS" title="API 密钥" copy="同一交易所与环境可保存多个具名账户；密钥加密存储且永不回显。" actions={<button className="bh-button primary" onClick={() => setShowForm(!showForm)}>新增账户</button>} />
    {error && <div className="bh-error">{error}</div>}
    {showForm && <AccountForm onCreated={() => { setShowForm(false); void load(); }} onError={setError} />}
    <div className="bh-account-grid">{items.map((item) => <article className="bh-account" key={item.id}><header><strong>{exchangeNames[item.exchange]}</strong><span>{item.environment.toUpperCase()}</span></header><h3>{item.label}</h3><code>{item.masked_api_key}</code><div><span>交易默认 {item.trading_default ? "✓" : "—"}</span><span>扫描默认 {item.scanner_default ? "✓" : "—"}</span><span>费率 {item.fees.source}</span></div></article>)}{items.length === 0 && <div className="bh-empty-card">尚未配置账户</div>}</div>
  </main>;
}

function AccountForm({ onCreated, onError }: { onCreated: () => void; onError: (value: string) => void }) {
  const [exchange, setExchange] = useState<Exchange>("binance");
  const [environment, setEnvironment] = useState<Environment>("live");
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createV2Account({
        exchange, environment, label, api_key: apiKey, api_secret: secret,
        passphrase: passphrase || null,
      });
      onCreated();
    } catch (reason) {
      onError((reason as Error).message);
    }
  };
  return <form className="bh-card bh-account-form" onSubmit={(event) => void submit(event)}><div className="bh-form-grid four"><label>交易所<select value={exchange} onChange={(event) => setExchange(event.target.value as Exchange)}>{exchanges.map((item) => <option key={item}>{item}</option>)}</select></label><label>环境<select value={environment} onChange={(event) => setEnvironment(event.target.value as Environment)}><option value="live">Live</option><option value="sandbox">Sandbox</option></select></label><label>账户名称<input required value={label} onChange={(event) => setLabel(event.target.value)} /></label><label>API Key<input required value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></label><label>Secret<input required type="password" value={secret} onChange={(event) => setSecret(event.target.value)} /></label>{["okx", "bitget"].includes(exchange) && <label>Passphrase<input required type="password" value={passphrase} onChange={(event) => setPassphrase(event.target.value)} /></label>}</div><button className="bh-button primary">加密保存</button></form>;
}

function EmptyMonitor({ title, copy }: { title: string; copy: string }) {
  return <main className="bh-page"><PageIntro eyebrow="MONITORING" title={title} copy={copy} /><section className="bh-card bh-monitor-empty"><div>✓</div><h2>当前没有需要处理的记录</h2><p>后台 worker 会持续更新此页面。</p></section></main>;
}

const notificationStatusNames: Record<NotificationHistoryItem["status"], string> = {
  pending: "待投递",
  sending: "投递中",
  retry: "等待重试",
  sent: "已送达",
  dead: "已停止",
};

function AlertsPage() {
  const [items, setItems] = useState<NotificationHistoryItem[]>([]);
  const [severity, setSeverity] = useState<"all" | "warning" | "critical">("all");
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    setError(null);
    void api.notificationHistory()
      .then((value) => setItems(value.items.filter((item) => item.severity !== "info")))
      .catch((reason: Error) => setError(reason.message));
  }, []);
  useEffect(load, [load]);
  const visible = items.filter((item) => severity === "all" || item.severity === severity);
  const unresolved = items.filter((item) => item.status !== "sent").length;
  return <main className="bh-page">
    <PageIntro
      eyebrow="MONITORING"
      title="预警监控"
      copy="集中查看交易失败、人工复核、安全暂停和通知投递状态。"
      actions={<button className="bh-button" onClick={load}>刷新</button>}
    />
    {error && <div className="bh-error">{error}</div>}
    <div className="bh-metrics four">
      <Metric label="未处理" value={unresolved} tone={unresolved ? "red" : undefined} />
      <Metric label="严重" value={items.filter((item) => item.severity === "critical").length} tone="red" />
      <Metric label="警告" value={items.filter((item) => item.severity === "warning").length} />
      <Metric label="投递失败" value={items.filter((item) => item.status === "dead").length} tone="red" />
    </div>
    <div className="bh-tabs">
      {([
        ["all", "全部"],
        ["critical", "严重"],
        ["warning", "警告"],
      ] as const).map(([value, label]) => <button
        key={value}
        className={severity === value ? "active" : ""}
        onClick={() => setSeverity(value)}
      >{label}<em>{value === "all" ? items.length : items.filter((item) => item.severity === value).length}</em></button>)}
    </div>
    <div className="bh-table-wrap"><table className="bh-table"><thead><tr>
      <th>级别</th><th>事件</th><th>摘要</th><th>通道</th><th>投递状态</th><th>尝试</th><th>发生时间</th>
    </tr></thead><tbody>
      {visible.map((item) => <tr key={item.id}>
        <td><span className={`bh-alert-level ${item.severity}`}>{item.severity === "critical" ? "严重" : "警告"}</span></td>
        <td><strong>{item.event_type}</strong><small>{item.id.slice(0, 8)}</small></td>
        <td>{item.subject}</td>
        <td>{item.channel === "email" ? "邮件" : "Telegram"}</td>
        <td><span className={`bh-status ${item.status}`}>{notificationStatusNames[item.status]}</span>{item.last_error_code && <small>{item.last_error_code}</small>}</td>
        <td>{item.attempts}</td>
        <td>{time(item.created_at)}</td>
      </tr>)}
      {visible.length === 0 && <tr><td className="bh-empty" colSpan={7}>当前没有匹配的预警记录</td></tr>}
    </tbody></table></div>
  </main>;
}

function EmailPage() {
  const [items, setItems] = useState<NotificationHistoryItem[]>([]);
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    setError(null);
    void Promise.all([
      api.notificationSettings(),
      api.notificationHistory({ channel: "email" }),
    ])
      .then(([configuration, history]) => {
        setSettings(configuration);
        setItems(history.items);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);
  useEffect(load, [load]);
  const testEmail = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await api.testNotifications(["email"]);
      setMessage("测试邮件已进入投递队列");
      load();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return <main className="bh-page">
    <PageIntro
      eyebrow="DELIVERY"
      title="邮件推送"
      copy="关键成交、人工复核与安全暂停通过 SMTP 异步投递。"
      actions={<><button className="bh-button" onClick={load}>刷新</button><button className="bh-button primary" disabled={busy || !settings?.email.configured} onClick={() => void testEmail()}>{busy ? "提交中…" : "发送测试邮件"}</button></>}
    />
    {error && <div className="bh-error">{error}</div>}
    {message && <div className="bh-success">{message}</div>}
    <section className="bh-notification-config">
      <article className="bh-card"><header><h2>SMTP 通道</h2><span className={`bh-channel-state ${settings?.email.configured ? "ready" : ""}`}>{settings?.email.configured ? "已配置" : "未配置"}</span></header>
        <dl><div><dt>传输安全</dt><dd>{settings?.email.security?.toUpperCase() || "—"}</dd></div><div><dt>端口</dt><dd>{settings?.email.port || "—"}</dd></div><div><dt>身份认证</dt><dd>{settings?.email.authentication_configured ? "已配置" : "未配置"}</dd></div><div><dt>发件人与收件人</dt><dd>{settings?.email.sender_configured && settings?.email.recipient_configured ? "已配置" : "未完整配置"}</dd></div></dl>
        {!settings?.email.configured && <p>请在服务器环境中配置 SMTP Host、发件人与收件人；密码不会通过 API 返回。</p>}
      </article>
      <article className="bh-card"><header><h2>投递概况</h2></header><dl><div><dt>累计记录</dt><dd>{items.length}</dd></div><div><dt>已送达</dt><dd>{items.filter((item) => item.status === "sent").length}</dd></div><div><dt>等待重试</dt><dd>{items.filter((item) => item.status === "retry").length}</dd></div><div><dt>已停止</dt><dd>{items.filter((item) => item.status === "dead").length}</dd></div></dl></article>
    </section>
    <div className="bh-table-wrap"><table className="bh-table"><thead><tr>
      <th>主题</th><th>事件</th><th>级别</th><th>状态</th><th>尝试</th><th>最近更新</th>
    </tr></thead><tbody>
      {items.map((item) => <tr key={item.id}>
        <td><strong>{item.subject}</strong><small>{item.id.slice(0, 8)}</small></td>
        <td>{item.event_type}</td>
        <td><span className={`bh-alert-level ${item.severity}`}>{item.severity === "critical" ? "严重" : item.severity === "warning" ? "警告" : "信息"}</span></td>
        <td><span className={`bh-status ${item.status}`}>{notificationStatusNames[item.status]}</span>{item.last_error_code && <small>{item.last_error_code}</small>}</td>
        <td>{item.attempts}</td>
        <td>{time(item.updated_at)}</td>
      </tr>)}
      {items.length === 0 && <tr><td className="bh-empty" colSpan={6}>暂无邮件投递记录</td></tr>}
    </tbody></table></div>
  </main>;
}

function ProfilePage({
  username,
  onSignedOut,
}: {
  username: string;
  onSignedOut: () => void;
}) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [currentTotp, setCurrentTotp] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [rotatePassword, setRotatePassword] = useState("");
  const [rotateTotpCode, setRotateTotpCode] = useState("");
  const [rotateConfirmed, setRotateConfirmed] = useState(false);
  const [provisioningUri, setProvisioningUri] = useState<string | null>(null);
  const [busy, setBusy] = useState<"password" | "totp" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const changePassword = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    if (newPassword !== confirmation) {
      setError("两次输入的新密码不一致");
      return;
    }
    setBusy("password");
    try {
      await api.changePassword(currentPassword, currentTotp, newPassword);
      onSignedOut();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const rotateTotp = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!rotateConfirmed) {
      setError("请先确认已准备好重新绑定验证器");
      return;
    }
    setBusy("totp");
    try {
      const result = await api.rotateTotp(rotatePassword, rotateTotpCode);
      setProvisioningUri(result.provisioning_uri);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return <main className="bh-page">
    <PageIntro eyebrow="SECURITY" title="账户设置" copy="管理唯一管理员的密码、TOTP 和会话安全。" />
    {error && <div className="bh-error">{error}</div>}
    <section className="bh-profile-summary">
      <article className="bh-card"><header><h2>当前管理员</h2><span className="bh-channel-state ready">已登录</span></header><div><span className="bh-profile-avatar">{username.slice(0, 1).toUpperCase()}</span><strong>{username}</strong><small>单管理员 · PASSWORD + TOTP</small></div></article>
      <article className="bh-card"><header><h2>会话保护</h2></header><ul><li>Secure / SameSite Cookie</li><li>所有写请求校验 CSRF</li><li>修改密码或 TOTP 后撤销全部会话</li></ul></article>
    </section>
    <section className="bh-security-grid">
      <form className="bh-card bh-security-form" onSubmit={(event) => void changePassword(event)}>
        <header><h2>修改密码</h2></header>
        <div>
          <label>当前密码<input type="password" autoComplete="current-password" required value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
          <label>当前 TOTP<input inputMode="numeric" autoComplete="one-time-code" required minLength={6} maxLength={10} value={currentTotp} onChange={(event) => setCurrentTotp(event.target.value)} /></label>
          <label>新密码<input type="password" autoComplete="new-password" required minLength={12} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /><small>至少 12 个字符，且不能与当前密码相同。</small></label>
          <label>确认新密码<input type="password" autoComplete="new-password" required minLength={12} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
          <button className="bh-button primary" disabled={busy !== null}>{busy === "password" ? "修改中…" : "修改并退出全部会话"}</button>
        </div>
      </form>
      <form className="bh-card bh-security-form" onSubmit={(event) => void rotateTotp(event)}>
        <header><h2>轮换 TOTP</h2></header>
        {!provisioningUri ? <div>
          <p className="bh-security-warning">轮换会立即使旧验证码和全部现有会话失效。提交前请准备好验证器。</p>
          <label>当前密码<input type="password" autoComplete="current-password" required value={rotatePassword} onChange={(event) => setRotatePassword(event.target.value)} /></label>
          <label>当前 TOTP<input inputMode="numeric" autoComplete="one-time-code" required minLength={6} maxLength={10} value={rotateTotpCode} onChange={(event) => setRotateTotpCode(event.target.value)} /></label>
          <label className="bh-check"><input type="checkbox" checked={rotateConfirmed} onChange={(event) => setRotateConfirmed(event.target.checked)} />我已准备好立即绑定新的 TOTP</label>
          <button className="bh-button danger" disabled={busy !== null || !rotateConfirmed}>{busy === "totp" ? "轮换中…" : "确认轮换 TOTP"}</button>
        </div> : <div className="bh-provisioning">
          <strong>新的 TOTP 已生效</strong>
          <p>请立即把下列 URI 导入验证器。离开此页后系统不会再次显示。</p>
          <code>{provisioningUri}</code>
          <button type="button" className="bh-button primary" onClick={onSignedOut}>已保存，前往重新登录</button>
        </div>}
      </form>
    </section>
  </main>;
}

function AdlPage() {
  const [items, setItems] = useState<AdlPosition[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void api.v2Adl()
      .then((value) => setItems(value.items))
      .catch((reason: Error) => setError(reason.message));
  }, []);
  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      setItems((await api.refreshV2Adl()).items);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return <main className="bh-page">
    <PageIntro eyebrow="RISK / ADL" title="ADL 监控" copy="实时监控永续仓位自动减仓队列，等级 5 为最高风险。"
      actions={<button className="bh-button primary" disabled={busy} onClick={() => void refresh()}>{busy ? "刷新中…" : "刷新账户"}</button>} />
    {error && <div className="bh-error">{error}</div>}
    <div className="bh-metrics"><Metric label="监控账户" value={new Set(items.map((item) => item.account_id)).size} /><Metric label="高风险仓位" value={items.filter((item) => (item.risk_level ?? 0) >= 4).length} tone="red" /><Metric label="事件模式账户" value={items.filter((item) => item.event_only).length} /></div>
    <div className="bh-table-wrap"><table className="bh-table"><thead><tr><th>交易所 / 账户</th><th>环境</th><th>合约</th><th>方向</th><th>ADL 风险</th><th>原生值</th><th>观测时间</th></tr></thead><tbody>
      {items.map((item) => <tr key={`${item.account_id}-${item.symbol}-${item.position_side}`}><td><strong>{exchangeNames[item.exchange]}</strong><small>{item.account_label}</small></td><td>{item.environment.toUpperCase()}</td><td>{item.event_only ? "私有事件监听" : item.symbol}</td><td>{item.position_side}</td><td><div className="bh-adl-levels">{[1, 2, 3, 4, 5].map((level) => <i key={level} className={(item.risk_level ?? 0) >= level ? `active level-${level}` : ""} />)}<span>{item.event_only ? "EVENT" : item.risk_level ?? "—"}</span></div></td><td>{item.native_value ?? "—"}</td><td>{time(item.observed_at)}</td></tr>)}
      {items.length === 0 && <tr><td className="bh-empty" colSpan={7}>暂无 ADL 快照；点击“刷新账户”读取实时风险等级</td></tr>}
    </tbody></table></div>
  </main>;
}

function SystemPage() {
  const [tab, setTab] = useState<OperationsTab>("system");
  const tabs: Array<[OperationsTab, string]> = [["system", "执行状态"], ["accounts", "旧账户"], ["trades", "旧交易"], ["ledger", "旧账本"], ["transfers", "内部划转"], ["automation", "旧自动策略"], ["history", "审计通知"]];
  return <div className="bh-system"><div className="bh-system-tabs">{tabs.map(([value, label]) => <button className={tab === value ? "active" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}</div><OperationsPanel activeTab={tab} opportunities={[]} /></div>;
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: "green" | "red" }) {
  return <article className={`bh-metric ${tone ?? ""}`}><span>{label}</span><strong>{value}</strong><small>实时账本汇总</small></article>;
}
