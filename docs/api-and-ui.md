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
  私有流就绪状态、远端结果完整性、挂单数和仓位数。
- `POST /api/trades/paper/open`：使用当前健康机会持久化纸面开仓意图和现货买入/永续卖出双腿；
  必须提供 UUID `Idempotency-Key`，随后由唯一 worker 原子模拟双腿 taker 成交。
- `GET /api/trades/intents/{uuid}`：读取交易意图、版本和双腿状态。
- `GET /api/trades/intents/{uuid}/fills`：读取该意图的成交和手续费。
- `GET /api/trades/positions?status=open`：读取配对仓位。
- `POST /api/trades/paper/positions/{uuid}/close`：使用现货 bid 卖出及永续 ask reduce-only
  买回计划纸面平仓，同样要求 UUID `Idempotency-Key`。
- `POST /api/trades/open/preview`：为已配置凭据的 `sandbox`/`live` 账户生成 15 秒真实开仓预览票据，
  返回双腿参考价/保护价、原生数量、合约乘数、预计费用、现货余额需求、永续保证金需求及最坏基差。
- `POST /api/trades/open/confirm`：请求体必须对预览票据显式发送 `confirmed=true`，同时提供 UUID
  `Idempotency-Key`；仅在全局执行状态为 `ready` 且票据仍匹配当前行情时持久化真实 `planned` 意图。
- `POST /api/trades/positions/{uuid}/close/preview`：为既有 `sandbox`/`live` 配对仓位生成 15 秒
  平仓预览，展示现货卖出与永续 reduce-only 买回的参考价、保护价、原生数量、合约乘数、预计费用、
  毛盈亏及扣除剩余开仓费后的预计净盈亏。
- `POST /api/trades/positions/{uuid}/close/confirm`：要求显式 `confirmed=true`、UUID
  `Idempotency-Key`、全局 `ready` 及未变化的仓位/行情指纹；只持久化 `planned` 平仓意图。
- `GET /api/automation`：读取独立于账户执行状态的自动交易状态、当前生效策略和最新草稿版本。
- `PUT /api/automation/config`：保存新的不可变完整策略版本；不会自动启用或修改旧版本。
- `POST /api/automation/enable`：要求策略 UUID、`confirmed=true`、全局执行 `ready`，并确认所有目标
  交易所在策略环境中已配置凭据。
- `POST /api/automation/pause`、`/resume`、`/disable`：暂停、重新验证后恢复或禁用自动交易；
  不把暂停等同于紧急清仓。

写入接口只接受已认证且 CSRF 校验通过的请求。明文只在单次请求内进入内存，随后使用绑定交易所与环境的
AES-GCM 关联数据加密；响应、审计事件和日志均不得包含 API Secret、passphrase 或完整 API Key。
`paper` 环境不接受交易所凭据。

账户快照使用各所官方只读接口和签名规则。签名错误、超时及 HTTP 错误统一映射为不带请求 URL、
签名参数或响应原文的脱敏错误。MEXC 和 Gate 没有满足同所现货+USDT 永续完整验收要求的沙盒，
其 `sandbox` 快照明确返回不支持，不会回退到实盘地址。Bybit V5 不直接返回无持仓标的的全局持仓模式，
因此当前快照如实返回 `unknown`；模式未知时后续状态机必须禁止下单，不能按默认值猜测。
OKX 快照从账户配置的 `perm` 确认 `trade`；Bybit 从当前 API Key 信息同时确认非只读、SpotTrade
和 ContractTrade Order 权限。缺少权限返回 `false`，接口未提供字段则保持 `unknown`。
Bitget UTA 从 `/api/v3/account/info` 要求 `permType=read-and-write`，并同时具有 `uta_trade` 与
`uta_mgt`；Classic 从 `/api/v2/spot/account/info` 同时要求现货交易、合约订单和合约持仓写权限。
Gate 从 `/api/v4/account/main_keys` 对当前 Key 做唯一的完整或脱敏前缀匹配，要求 Key 正常、没有
交易对白名单且 spot/futures 均非只读；无法读取主 Key 清单、匹配不唯一或存在交易对白名单时保持
`unknown`，不会把失败的权限探测扩大成余额接口失败。MEXC 只有在现货账户明确 `canTrade=true`，且
官方标注需要 Trading 权限的合约持仓模式查询成功时确认双腿权限。
当前 worker 只完成余额、权益及账户模式快照；在挂单、成交和仓位的 REST/私有流对账完成前，
全局执行状态固定为 `blocked`，该状态不能由 API 绕过。

