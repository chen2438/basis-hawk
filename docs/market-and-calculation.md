# 行情与计算

每个适配器只访问官方公共 REST API，并输出共同的 `InstrumentPair`、`MarketQuote` 和 `FundingObservation`。只接受状态正常、现货计价为 USDT、永续计价与结算均为 USDT 的线性永续。

候选池按 `min(spot_quote_volume_24h, perp_quote_volume_24h)` 降序确定。可执行开仓价格是现货 ask 与永续 bid；可执行基差为 `perp_bid / spot_ask - 1`，最优档名义容量取两腿最优档 USDT 名义量的较小值。

当前资金费年化为 `rate × 24 / interval_hours × 365`。历史窗口年化按窗口内已结算费率之和除以实际覆盖天数再乘 365。30 天预计净收益使用近 7 天日均资金费外推 30 天，再减去两腿开仓和平仓 taker 费用。基差不计入预计净收益。

价格超过 15 秒、资金费超过 10 分钟或任一关键字段缺失时标记为 `stale`；近 7 天历史未覆盖至少 6 天时标记为 `warming`。失效机会不会进入默认 `healthy` 排名。
