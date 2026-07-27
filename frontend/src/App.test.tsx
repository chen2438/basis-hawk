import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { apiErrorMessage } from "./api";

class FakeSocket { onmessage: ((event: { data: string }) => void) | null = null; close() {} }

describe("Basis Hawk dashboard", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const value = url.includes("auth/session") ? { username: "admin" }
        : url.includes("opportunities?") ? { items: [], sequence: 0 }
        : url.includes("exchanges/status") ? { items: [] }
        : url.includes("system/execution") ? { state: "blocked", reason: "worker pending", updated_at: null, accounts: [] }
        : url.includes("operations/audit") ? { items: [] }
        : url.includes("operations/notifications") ? { items: [] }
        : url.includes("operations/update") ? {
          enabled: true,
          state: "update_available",
          current_commit: "1111111111111111111111111111111111111111",
          available_commit: "2222222222222222222222222222222222222222",
          request_id: null,
          checked_at: "2026-07-27T00:00:00Z",
          completed_at: null,
          error_code: null,
        }
        : url.includes("operations/backup") ? {
          directory_available: true,
          archive_count: 2,
          latest: { name: "basis-hawk-20260727T000000Z-daily.bhbk", size_bytes: 1024, modified_at: "2026-07-27T00:00:00Z", checksum_present: true },
          archives: [
            { name: "basis-hawk-20260727T000000Z-daily.bhbk", size_bytes: 1024, modified_at: "2026-07-27T00:00:00Z", checksum_present: true, latest: true },
            { name: "basis-hawk-20260726T000000Z-daily.bhbk", size_bytes: 1024, modified_at: "2026-07-26T00:00:00Z", checksum_present: true, latest: false },
          ],
        }
        : url.includes("accounts/credentials") ? { items: [{
          exchange: "bybit",
          environment: "live",
          label: "primary",
          masked_api_key: "abcd…wxyz",
          position_mode: null,
          updated_at: "2026-07-27T00:00:00Z",
        }] }
        : url.includes("accounts/binance/live/snapshot") ? {
          exchange: "binance",
          environment: "live",
          observed_at: "2026-07-27T10:00:00Z",
          spot_usdt_available: "11.25",
          perp_usdt_available: "8.5",
          perp_usdt_equity: "9.75",
          shared_balance: false,
          account_mode: "SPOT",
          position_mode: "hedge",
          trade_permission: true,
        }
        : url.includes("trades/positions") ? { items: [] }
        : url.includes("trades/intents") ? { items: [] }
        : url.includes("trades/orders") ? { items: [] }
        : url.includes("trades/fills") ? { items: [] }
        : url.includes("trades/pnl") ? { items: [] }
        : url.includes("trades/funding-income") ? { items: [] }
        : url.includes("transfers") ? { items: [] }
        : url.includes("automation") ? { state: "disabled", reason: "disabled", updated_by: "system", updated_at: "2026-07-26T00:00:00Z", active_strategy: null, latest_strategy: null }
        : { universe_size: 500, minimum_quote_volume: "1000000", holding_period_days: 30, retention_days: 30, fee_checked_at: "2026-07-23", fees: { binance: { spot_taker: "0.001", perp_taker: "0.0005" }, okx: { spot_taker: "0.001", perp_taker: "0.0005" }, mexc: { spot_taker: "0.0005", perp_taker: "0.0004" }, bybit: { spot_taker: "0.001", perp_taker: "0.00055" }, bitget: { spot_taker: "0.001", perp_taker: "0.0006" }, gate: { spot_taker: "0.001", perp_taker: "0.00075" } } };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(value) });
    }));
  });

  it("turns a minimum-notional API error into an actionable Chinese message", () => {
    expect(apiErrorMessage({
      code: "notional_below_minimum",
      message: "requested notional is below the minimum executable amount",
      minimum_notional_usdt: "5.15500",
    }, "操作失败")).toBe(
      "名义金额过低；该标的当前至少需要 5.15500 USDT（受现货与永续共同数量步长及交易所最低下单规则限制）",
    );
  });

  it("turns a top-book capacity error into an actionable Chinese message", () => {
    expect(apiErrorMessage({
      code: "notional_exceeds_top_book",
      message: "notional exceeds current top-book capacity",
      capacity_notional_usdt: "2.0553",
    }, "操作失败")).toBe(
      "名义金额超过当前一档双腿可执行容量；当前最多可下 2.0553 USDT，盘口会实时变化，建议输入略低于该值的金额后重试",
    );
  });

  it("turns an invalidated trade preview into a Chinese retry instruction", () => {
    expect(apiErrorMessage(
      "market or configuration changed after trade preview",
      "操作失败",
    )).toBe("交易规则或配置已发生变化，旧预览已失效，请重新生成预览");
    expect(apiErrorMessage(
      "market moved beyond preview slippage protection",
      "操作失败",
    )).toBe("行情已超出预览中设置的最大滑点保护，请重新生成预览，或在确认风险后适当提高最大滑点");
    expect(apiErrorMessage(
      "trade preview has expired",
      "操作失败",
    )).toBe("预览票据已超过 60 秒有效期，请重新生成预览");
  });

  it("renders the read-only Chinese scanner", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("资金费机会，一眼看清。")).toBeTruthy());
    expect(screen.getByText("机会排行榜")).toBeTruthy();
    expect(screen.getByText("admin")).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "主菜单" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "市场总览" })).toBeTruthy();
    expect(screen.getAllByText("Bitget").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Gate").length).toBeGreaterThan(0);
  });

  it("loads and shows each opening-leg top-book capacity", async () => {
    const opportunity = {
      exchange: "gate",
      base_asset: "龙虾",
      spot_symbol: "龙虾_USDT",
      perp_symbol: "龙虾_USDT",
      observed_at: "2026-07-27T14:00:00Z",
      spot_ask: "0.020439",
      perp_bid: "0.020553",
      executable_basis: "0.0055",
      top_book_notional: "0",
      spot_ask_notional: "0",
      perp_bid_notional: "2.0553",
      current_funding_rate: "0.0001",
      funding_interval_hours: "4",
      next_funding_at: null,
      current_apr: "0.219",
      apr_24h: "0.2",
      apr_7d: "0.2",
      net_return: "0.01",
      spot_quote_volume_24h: "1000000",
      perp_quote_volume_24h: "1000000",
      spot_taker_fee: "0.001",
      perp_taker_fee: "0.00075",
      quality: "healthy",
    };
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const value = url.includes("auth/session") ? { username: "admin" }
        : url.includes("/top-book") ? {
          ...opportunity,
          top_book_notional: "2.0553",
          spot_ask_notional: "46.684676",
        }
        : url.includes("/history") ? { items: [] }
        : url.includes("opportunities?") ? { items: [opportunity], sequence: 1 }
        : url.includes("exchanges/status") ? { items: [] }
        : { universe_size: 500, minimum_quote_volume: "1000000", holding_period_days: 30, retention_days: 7, fee_checked_at: "2026-07-23", fees: {} };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(value) });
    }));

    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByText("龙虾"));
    await waitFor(() => expect(screen.getByText("现货卖一容量")).toBeTruthy());
    expect(screen.getByText("46.6847 USDT")).toBeTruthy();
    expect(screen.getAllByText("2.0553 USDT").length).toBe(2);
    expect(fetch).toHaveBeenCalledWith(
      "/api/opportunities/gate/%E9%BE%99%E8%99%BE/top-book",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("shows the administrator login when no session exists", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: false,
      status: 401,
      text: () => Promise.resolve('{"detail":"authentication required"}'),
    })));
    render(<App />);
    await waitFor(() => expect(screen.getByText("登录控制台")).toBeTruthy());
    expect(screen.getByText("动态验证码")).toBeTruthy();
  });

  it("opens the operational console with safety state", async () => {
    render(<App />);
    const user = userEvent.setup();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "执行状态" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "运营控制台" })).toBeTruthy());
    expect(screen.getByRole("heading", { name: "执行状态" })).toBeTruthy();
    expect(screen.getByText("worker pending")).toBeTruthy();
    expect(screen.getByRole("button", { name: "交易所账户" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "软件更新" })).toBeTruthy();
    expect(screen.getByText("发现新版本")).toBeTruthy();
    expect((screen.getByRole("button", { name: "立即更新" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getAllByRole("button", { name: "删除旧备份" })[0] as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getAllByRole("button", { name: "删除旧备份" })[1] as HTMLButtonElement).disabled).toBe(false);
    const batchDelete = screen.getByRole("button", { name: "批量删除已选（0）" }) as HTMLButtonElement;
    expect(batchDelete.disabled).toBe(true);
    expect((screen.getByRole("checkbox", { name: /20260727/ }) as HTMLInputElement).disabled).toBe(true);
    await user.click(screen.getByRole("checkbox", { name: /20260726/ }));
    await user.click(screen.getByRole("button", { name: "批量删除已选（1）" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/operations/backups/batch-delete",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          archive_names: ["basis-hawk-20260726T000000Z-daily.bhbk"],
          confirmed: true,
        }),
      }),
    ));
    const reconcile = screen.getByRole("button", { name: "重新对账" }) as HTMLButtonElement;
    expect(reconcile.disabled).toBe(false);
    await user.click(reconcile);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/system/execution/resume",
      expect.objectContaining({ method: "POST" }),
    ));
    confirmation.mockRestore();
  });

  it("shows bounded manual paired-trade controls", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "手动交易" }));
    expect(screen.getByText("手动配对开仓")).toBeTruthy();
    expect(screen.getByText("真实配对持仓平仓")).toBeTruthy();
    expect((screen.getByRole("button", { name: "生成开仓预览" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/先生成 60 秒预览票据/)).toBeTruthy();
  });

  it("lets an existing Bybit account declare its empty-account position mode", async () => {
    render(<App />);
    const user = userEvent.setup();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "交易所账户" }));
    expect(screen.getByRole("combobox", { name: "Bybit 持仓模式" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "保存模式声明" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/accounts/bybit/live/position-mode",
      expect.objectContaining({ method: "PUT" }),
    ));
    confirmation.mockRestore();
  });

  it("shows the selected exchange spot and perpetual balances on the transfer page", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "内部划转" }));
    expect(await screen.findByRole("region", { name: "划转账户余额" })).toBeTruthy();
    await waitFor(() => expect(screen.getByText("11.25 USDT")).toBeTruthy());
    expect(screen.getByText("8.5 USDT")).toBeTruthy();
    expect(screen.getByText("9.75 USDT")).toBeTruthy();
    expect(screen.getByText("本次来源账户")).toBeTruthy();
    expect(screen.getByText("独立余额")).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith(
      "/api/accounts/binance/live/snapshot",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("shows every automatic strategy risk group without enabling by default", async () => {
    render(<App />);
    const user = userEvent.setup();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "自动策略" }));
    expect(screen.getByText("自动策略完整配置")).toBeTruthy();
    expect(screen.getByText("资金与仓位")).toBeTruthy();
    expect(screen.getByText("开仓门槛")).toBeTruthy();
    expect(screen.getByText("退出与时间")).toBeTruthy();
    expect((screen.getByRole("button", { name: "启用最新策略" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/自动交易保持 disabled/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "保存新版本" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/automation/config",
      expect.objectContaining({ method: "PUT" }),
    ));
    const saveCall = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/automation/config");
    expect(JSON.parse(String((saveCall?.[1] as RequestInit).body)).enabled_exchanges).toEqual(["binance"]);
    confirmation.mockRestore();
  });

  it("shows redacted audit and notification history workspaces", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "审计与通知" }));
    expect(screen.getByText("管理员审计")).toBeTruthy();
    expect(screen.getByText("通知投递")).toBeTruthy();
    expect(screen.getByText(/敏感键由服务端递归脱敏/)).toBeTruthy();
    expect(screen.getByText(/不返回消息正文或去重键/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "测试 Telegram" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "测试邮件" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "清理旧日志" })).toBeTruthy();
  });

  it("shows bounded trade, fill, pnl, and funding ledgers", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "交易账本" }));
    expect(screen.getByRole("heading", { name: "交易意图" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "订单腿" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "成交明细" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "已实现盈亏" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "实际资金费" })).toBeTruthy();
    expect(screen.getByText(/最近 100 条持久化开平仓请求/)).toBeTruthy();
  });
});