私有适配层已能统一读取六所当前现货/永续挂单及 USDT 永续仓位，但明细尚未作为公开 HTTP 接口返回。
Bybit 游标会读取到末页；其余接口一旦达到单页上限或交易所声明的总数超过本页，统一标记结果不完整，
不得用截断结果通过启动对账。worker 会持久化这些远端明细，并将任何未匹配的挂单或仓位列为阻断原因；
成交仍需按本地客户端订单 ID 和时间窗口关联后才能构成完整真相。
挂单匹配同时使用交易所订单 ID 与客户端订单 ID，并核对市场、标的、方向、原生数量和 reduce-only；
已匹配但尚未终结的 IOC 仍禁止新交易。仓位匹配将本地配对仓位基础币数量按开仓腿合约乘数还原为交易所
原生数量，并核对空头方向、逐仓和杠杆；完全匹配的既有套利仓位不再仅因“账户有仓位”被误判为未知，
但额外、缺失或冲突仓位仍会在 `/api/system/execution` 的账户原因中阻断。
六所私有客户端现已提供逐订单成交 REST 查询，统一输出交易所成交/订单 ID、客户端订单 ID、市场、
标的、方向、价格、数量、费用资产、标准化费用和 maker/taker 时间。OKX、Bitget 等原始负数扣费统一
转换为正数成本、正数返佣转换为负数；触及单页上限或交易所要求的订单 ID 尚未知时返回
`complete=false`，不得据此把订单标记为已完整对账。该能力目前仅在私有适配层，尚未作为 HTTP 接口
公开，也尚未解除 worker 的全局阻断。worker 会对非终态真实订单腿调用该接口，校验市场、标的、方向、
客户端 ID 和交易所订单 ID 后幂等写入本地 `fills`，再由全部成交重算订单腿累计数量、加权均价及状态。
`GET /api/system/execution` 的账户项包含 `fill_reconciliation_complete` 和 `fill_count`；分页不完整
或缺少必需的交易所订单 ID 时前者为 `false`。账户项同时包含 `private_stream_ready`；它只在认证完成、
订单/成交/仓位三类订阅全部成功且最近心跳不超过 30 秒时为 `true`。Binance、OKX、Bybit、Bitget、
Gate LIVE 与 MEXC LIVE 的认证连接均已装配到常驻 worker。任一私有事件会合并唤醒同一执行器锁内的
严格 REST 对账，快速更新订单、成交和仓位账本；固定周期对账仍作为漏事件与断线恢复路径。
当全部已配置账户的余额、交易权限、持仓模式、远端挂单/成交/仓位关联以及私有流新鲜度都通过时，
worker 才会把账户项和全局状态置为 `ready`；任一账户失败或阻断都会保持 `blocked`，已有的
`paused` 安全状态优先且不会被普通对账清除。
六所客户端也可按客户端订单 ID 查询单笔订单。worker 对明确进入已提交状态但缺少交易所订单 ID 的
本地订单腿执行恢复，并对已经关联但仍非终态的 IOC 持续刷新；两种情况都严格核对市场、标的、方向、
原生数量及 reduce-only，`created` 订单不会被误当成 ACK 丢失订单。部分成交后撤销的 IOC 保留撤销
终态和累计成交，不会被重新标成活动订单。查不到订单仍是不确定状态，禁止自动重发。执行状态账户项新增
`order_reconciliation_complete` 和 `recovered_order_count`，用于区分查单完整性与成交完整性。

交易意图的订单腿响应包含原生 `quantity`、`filled_quantity` 与 `base_multiplier`，基础币数量按
“原生数量 × 乘数”计算。现货乘数为 1；按张下单的永续乘数来自已持久化标的目录，前后端均不得假定
永续原生数量就是基础币数量。意图响应同时返回计划时固化的 `leverage`。

