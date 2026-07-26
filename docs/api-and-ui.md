# API 与界面

生产服务在容器网络监听 `0.0.0.0:8000`，只通过 Caddy 域名 HTTPS 暴露。健康检查与静态登录页公开；
其余 HTTP 和 WebSocket API 均要求管理员会话。当前市场接口为 `/api/opportunities`、单机会
`/history`、`/api/exchanges/status`、`/api/settings` 和 `/api/ws/opportunities`。
所有比例使用小数值字符串，例如 `0.001` 表示 `0.1%`。
机会对象同时返回开仓方向 `spot_ask`/`perp_bid` 与平仓方向 `spot_bid`/`perp_ask`，
以及各自的最优档容量。

管理员使用密码与 TOTP 登录。服务通过 Secure、HttpOnly、SameSite=Strict Cookie 保存会话；
所有修改请求还必须提供与 Cookie 会话绑定的 `X-CSRF-Token`。连续失败登录受限流保护。
前端不会把会话令牌写入 localStorage。

交易所凭据接口为：

- `GET /api/accounts/credentials`：仅返回交易所、环境、标签、更新时间和 API Key 掩码；
- `PUT /api/accounts/{exchange}/{sandbox|live}/credentials`：保存或替换 API Key、Secret，
  OKX/Bitget 还必须提供 passphrase；
- `DELETE /api/accounts/{exchange}/{sandbox|live}/credentials`：删除本地加密凭据。
- `GET /api/accounts/{exchange}/{sandbox|live}/snapshot`：按需解密凭据并从交易所读取 USDT
  现货可用余额、永续可用余额/权益、账户类型和持仓模式；响应仍不包含任何凭据。
- `GET /api/system/execution`：读取 worker 的全局执行阻断状态，以及各账户最近一次启动对账状态、
  远端结果完整性、挂单数和仓位数。
- `POST /api/trades/paper/open`：使用当前健康机会持久化纸面开仓意图和现货买入/永续卖出双腿；
  必须提供 UUID `Idempotency-Key`，随后由唯一 worker 原子模拟双腿 taker 成交。
- `GET /api/trades/intents/{uuid}`：读取交易意图、版本和双腿状态。
- `GET /api/trades/intents/{uuid}/fills`：读取该意图的成交和手续费。
- `GET /api/trades/positions?status=open`：读取配对仓位。
- `POST /api/trades/paper/positions/{uuid}/close`：使用现货 bid 卖出及永续 ask reduce-only
  买回计划纸面平仓，同样要求 UUID `Idempotency-Key`。

写入接口只接受已认证且 CSRF 校验通过的请求。明文只在单次请求内进入内存，随后使用绑定交易所与环境的
AES-GCM 关联数据加密；响应、审计事件和日志均不得包含 API Secret、passphrase 或完整 API Key。
`paper` 环境不接受交易所凭据。

账户快照使用各所官方只读接口和签名规则。签名错误、超时及 HTTP 错误统一映射为不带请求 URL、
签名参数或响应原文的脱敏错误。MEXC 和 Gate 没有满足同所现货+USDT 永续完整验收要求的沙盒，
其 `sandbox` 快照明确返回不支持，不会回退到实盘地址。Bybit V5 不直接返回无持仓标的的全局持仓模式，
因此当前快照如实返回 `unknown`；模式未知时后续状态机必须禁止下单，不能按默认值猜测。
当前 worker 只完成余额、权益及账户模式快照；在挂单、成交和仓位的 REST/私有流对账完成前，
全局执行状态固定为 `blocked`，该状态不能由 API 绕过。

