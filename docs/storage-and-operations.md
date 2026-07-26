# 存储与运维

生产数据库固定使用 PostgreSQL，URL 由 `BASIS_HAWK_DATABASE_URL` 指定。Alembic 是生产 schema
的唯一迁移入口；应用只为 SQLite 测试数据库自动建表，禁止在 PostgreSQL 启动时隐式 `create_all`。
`instruments` 表持久化六所现货/永续价格和数量步长、最小数量/名义额及永续合约乘数；旧目录记录迁移
后以 0 表示未知，并在下一次公共目录刷新时更新。任一真实下单规划看到未知规则都必须阻断。
Binance、OKX、Bybit、Bitget Classic V2/UTA V3、Gate 及 MEXC 永续配置只接受 1–10 倍杠杆。Binance
切换到逐仓前会查询该标的挂单和仓位；
OKX 在目标逐仓杠杆尚未匹配时也会先检查该标的挂单和仓位。Bybit UTA 2.0 的逐仓是账户级
`ISOLATED_MARGIN`，所以切换前会翻页检查全部 USDT 线性挂单和仓位；任一敞口存在时拒绝改变账户模式或
杠杆。Bybit 通过 `positionIdx` 自动识别单向/双向模式，配置后重新查询账户模式与目标空头侧杠杆，
不能确认时视为失败。Bitget V2 的逐仓和杠杆是标的级配置，修改前检查该标的所有挂单和非零仓位，
并用单账户查询二次确认空头侧逐仓杠杆。Bitget UTA 使用 V3 统一余额、订单、成交和仓位接口；写操作前
必须由 V3 settings 或可确认的 V2 合约账户响应识别账户代际，识别不清、升级中或切换中一律阻断。
UTA 不自动修改账户级模式，只在目标标的没有挂单和仓位时设置空头侧逐仓杠杆，并重新读取
`symbolConfigList` 确认；写接口失败后绝不回退到 Classic V2。OKX 每条下单/撤单响应还必须明确返回成功的子状态码；顶层成功
但单条命令失败或缺失子状态码不能视为已接受。Gate 使用明确指定 `margin_mode=isolated` 的新版
`set_leverage` 接口；双向模式只配置 `dual_short`，修改前检查目标标的挂单与所有非零仓位，响应未返回
目标逐仓模式和杠杆时拒绝继续。MEXC 合约下单和撤单仍被官方标为维护中，因此每个进程必须先调用
`change_leverage` 成功写入空头逐仓配置、再查询确认目标杠杆，才在内存中放行该标的合约下单；任一
写请求失败会立即清除此状态并降级为只读。已有挂单或仓位时禁止通过能力探测改变杠杆。

Docker Compose 当前提供 PostgreSQL、FastAPI、唯一交易 worker 和 Caddy。Caddy 自动管理 TLS，只暴露 80/443；
数据库只在 Compose 网络可见。生产启动顺序为数据库健康检查、`alembic upgrade head`、API 健康检查、
worker 启动对账、Caddy 接入。worker 使用 PostgreSQL advisory lock；同一数据库已有执行器时第二个
worker 会拒绝运行。

首次部署必须配置 32 字节 URL-safe Base64 主密钥
`BASIS_HAWK_CREDENTIAL_MASTER_KEY`，再运行：

```bash
docker compose run --rm api basis-hawk admin-create --username admin
```

