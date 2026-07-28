import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { apiErrorMessage } from "./api";
import {
  executionReason,
  OperationsPanel,
  PositionsView,
  tradeFailureReason,
} from "./OperationsPanel";

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
        : url.includes("trades/intents") ? { items: [{
          id: "failed-intent",
          paired_position_id: null,
          exchange: "gate",
          environment: "live",
          base_asset: "WET",
          action: "open",
          emergency: false,
          status: "failed",
          failure_code: "market_data_expired",
          leverage: 1,
          requested_notional: "60",
          base_quantity: "900",
          created_at: "2026-07-27T18:58:28Z",
          updated_at: "2026-07-27T18:58:28Z",
        }] }
        : url.includes("trades/orders") ? { items: [{
          id: "order-leg",
          trade_intent_id: "failed-intent",
          exchange: "gate",
          environment: "live",
          base_asset: "WET",
          action: "open",
          emergency: false,
          leg: "spot",
          market: "spot",
          symbol: "WET_USDT",
          side: "buy",
          status: "failed",
          failure_code: "gate_invalid_param_value",
          quantity: "900",
          filled_quantity: "900",
          average_price: "0.066",
          reduce_only: false,
          created_at: "2026-07-27T18:58:28Z",
          updated_at: "2026-07-27T18:58:30Z",
        }] }
        : url.includes("trades/fills") ? { items: [{
          id: "fill",
          trade_intent_id: "failed-intent",
          exchange: "gate",
          environment: "live",
          base_asset: "WET",
          action: "open",
          leg: "spot",
          symbol: "WET_USDT",
          side: "buy",
          quantity: "900",
          price: "0.066",
          fee_amount: "0.9",
          fee_asset: "WET",
          liquidity: "taker",
          occurred_at: "2026-07-27T18:58:29Z",
        }] }
        : url.includes("trades/pnl") ? { items: [] }
        : url.includes("trades/funding-income") ? { items: [] }
        : url.includes("transfers/limits") ? {
          per_request_limit_usdt: "0",
          daily_limit_usdt: "0",
          enabled: false,
          updated_by: "environment",
          updated_at: "2026-07-27T00:00:00Z",
        }
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

  it("turns an automatic-trading readiness race into an actionable Chinese message", () => {
    expect(apiErrorMessage(
      "execution is not ready for automatic trading",
      "操作失败",
    )).toBe(
      "全局执行状态刚刚发生变化，当前不能启用或恢复自动交易；页面状态已刷新，请先完成划转或安全对账，并等待执行状态变为“就绪”",
    );
    expect(executionReason(
      "internal account transfer requires balance confirmation",
    )).toBe("内部划转正在等待交易所处理及余额到账确认");
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

  it("translates safe live preflight reasons for execution and ledger views", () => {
    expect(executionReason(
      "live_order_preflight:gate:perp_configuration_failed",
    )).toContain(
      "Gate 实盘订单预检未通过：永续保证金模式或杠杆配置失败",
    );
    expect(tradeFailureReason({
      status: "planned",
      failure_code: "perp_configuration_failed",
    } as Parameters<typeof tradeFailureReason>[0])).toBe(
      "永续保证金模式或杠杆配置失败",
    );
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
    expect((screen.getByRole("button", { name: "立即更新" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/处理完成并恢复“就绪”后才能更新软件/)).toBeTruthy();
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

  it("translates a software update safety-state conflict", () => {
    expect(apiErrorMessage(
      "execution is not ready for software update",
      "fallback",
    )).toContain("不能开始软件更新");
  });

  it("polls execution every three seconds only while reconciling", async () => {
    vi.useFakeTimers();
    try {
      const fallbackFetch = vi.mocked(fetch);
      let executionCalls = 0;
      const controlledFetch = vi.fn(
        (url: string, init?: RequestInit) => {
          if (url.includes("system/execution")) {
            executionCalls += 1;
            const value = executionCalls === 1
              ? {
                  state: "reconciling",
                  reason: "administrator requested a fresh safety reconciliation",
                  updated_at: "2026-07-28T10:37:53Z",
                  accounts: [],
                }
              : {
                  state: "ready",
                  reason: "all configured accounts passed startup reconciliation",
                  updated_at: "2026-07-28T10:38:41Z",
                  accounts: [],
                };
            return Promise.resolve({
              ok: true,
              json: () => Promise.resolve(value),
            });
          }
          return fallbackFetch(url, init);
        },
      );
      vi.stubGlobal("fetch", controlledFetch);

      render(
        <OperationsPanel
          opportunities={[]}
          activeTab="system"
        />,
      );
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByText("reconciling")).toBeTruthy();
      expect(executionCalls).toBe(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(screen.getByText("ready")).toBeTruthy();
      expect(executionCalls).toBe(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000);
      });
      expect(executionCalls).toBe(2);
    } finally {
      vi.useRealTimers();
    }
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

  it("shows price-only unrealized PnL with its executable exit marks", () => {
    render(<PositionsView positions={[{
      id: "position-1",
      opening_intent_id: "intent-1",
      closing_intent_id: null,
      exchange: "gate",
      environment: "sandbox",
      base_asset: "VINE",
      initial_quantity: "100",
      quantity: "100",
      notional_usdt: "0.84",
      leverage: 3,
      spot_entry_price: "0.0084",
      perp_entry_price: "0.0085",
      opening_fees_usdt: "0.01",
      remaining_opening_fees_usdt: "0.01",
      closing_fees_usdt: null,
      realized_pnl_usdt: null,
      spot_exit_price: "0.0086",
      perp_exit_price: "0.0084",
      unrealized_pnl_usdt: "0.03",
      valuation_observed_at: "2026-07-28T20:00:00Z",
      status: "open",
      opened_at: "2026-07-28T19:00:00Z",
      closed_at: null,
    }]} />);
    expect(screen.getByText("未实现 PnL（价格）")).toBeTruthy();
    expect(screen.getByText("名义额（USDT）")).toBeTruthy();
    expect(screen.getByText("0.84 USDT")).toBeTruthy();
    expect(screen.getByText("3×")).toBeTruthy();
    expect(screen.getByText("0.0084 / 0.0086")).toBeTruthy();
    expect(screen.getByText("0.0085 / 0.0084")).toBeTruthy();
    expect(screen.getByText("0.03 USDT")).toBeTruthy();
    expect(screen.getByText(/不含资金费及开平仓手续费/)).toBeTruthy();
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
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "内部划转" }));
    expect(await screen.findByRole("region", { name: "内部划转限额" })).toBeTruthy();
    expect(screen.getByText("已禁用")).toBeTruthy();
    await user.clear(screen.getByRole("spinbutton", { name: "单次最高 USDT" }));
    await user.type(screen.getByRole("spinbutton", { name: "单次最高 USDT" }), "100");
    await user.clear(screen.getByRole("spinbutton", { name: "每日累计最高 USDT" }));
    await user.type(screen.getByRole("spinbutton", { name: "每日累计最高 USDT" }), "500");
    await user.click(screen.getByRole("button", { name: "确认并保存限额" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/transfers/limits",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          per_request_limit_usdt: "100",
          daily_limit_usdt: "500",
          confirmed: true,
        }),
      }),
    ));
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
    confirmation.mockRestore();
  });

  it("shows every automatic strategy risk group without enabling by default", async () => {
    render(<App />);
    const user = userEvent.setup();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "自动策略" }));
    expect(screen.getByText("自动策略完整配置")).toBeTruthy();
    expect(screen.getByText("资金与仓位")).toBeTruthy();
    expect(screen.getByText("开仓门槛")).toBeTruthy();
    expect(screen.getByText("最低开仓基差")).toBeTruthy();
    expect(screen.getByText("退出与时间")).toBeTruthy();
    expect((screen.getByRole("button", { name: "启用最新策略" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/自动交易保持 disabled/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "保存新版本" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/automation/config",
      expect.objectContaining({ method: "PUT" }),
    ));
    const saveCall = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/automation/config");
    const savedConfig = JSON.parse(String((saveCall?.[1] as RequestInit).body));
    expect(savedConfig.enabled_exchanges).toEqual(["binance"]);
    expect(savedConfig.minimum_opening_basis).toBe("0");
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
    expect(screen.getByText("Gate 拒绝了订单参数值")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "成交明细" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "已实现盈亏" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "实际资金费" })).toBeTruthy();
    expect(screen.getByText(/最近 100 条持久化开平仓请求/)).toBeTruthy();
    expect(screen.getByText("行情数据过期，订单未提交")).toBeTruthy();
    expect(screen.getAllByText("failed-i")).toHaveLength(3);
    expect(screen.getAllByText("创建时间")).toHaveLength(2);
    expect(screen.getByText("状态更新时间")).toBeTruthy();
    expect(screen.getByText("成交时间")).toBeTruthy();
  });
});
