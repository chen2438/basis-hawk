import type {
  AccountSnapshot,
  AutomationStatus,
  CredentialSummary,
  Environment,
  Exchange,
  ExchangeStatus,
  ExecutionStatus,
  InternalTransfer,
  LiveClosePreview,
  LiveOpenPreview,
  Opportunity,
  PairedPosition,
  Settings,
} from "./types";

function cookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const value = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return value ? decodeURIComponent(value.slice(prefix.length)) : null;
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
      detail = (JSON.parse(body) as { detail?: string }).detail || detail;
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
  opportunities: () => request<{ items: Opportunity[]; sequence: number }>("/api/opportunities?page_size=300"),
  statuses: () => request<{ items: ExchangeStatus[] }>("/api/exchanges/status"),
  settings: () => request<Settings>("/api/settings"),
  saveSettings: (value: Settings) => request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(value) }),
  history: (item: Opportunity, range: string) =>
    request<{ items: Opportunity[] }>(`/api/opportunities/${item.exchange}/${item.base_asset}/history?range=${range}`),
  execution: () => request<ExecutionStatus>("/api/system/execution"),
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
    value: { label: string; api_key: string; api_secret: string; passphrase?: string },
  ) => request<CredentialSummary>(`/api/accounts/${exchange}/${environment}/credentials`, {
    method: "PUT",
    body: JSON.stringify(value),
  }),
  deleteCredential: (exchange: Exchange, environment: Environment) =>
    request<void>(`/api/accounts/${exchange}/${environment}/credentials`, { method: "DELETE" }),
  accountSnapshot: (exchange: Exchange, environment: Environment) =>
    request<AccountSnapshot>(`/api/accounts/${exchange}/${environment}/snapshot`),
  positions: () => request<{ items: PairedPosition[] }>("/api/trades/positions"),
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