管理员密码使用 Argon2id，TOTP 密钥和后续交易所凭据使用 AES-256-GCM；关联数据绑定管理员或交易所环境，
数据库只保存密文、nonce 和密钥版本。主密钥不得写入仓库、日志或数据库。
同一交易所、同一环境只保存一个账户配置；替换和删除都会写入不含秘密值的审计事件。API 读取只能返回
Key 掩码，私有适配器在进程内按需解密，不能把解密结果缓存到数据库或发送给前端。
私有请求的签名查询串必须和实际发送顺序完全一致；异常消息禁止包含完整 URL，因为查询参数可能带签名。
worker 定期写入 `account_snapshots`、`remote_open_order_snapshots`、
`remote_position_snapshots` 和各账户最新 `account_reconciliation` 状态；全局
`execution_control` 在对账开始时进入 `reconciling`。余额、权益、模式、挂单与仓位已接入持久化，
分页不完整、未匹配的远端订单/仓位、成交尚未关联或私有流尚未就绪时，每轮结束都会保持 `blocked`，
不能据此执行交易。
`private_stream_states` 按交易所与环境保存连接、认证、订单/成交/仓位订阅和最近心跳/事件时间；
只有全部三类订阅成功且心跳不超过 30 秒才视为就绪。worker 每次启动都先把旧连接状态重置为断开，
防止把上一个已退出进程的记录当作活连接；全局状态为 `ready` 时任一私有流断开会原子切换为
`paused`，后续必须完成 REST 对账才能恢复。表内只保存通用健康标志和时间，不保存凭据、订阅载荷或
交易所错误原文。当前仅完成该健康状态与阻断基础，六所私有 WebSocket 的认证、续期和事件消费尚未接入。
通用私有流监督器以独立任务管理每个账户连接：收到事件时记录事件心跳，空闲 10 秒时必须由连接适配器
完成真实 ping/pong 探测后才续写健康心跳；异常会先关闭连接、写入断开状态，再以 1–30 秒指数退避
重连。日志只记录交易所和环境，不记录异常正文、URL、签名、订阅载荷或凭据。当前监督器尚未由 worker
创建具体交易所连接。
Binance 私有连接由两条通道组成：现货使用
`userDataStream.subscribe.signature`，USDT 永续使用 `/fapi/v1/listenKey` 后连接私有
WebSocket，并每 30 分钟续期。只有现货签名订阅返回成功且永续 listenKey 与连接都建立后，通用监督器
才可登记三类订阅就绪；任一通道关闭、ping/pong 失败或续期返回不同 listenKey 都使整个 Binance 账户
断开并进入 REST 对账。沙盒使用当前官方 Spot Testnet 与 USDⓈ-M Demo 地址。
常驻 worker 会为每个已配置的 Binance 沙盒或实盘账户创建该连接，并与 60 秒 REST 对账循环并行运行；
`worker --once` 不创建无法持续保活的 WebSocket。当前事件只用于确认连接活动，订单、成交和仓位仍由
严格 REST 对账写入本地账本；其余五所尚未由连接工厂创建私有流，所以仍保持私有流阻断。
每轮对账对每个账户独立汇总阻断原因；只有全部已配置账户都没有原因且没有请求失败，才把账户状态写为
`ready`。写入全局 `ready` 前会再次检查每个账户的私有流心跳，避免把本轮处理中已经陈旧的连接放行。
任一账户为 `blocked`/`error` 或已有补偿失败等安全暂停时，全局状态不会进入 `ready`。
六所私有适配器可以按本地订单腿的交易所订单 ID 查询成交，并明确报告分页是否完整；需要交易所订单 ID
但 ACK 尚未关联时必须继续阻断。远端成交通过
`(order_leg_id, exchange_trade_id)` 唯一约束幂等写入，避免不同交易所可能重复的数字成交 ID 冲突；
写入前强制核对市场、标的、方向和订单 ID，随后从完整本地成交集合重算订单腿累计数量及加权均价。
每个账户最近的 `fill_reconciliation_complete` 和 `fill_count` 随启动快照持久化。
本地订单腿已经处于 `submitted`、`acknowledged`、`partially_filled` 或 `unknown`，但下单 ACK
未能保存交易所订单 ID 时，worker 会先使用持久化的客户端订单 ID 向对应交易所查单；已经有关联 ID 的
非终态 IOC 也在每轮用同一客户端 ID 刷新终态，不能把一次 ACK 当作订单仍然活动。找回结果必须逐项
核对客户端 ID、市场、标的、方向、原始数量和 reduce-only 标记，完全一致才允许关联
`exchange_order_id` 并继续查询成交；单纯查单响应不会把订单标记为已成交，成交状态仍只从幂等成交账本
推导。IOC 在部分成交后撤销时，订单腿保留 `canceled` 终态及真实累计成交量，成交汇总不会把它重新改成
活动的 `partially_filled`。未找到、查询窗口受限或结果不完整时保持阻断，绝不据此重发订单。每个账户同时保存
`order_reconciliation_complete` 和本轮 `recovered_order_count`。

