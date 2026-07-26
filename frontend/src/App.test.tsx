import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

class FakeSocket { onmessage: ((event: { data: string }) => void) | null = null; close() {} }

describe("Basis Hawk dashboard", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const value = url.includes("auth/session") ? { username: "admin" }
        : url.includes("opportunities?") ? { items: [], sequence: 0 }
        : url.includes("status") ? { items: [] }
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
});
