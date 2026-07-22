import type { ExchangeStatus, Opportunity, Settings } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

export const api = {
  opportunities: () => request<{ items: Opportunity[]; sequence: number }>("/api/opportunities?page_size=300"),
  statuses: () => request<{ items: ExchangeStatus[] }>("/api/exchanges/status"),
  settings: () => request<Settings>("/api/settings"),
  saveSettings: (value: Settings) => request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(value) }),
  history: (item: Opportunity, range: string) =>
    request<{ items: Opportunity[] }>(`/api/opportunities/${item.exchange}/${item.base_asset}/history?range=${range}`),
};
