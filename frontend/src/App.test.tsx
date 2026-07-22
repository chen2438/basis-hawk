import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

class FakeSocket { onmessage: ((event: { data: string }) => void) | null = null; close() {} }

describe("Basis Hawk dashboard", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const value = url.includes("opportunities?") ? { items: [], sequence: 0 }
        : url.includes("status") ? { items: [] }
        : { universe_size: 100, minimum_quote_volume: "1000000", holding_period_days: 30, retention_days: 30, fee_checked_at: "2026-07-23", fees: { binance: { spot_taker: "0.001", perp_taker: "0.0005" }, okx: { spot_taker: "0.001", perp_taker: "0.0005" }, mexc: { spot_taker: "0.0005", perp_taker: "0.0004" }, bybit: { spot_taker: "0.001", perp_taker: "0.00055" } } };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(value) });
    }));
  });

  it("renders the read-only Chinese scanner", async () => {
    render(<App />);
    expect(screen.getByText("资金费机会，一眼看清。")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("机会排行榜")).toBeTruthy());
    expect(screen.getByText("只读模式")).toBeTruthy();
  });
});
