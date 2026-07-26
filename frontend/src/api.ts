import type { ExchangeStatus, Opportunity, Settings } from "./types";

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
};
