import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { exchangeMarketUrl } from "./App";
import type { Opportunity, V2Opportunity } from "./types";

const opportunity: V2Opportunity = {
  id: "candidate-1",
  opportunity_type: "funding",
  base_asset: "BTC",
  observed_at: "2026-07-29T12:00:00Z",
  entry_spread: "0.01",
  annualized_return: "0.42",
  projected_return: "0.025",
  estimated_round_trip_fees: "0.003",
  holding_days: 7,
  executable_notional_usdt: "12000",
  legs: [
    {
      account_id: null,
      exchange: "binance",
      market_type: "spot",
      side: "buy",
      symbol: "BTCUSDT",
      price: "100000",
      quote_volume_24h: "10000000",
      funding_rate: null,
      funding_interval_hours: null,
      annualized_funding: null,
      fee_rate: "0.001",
      fee_source: "default",
    },
    {
      account_id: null,
      exchange: "okx",
      market_type: "perpetual",
      side: "sell",
      symbol: "BTC-USDT-SWAP",
      price: "101000",
      quote_volume_24h: "20000000",
      funding_rate: "0.0004",
      funding_interval_hours: "8",
      annualized_funding: "0.438",
      fee_rate: "0.0005",
      fee_source: "actual",
    },
  ],
};

function response(value: unknown, status = 200) {
  return Promise.resolve(new Response(
    status === 204 ? null : JSON.stringify(value),
    { status, headers: { "Content-Type": "application/json" } },
  ));
}

function mockApi() {
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = String(input);
    if (path === "/api/auth/session") return response({ username: "admin" });
    if (path.startsWith("/api/v2/opportunities")) {
      return response({
        type: "funding",
        holding_days: 7,
        observed_at: opportunity.observed_at,
        items: [opportunity],
      });
    }
    if (path.startsWith("/api/v2/execution-tasks") && (!init || init.method !== "POST")) {
      return response({ items: [] });
    }
    if (path === "/api/v2/accounts") return response({ items: [] });
    if (path.startsWith("/api/v2/strategies")) return response({ items: [] });
    if (path === "/api/auth/logout") return response(null, 204);
    return response({ items: [] });
  });
}

describe("Basis Hawk v2 console", () => {
  beforeEach(() => {
    Object.defineProperty(document, "cookie", { value: "", writable: true });
    mockApi();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders the light reference information architecture", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "套利机会" })).toBeTruthy();
    for (const label of [
      "API 密钥", "自动下单", "资金统计", "持仓总览", "总览看板",
      "策略列表", "成交选择", "预警监控", "ADL 监控", "邮件推送",
      "账户设置", "系统管理",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }
    expect(screen.getByText("BASIS HAWK")).toBeTruthy();
  });

  it("shows all three opportunity modes and cross-venue legs", async () => {
    render(<App />);
    expect(await screen.findByText("42.0%")).toBeTruthy();
    expect(screen.getByText(/Binance 现货/)).toBeTruthy();
    expect(screen.getByText(/OKX 合约/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /跨所费率套利/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /基差交易/ })).toBeTruthy();
  });

  it("turns an opportunity into a multi-leg Maker task draft", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "创建任务" }));
    expect(screen.getByRole("heading", { name: "新建自动下单任务" })).toBeTruthy();
    expect(screen.getByDisplayValue("BTC 跨所套利")).toBeTruthy();
    expect(screen.getAllByText(/主腿配置|对冲腿 1/)).toHaveLength(2);
    expect(screen.getByDisplayValue("BTC-USDT-SWAP")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "＋ 添加对冲腿" }));
    expect(screen.getByRole("heading", { name: "对冲腿 2" })).toBeTruthy();
  });

  it("navigates to portfolio and dashboard read models", async () => {
    render(<App />);
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "套利机会" });
    await user.click(screen.getByRole("button", { name: "策略列表" }));
    expect(await screen.findByRole("heading", { name: "我的策略" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "总览看板" }));
    expect(await screen.findByRole("heading", { name: "总览看板" })).toBeTruthy();
    expect(screen.getByText("运行中策略")).toBeTruthy();
  });

  it("uses the expected exchange market host", () => {
    const legacy = {
      exchange: "gate",
      perp_symbol: "BTC_USDT",
    } as Opportunity;
    expect(exchangeMarketUrl(legacy, "sandbox")).toContain("testnet.gate.com");
  });

  it("filters opportunity symbols", async () => {
    render(<App />);
    const input = await screen.findByRole("textbox", { name: "搜索币种" });
    fireEvent.change(input, { target: { value: "ETH" } });
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        expect.stringContaining("search=ETH"),
        expect.anything(),
      );
    });
  });
});