`trade_intents` 在执行前保存幂等键、请求指纹、市场时间、配置哈希、金融数量、状态与乐观锁版本；
真实意图额外固化 1–10 倍请求杠杆，旧记录迁移为安全默认值 1；
`order_legs` 同一意图固定一条现货腿和一条永续腿，并在提交交易所前生成唯一客户端订单 ID；每条腿还
保存严格为正的 `base_multiplier`，使交易所原生数量及成交量可以无歧义换算成基础币。已有纸面订单腿
迁移时乘数为 1；后续真实永续腿必须使用标的目录的合约乘数。
非终态意图可由 worker 按创建时间恢复。纸面 worker 在单一数据库事务中更新双腿、写入 `fills` 并创建
`paired_positions`；唯一交易 ID 和开仓意图约束保证重复运行不会重复成交。真实成交仍必须以交易所
私有流或 REST 查询为准。
真实开仓执行器只读取 `planned` 的 `sandbox`/`live` 意图，并要求全局执行状态已经是 `ready`。发单前
重新读取余额、权限、持仓模式以及完整远端挂单/仓位，当前安全阶段要求账户没有任何挂单或仓位；随后
确认目标永续逐仓杠杆。两条主订单腿必须在同一数据库事务中由 `created` 变为 `submitted`，意图进入
`executing` 后才允许并行调用交易所。两条 ACK 分别核对市场、标的和客户端 ID 后持久化；网络异常、
响应不匹配或 ACK 落库失败均把对应腿置为 `unknown` 并原子暂停全局执行。进程在事务提交后的任意位置
崩溃，都只能通过客户端订单 ID 查单恢复，执行器不会再次提交 `executing` 意图。
REST 成交对账完整且两条主腿都进入 `filled`、`canceled` 或 `failed` 终态后，worker 才尝试结算真实开仓。
两腿原生成交量分别乘以 `base_multiplier`；基础币数量相等且非零时，使用真实加权均价创建
`paired_positions`。USDT 费用直接计价，基础币费用按该笔成交价折算；其他折扣币费用当前无法可靠估值，
因此与任一数量失衡一起进入 `manual_review` 并全局暂停。两腿均为零成交时意图安全失败且不创建仓位。
新产生的暂停会在本轮对账结束时重新读取并保留，不能被通用 `blocked` 状态覆盖。
远端挂单不再只按“非空”粗略判断：worker 同时用交易所订单 ID 和客户端订单 ID 定位本地腿，并核对
市场、标的、方向、原生数量、reduce-only 与本地非终态；双 ID 指向不同本地腿或任何字段冲突都阻断。
已匹配但仍活动的 IOC 同样阻断新交易，等待其终态。远端永续仓位使用本地配对仓位基础币数量除以开仓腿
`base_multiplier` 得到预期原生数量，再核对标的、空头方向、逐仓和开仓意图杠杆；多个同标的本地仓位
只在杠杆一致时聚合。缺失、额外、方向错误或数值不一致的仓位均视为未知敞口。
平仓意图通过 `paired_position_id` 关联原仓位，计划事务用行锁把仓位置为 `closing`；该状态及
`closing_intent_id` 阻止同一仓位并发创建两个平仓意图。完整平仓写入累计平仓费用、已实现净盈亏和
`closed_at`。
纸面开仓的双腿成交量不同时，主成交事务把意图转为 `compensating`，并新增
`spot_compensation` 或 `perp_compensation` 订单腿。下一次 worker 运行可恢复并成交该反向腿，
只按主订单共同成交量创建仓位；全部实际主成交与补偿费用均计入该仓位开仓费用。若补偿失败，意图转为
`manual_review` 并原子写入全局 `paused`，普通启动对账只能保留、不能覆盖此安全暂停。
部分平仓补偿完成后，仅按两腿共同成交量扣减 `quantity`，按平仓比例从
`remaining_opening_fees_usdt` 分摊开仓费，并累计实际主成交/补偿费用和已实现盈亏。仓位仍有余额时
清空当前 `closing_intent_id` 并回到 `open`，因此同一仓位可以保留多个历史平仓意图但始终只有一个
当前平仓意图；`initial_quantity` 始终保存原始建仓数量。

可使用单次命令验证解密、签名、持久化与安全阻断：

```bash
docker compose run --rm worker basis-hawk worker --once
```

`.env.example` 只包含占位符。修改它时不得读取、输出或提交本地 `.env`；交易所 Key 必须禁止提现并绑定
VPS 出口 IP。

CI 对提交信息、后端 Ruff/Pytest 和前端 Vitest/TypeScript/Vite 分别验收。容器层另执行
`docker compose --env-file .env.example config --quiet`；PostgreSQL 可用时还必须实际运行 Alembic
upgrade/current。
