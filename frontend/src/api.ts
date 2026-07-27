import type {
  AccountSnapshot,
  AuditEvent,
  AutoStrategyConfig,
  AutomationStatus,
  BackupStatus,
  CredentialSummary,
  Environment,
  Exchange,
  ExchangeStatus,
  ExecutionStatus,
  InternalTransfer,
  LiveClosePreview,
  LiveOpenPreview,
  FillHistoryItem,
  FundingIncome,
  NotificationHistoryItem,
  Opportunity,
  OrderHistoryItem,
  PairedPosition,
  PnlRealization,
  Settings,
  TradeIntent,
  UpdateStatus,
} from "./types";

function cookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const value = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return value ? decodeURIComponent(value.slice(prefix.length)) : null;
}

export function apiErrorMessage(detail: unknown, fallback: string): string {
  const translations: Record<string, string> = {
    "exchange credential is not configured": "尚未配置该交易所的账户凭据",
    "executable top-of-book data is unavailable": "无法读取当前可执行一档盘口，请稍后重试",
    "opportunity is not available": "当前找不到该标的的有效机会",
    "instrument trading rules are not available": "当前无法读取该标的的交易规则",
    "only healthy opportunities can be planned": "该机会当前不是有效状态，不能生成开仓预览",
    "market quote is stale": "行情已经陈旧，请等待下一次刷新后重试",
    "current market or trading rules are not available": "当前行情或交易规则不可用，请等待刷新后重新生成预览",
    "market or configuration changed after trade preview": "交易规则或配置已发生变化，旧预览已失效，请重新生成预览",
    "market moved beyond preview slippage protection": "行情已超出预览中设置的最大滑点保护，请重新生成预览，或在确认风险后适当提高最大滑点",
    "trade preview has expired": "预览票据已超过 60 秒有效期，请重新生成预览",
    "trade preview was created by an older software version": "该预览由升级前版本生成，请重新生成预览",
    "trade preview was not found": "找不到该预览票据，请重新生成预览",
    "trade preview belongs to another administrator": "该预览票据不属于当前管理员，请重新生成预览",
    "trade preview was already confirmed with another idempotency key": "该预览票据已经确认，不能重复提交",
  };
  if (typeof detail === "string") return translations[detail] || detail;
  if (!detail || typeof detail !== "object") return fallback;
  const value = detail as {
    code?: string;
    message?: string;
    minimum_notional_usdt?: string;
    capacity_notional_usdt?: string;
  };
  if (value.code === "notional_below_minimum" && value.minimum_notional_usdt) {
    return `名义金额过低；该标的当前至少需要 ${value.minimum_notional_usdt} USDT（受现货与永续共同数量步长及交易所最低下单规则限制）`;
  }
  if (value.code === "notional_exceeds_top_book" && value.capacity_notional_usdt) {
    return `名义金额超过当前一档双腿可执行容量；当前最多可下 ${value.capacity_notional_usdt} USDT，盘口会实时变化，建议输入略低于该值的金额后重试`;
  }
  if (value.message && translations[value.message]) return translations[value.message];
  return value.message || fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method?.toUpperCase() ?? "GET";
  const csrf = method === "GET" || method === "HEAD" ? null : cookie("basis_hawk_csrf");
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text();
    let detail = body || `HTTP ${response.status}`;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      detail = apiErrorMessage(parsed.detail, detail);
    } catch {
      // Preserve the plain-text response.
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  session: () => request<{ username: string }>("/api/auth/session"),
  login: (username: string, password: string, totpCode: string) =>
    request<{ username: string; expires_at: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password, totp_code: totpCode }),
    }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  opportunities: () => request<{ items: Opportunity[]; sequence: number }>("/api/opportunities?page_size=3000"),
  statuses: () => request<{ items: ExchangeStatus[] }>("/api/exchanges/status"),
  settings: () => request<Settings>("/api/settings"),
  saveSettings: (value: Settings) => request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(value) }),
  history: (item: Opportunity, range: string) =>
    request<{ items: Opportunity[] }>(`/api/opportunities/${item.exchange}/${item.base_asset}/history?range=${range}`),
  topBook: (item: Opportunity) =>
    request<Opportunity>(
      `/api/opportunities/${item.exchange}/${encodeURIComponent(item.base_asset)}/top-book`,
    ),
  execution: () => request<ExecutionStatus>("/api/system/execution"),
  auditHistory: () => request<{ items: AuditEvent[] }>("/api/operations/audit?limit=100"),
  notificationHistory: () =>
    request<{ items: NotificationHistoryItem[] }>("/api/operations/notifications?limit=100"),
  backupStatus: () => request<BackupStatus>("/api/operations/backup"),
  updateStatus: () => request<UpdateStatus>("/api/operations/update"),
  checkForUpdates: () =>
    request<{ queued: true; request_id: string }>("/api/operations/update/check", {
      method: "POST",
      body: JSON.stringify({}),
    }),
  applyUpdate: (targetCommit: string) =>
    request<{ queued: true; request_id: string }>("/api/operations/update/apply", {
      method: "POST",
      body: JSON.stringify({ target_commit: targetCommit, confirmed: true }),
    }),
  deleteBackup: (archiveName: string) =>
    request<{ deleted: true; archive_name: string }>(
      `/api/operations/backups/${encodeURIComponent(archiveName)}`,
      {
        method: "DELETE",
        body: JSON.stringify({ confirmed: true }),
      },
    ),
  deleteBackups: (archiveNames: string[]) =>
    request<{ deleted_count: number; archive_names: string[] }>(
      "/api/operations/backups/batch-delete",
      {
        method: "POST",
        body: JSON.stringify({ archive_names: archiveNames, confirmed: true }),
      },
    ),
  pruneLogs: (retentionDays: number) =>
    request<{ deleted_count: number; cutoff: string }>("/api/operations/logs/prune", {
      method: "POST",
      body: JSON.stringify({ retention_days: retentionDays, confirmed: true }),
    }),
  testNotifications: (channels: ("telegram" | "email")[]) =>
    request<{ request_id: string; items: { id: string; channel: string; status: string }[] }>(
      "/api/operations/notifications/test",
      {
        method: "POST",
        body: JSON.stringify({ channels, confirmed: true }),
      },
    ),
  pauseExecution: (reason: string) => request<ExecutionStatus>("/api/system/execution/pause", {
    method: "POST",
    body: JSON.stringify({ confirmed: true, reason }),
  }),
  resumeExecution: () => request<ExecutionStatus>("/api/system/execution/resume", {
    method: "POST",
    body: JSON.stringify({ confirmed: true }),
  }),
  credentials: () => request<{ items: CredentialSummary[] }>("/api/accounts/credentials"),
  saveCredential: (
    exchange: Exchange,
    environment: Environment,
    value: {
      label: string;
      api_key: string;
      api_secret: string;
      passphrase?: string;
      position_mode?: "one_way" | "hedge";
    },
  ) => request<CredentialSummary>(`/api/accounts/${exchange}/${environment}/credentials`, {
    method: "PUT",
    body: JSON.stringify(value),
  }),
  updateBybitPositionMode: (
    environment: Environment,
    positionMode: "one_way" | "hedge",
  ) => request<CredentialSummary>(`/api/accounts/bybit/${environment}/position-mode`, {
    method: "PUT",
    body: JSON.stringify({ position_mode: positionMode, confirmed: true }),
  }),
  deleteCredential: (exchange: Exchange, environment: Environment) =>
    request<void>(`/api/accounts/${exchange}/${environment}/credentials`, { method: "DELETE" }),
  accountSnapshot: (exchange: Exchange, environment: Environment) =>
    request<AccountSnapshot>(`/api/accounts/${exchange}/${environment}/snapshot`),
  positions: () => request<{ items: PairedPosition[] }>("/api/trades/positions"),
  tradeIntents: () => request<{ items: TradeIntent[] }>("/api/trades/intents?limit=100"),
  orders: () => request<{ items: OrderHistoryItem[] }>("/api/trades/orders?limit=100"),
  fills: () => request<{ items: FillHistoryItem[] }>("/api/trades/fills?limit=100"),
  pnlRealizations: () => request<{ items: PnlRealization[] }>("/api/trades/pnl?limit=100"),
  fundingIncome: () => request<{ items: FundingIncome[] }>("/api/trades/funding-income?limit=100"),
  transfers: () => request<{ items: InternalTransfer[] }>("/api/transfers"),
  createTransfer: (
    value: {
      exchange: Exchange;
      environment: Environment;
      direction: "spot_to_perp" | "perp_to_spot";
      amount_usdt: string;
      confirmed: true;
    },
    idempotencyKey: string,
  ) => request<{ created: boolean; transfer: InternalTransfer }>("/api/transfers", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(value),
  }),
  automation: () => request<AutomationStatus>("/api/automation"),
  saveAutomationConfig: (value: AutoStrategyConfig) =>
    request<{ strategy: AutomationStatus["latest_strategy"] }>("/api/automation/config", {
      method: "PUT",
      body: JSON.stringify(value),
    }),
  enableAutomation: (strategyId: string) => request<AutomationStatus>("/api/automation/enable", {
    method: "POST",
    body: JSON.stringify({ strategy_id: strategyId, confirmed: true }),
  }),
  pauseAutomation: (reason: string) => request<AutomationStatus>("/api/automation/pause", {
    method: "POST",
    body: JSON.stringify({ reason }),
  }),
  resumeAutomation: () => request<AutomationStatus>("/api/automation/resume", { method: "POST", body: "{}" }),
  disableAutomation: () => request<AutomationStatus>("/api/automation/disable", { method: "POST", body: "{}" }),
  previewOpen: (value: {
    exchange: Exchange;
    environment: Environment;
    base_asset: string;
    notional_usdt: string;
    leverage: number;
    maximum_slippage: string;
  }) => request<{ preview_id: string; preview: LiveOpenPreview }>("/api/trades/open/preview", {
    method: "POST",
    body: JSON.stringify(value),
  }),
  confirmOpen: (previewId: string, idempotencyKey: string) =>
    request<{ created: boolean; intent: { id: string; status: string } }>("/api/trades/open/confirm", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ preview_id: previewId, confirmed: true }),
    }),
  previewClose: (positionId: string, emergency: boolean, maximumSlippage: string) =>
    request<{ preview_id: string; preview: LiveClosePreview }>(`/api/trades/positions/${positionId}/close/preview`, {
      method: "POST",
      body: JSON.stringify({ emergency, maximum_slippage: maximumSlippage }),
    }),
  confirmClose: (positionId: string, previewId: string, idempotencyKey: string) =>
    request<{ created: boolean; intent: { id: string; status: string } }>(`/api/trades/positions/${positionId}/close/confirm`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ preview_id: previewId, confirmed: true }),
    }),
};
