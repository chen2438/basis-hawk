import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

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
        : url.includes("accounts/credentials") ? { items: [] }
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

  it("renders the read-only Chinese scanner", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("资金费机会，一眼看清。")).toBeTruthy());
    expect(screen.getByText("机会排行榜")).toBeTruthy();
    expect(screen.getByText("admin")).toBeTruthy();
    expect(screen.getAllByText("Bitget").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Gate").length).toBeGreaterThan(0);
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
    await user.click(await screen.findByRole("button", { name: "运营控制台" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "运营控制台" })).toBeTruthy());
    expect(screen.getByText("实盘运营控制台")).toBeTruthy();
    expect(screen.getByText("worker pending")).toBeTruthy();
    expect(screen.getByRole("button", { name: "交易所账户" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "软件更新" })).toBeTruthy();
    expect(screen.getByText("发现新版本")).toBeTruthy();
    expect((screen.getByRole("button", { name: "立即更新" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getAllByRole("button", { name: "删除旧备份" })[0] as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getAllByRole("button", { name: "删除旧备份" })[1] as HTMLButtonElement).disabled).toBe(false);
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
    await user.click(await screen.findByRole("button", { name: "运营控制台" }));
    await user.click(await screen.findByRole("button", { name: "手动交易" }));
    expect(screen.getByText("手动配对开仓")).toBeTruthy();
    expect(screen.getByText("真实配对持仓平仓")).toBeTruthy();
    expect((screen.getByRole("button", { name: "生成开仓预览" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/先生成 15 秒预览票据/)).toBeTruthy();
  });

  it("shows every automatic strategy risk group without enabling by default", async () => {
    render(<App />);
    const user = userEvent.setup();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(await screen.findByRole("button", { name: "运营控制台" }));
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
    await user.click(await screen.findByRole("button", { name: "运营控制台" }));
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
    await user.click(await screen.findByRole("button", { name: "运营控制台" }));
    await user.click(await screen.findByRole("button", { name: "交易账本" }));
    expect(screen.getByRole("heading", { name: "交易意图" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "订单腿" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "成交明细" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "已实现盈亏" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "实际资金费" })).toBeTruthy();
    expect(screen.getByText(/最近 100 条持久化开平仓请求/)).toBeTruthy();
  });
});