私有适配层已能统一读取六所当前现货/永续挂单及 USDT 永续仓位，但明细尚未作为公开 HTTP 接口返回。
Bybit 游标会读取到末页；其余接口一旦达到单页上限或交易所声明的总数超过本页，统一标记结果不完整，
不得用截断结果通过启动对账。worker 会持久化这些远端明细，并将任何未匹配的挂单或仓位列为阻断原因；
成交仍需按本地客户端订单 ID 和时间窗口关联后才能构成完整真相。
六所私有客户端现已提供逐订单成交 REST 查询，统一输出交易所成交/订单 ID、客户端订单 ID、市场、
标的、方向、价格、数量、费用资产、标准化费用和 maker/taker 时间。OKX、Bitget 等原始负数扣费统一
转换为正数成本、正数返佣转换为负数；触及单页上限或交易所要求的订单 ID 尚未知时返回
`complete=false`，不得据此把订单标记为已完整对账。该能力目前仅在私有适配层，尚未作为 HTTP 接口
公开，也尚未解除 worker 的全局阻断。worker 会对非终态真实订单腿调用该接口，校验市场、标的、方向、
客户端 ID 和交易所订单 ID 后幂等写入本地 `fills`，再由全部成交重算订单腿累计数量、加权均价及状态。
`GET /api/system/execution` 的账户项包含 `fill_reconciliation_complete` 和 `fill_count`；分页不完整
或缺少必需的交易所订单 ID 时前者为 `false`。
六所客户端也可按客户端订单 ID 查询单笔订单。worker 仅对明确进入已提交状态但缺少交易所订单 ID 的
本地订单腿执行该恢复，并在严格核对市场、标的、方向、数量及 reduce-only 后保存关联；`created`
订单不会被误当成 ACK 丢失订单。查不到订单仍是不确定状态，禁止自动重发。执行状态账户项新增
`order_reconciliation_complete` 和 `recovered_order_count`，用于区分查单完整性与成交完整性。

纸面开仓计划只接受 15 秒内的 `healthy` 行情，且名义金额不得超过当前两腿最优档容量。服务在任何执行前
写入交易意图、配置哈希和两腿唯一客户端订单 ID；重复 UUID 加相同请求返回原意图，不同请求复用 UUID
返回冲突。状态更新使用版本号乐观锁，禁止跳过既定状态。当前接口不会直接成交或发送交易所订单。
HTTP 接口本身不直接成交。纸面 worker 使用计划时保存的价格和费率，在同一事务中填满两腿、写入两条
taker 成交并创建配对仓位；
崩溃发生在提交前会保留 `planned` 供重试，提交后再次运行不会重复生成成交。该模型不代表真实撮合，
不会访问交易所，也不用于实盘收益承诺。
纸面平仓只接受匹配标的的最新健康行情及足够的平仓方向容量；完成后持久化平仓费用，并以现货价差加
永续空仓价差减去开平仓双边费用计算 `realized_pnl_usdt`。重复平仓请求不会重复生成成交。
纸面开仓支持为测试注入双腿不同成交比例：只按两腿共同成交量创建配对仓位，多出的现货会反向卖出，
多出的永续空仓会用 reduce-only 买回。补偿订单与成交使用同一意图下的独立订单腿持久化；worker 在
主成交落库后重启可从 `compensating` 继续。补偿失败时意图进入 `manual_review`，全局执行状态进入
`paused`，后续账户对账不得清除该安全暂停。该注入能力不通过生产 HTTP API 暴露。
纸面部分平仓同样先补偿多出的一腿，再只扣减两腿共同成交量。持仓响应中的
`initial_quantity` 保留初始数量，`quantity` 表示剩余数量，
`remaining_opening_fees_usdt` 表示尚未分摊到已实现盈亏的开仓费用；每次安全完成部分平仓后仓位重新
回到 `open`，可用新的幂等键继续平仓，数量归零后才进入 `closed`。

WebSocket 首帧为 `snapshot`，后续帧为带单调 `sequence` 的 `update`；客户端发现序号断层或重连时重新读取 REST 快照。

界面使用简体中文与高对比度浅色主题，API 字段、代码枚举和交易所名保持英文。正收益、健康状态和当前选中机会使用绿色语义，负收益使用红色，次级说明使用深灰色，保证白色背景上的可读性。费用仅是可编辑估算，页面必须提示地区、账户等级和活动费率可能不同。

扫描设置中的每所候选数允许 10–500，默认 500；保存后由后端统一校验。