纸面开仓计划只接受 15 秒内的 `healthy` 行情，且名义金额不得超过当前两腿最优档容量。服务在任何执行前
写入交易意图、配置哈希和两腿唯一客户端订单 ID；重复 UUID 加相同请求返回原意图，不同请求复用 UUID
返回冲突。状态更新使用版本号乐观锁，禁止跳过既定状态。当前接口不会直接成交或发送交易所订单。
内部账本已经能生成 `sandbox`/`live` 开仓计划：使用完整标的规则向下取整共同数量，按最大滑点生成两腿
保护价，并生成满足 OKX 纯字母数字、Gate `t-` 前缀及其他交易所长度约束的客户端订单 ID。HTTP 预览
不会创建意图；确认要求同一管理员、未过期且行情/配置指纹未变化的持久化票据、显式确认布尔值、全局
`ready` 和唯一 UUID 幂等键。内部真实执行器已
实现“先原子落库双腿 submitted、再并行发单、逐腿保存 ACK”的崩溃安全边界，只有全局 `ready` 才工作；
任一 ACK 不确定时进入 `unknown` 和全局暂停，后续只查单而不重发。
常驻 worker 已在每轮严格 REST 对账前调用该真实执行器，因此未来 HTTP 确认只需安全持久化
`planned` 意图，不需要也不得由 API 进程直接访问交易所写接口。
预览票据绑定管理员、交易参数和行情指纹，只在 15 秒内允许原子预留一个 UUID 幂等键；票据本身不是
交易意图，worker 不会读取它。API 进程在确认时只持久化 `planned` 意图，不持有交易所写职责。
平仓票据还绑定 `paired_position_id`，确认事务用仓位行锁把仓位置为 `closing`，从而阻止对同一仓位并发
确认。平仓数量必须与仓位剩余基础币数量完全一致，并使用开仓永续腿持久化的合约乘数；当前目录的乘数或
步长发生不兼容变化时拒绝生成预览。重复使用同一票据和幂等键返回原意图，换键、换仓位或行情变化均拒绝。
唯一 worker 会消费已确认的真实平仓意图；发单前要求没有远端挂单，并把全部本地配对空仓按标的、原生
数量、杠杆和逐仓模式与远端完整快照精确匹配。现货腿只能卖出且不得 reduce-only，永续腿只能
reduce-only 买回。任一不一致都在接触写接口前暂停。
真实成交只在两腿 IOC 均终结且 REST 成交分页完整后结算；原生成交量先按订单腿乘数换算为基础币。
等量非零成交创建配对仓位，双零成交将意图置为失败；单腿或数量失衡、以及无法用 USDT 或基础币成交价
折算的手续费，都会进入 `manual_review` 并保持全局暂停。当前尚未实现失衡腿自动补偿。
真实平仓使用相同的终态和分页完整性门槛。等量成交按真实加权均价计算现货与永续价差，扣除按数量分摊
的剩余开仓费及本次真实平仓费；完整成交关闭仓位，等量部分成交递减仓位并解除本次 closing 锁，以便
再次预览剩余数量。双零成交恢复仓位为 open 并安全失败；失衡、超量或费用无法折算保持仓位 closing、
进入 `manual_review` 并暂停。
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

自动策略配置没有危险默认值：环境和至少一个目标交易所、1–10 倍杠杆、每笔名义额、单所/全局敞口、
最大并发、当前/24h/7d 年化阈值、最低净收益、最大开仓基差、最低两腿名义额、盘口容量倍数、正常/紧急
滑点、每日最大亏损、重入间隔、最长持有、最低清算缓冲、资金费/净收益/基差平仓阈值及止盈止损必须
全部提交并通过交叉校验。保存配置只创建新版本。后端已有确定性的单动作自动评估器，但当前 worker
尚未调用它生成开平仓意图。

界面使用简体中文与高对比度浅色主题，API 字段、代码枚举和交易所名保持英文。正收益、健康状态和当前选中机会使用绿色语义，负收益使用红色，次级说明使用深灰色，保证白色背景上的可读性。费用仅是可编辑估算，页面必须提示地区、账户等级和活动费率可能不同。

扫描设置中的每所候选数允许 10–500，默认 500；保存后由后端统一校验。
