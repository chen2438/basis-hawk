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
OKX 账户快照读取当前 Key 的 `perm`，只有包含 `trade` 才确认可交易；Bybit 额外调用当前 Key 信息，
要求 `readOnly=0`、现货含 `SpotTrade` 且合约含 `Order`。明确只读或缺任一权限写为 `false`，响应
缺字段则写为 `unknown`，不能把成功读取余额等同于具有双腿写权限。
Bitget UTA 读取无需额外权限的当前账号信息，要求 Key 为读写且同时有 `uta_trade`、`uta_mgt`；
Classic 要求 authorities 同时含 `stow`、`coow`、`cpow`。Gate 查询主账号 Key 清单，以当前完整 Key
或官方脱敏前缀唯一匹配，要求状态正常、未设置交易对白名单，并且 `spot`、`futures` 的 `read_only`
均为 false；接口不可用、匹配不唯一或白名单无法在账号级证明覆盖全部扫描标的时保持未知。MEXC 现货
必须明确 `canTrade=true`，同时合约 `position_mode` 查询成功；官方将后者标记为需要 Trading 权限，
因此可作为不会下单的合约写权限探测。任一字段缺失都不按成功猜测。
worker 定期写入 `account_snapshots`、`remote_open_order_snapshots`、
`remote_position_snapshots` 和各账户最新 `account_reconciliation` 状态；全局
`execution_control` 在对账开始时进入 `reconciling`。余额、权益、模式、挂单与仓位已接入持久化，
分页不完整、未匹配的远端订单/仓位、成交尚未关联或私有流尚未就绪时，每轮结束都会保持 `blocked`，
不能据此执行交易。
`private_stream_states` 按交易所与环境保存连接、认证、订单/成交/仓位订阅和最近心跳/事件时间；
只有全部三类订阅成功且心跳不超过 30 秒才视为就绪。worker 每次启动都先把旧连接状态重置为断开，
防止把上一个已退出进程的记录当作活连接；全局状态为 `ready` 时任一私有流断开会原子切换为
`paused`，后续必须完成 REST 对账才能恢复。表内只保存通用健康标志和时间，不保存凭据、订阅载荷或
交易所错误原文。
通用私有流监督器以独立任务管理每个账户连接：收到事件时记录事件心跳，空闲 10 秒时必须由连接适配器
完成真实 ping/pong 探测后才续写健康心跳；异常会先关闭连接、写入断开状态，再以 1–30 秒指数退避
重连。日志只记录交易所和环境，不记录异常正文、URL、签名、订阅载荷或凭据。
Binance 私有连接由两条通道组成：现货使用
`userDataStream.subscribe.signature`，USDT 永续使用 `/fapi/v1/listenKey` 后连接私有
WebSocket，并每 30 分钟续期。只有现货签名订阅返回成功且永续 listenKey 与连接都建立后，通用监督器
才可登记三类订阅就绪；任一通道关闭、ping/pong 失败或续期返回不同 listenKey 都使整个 Binance 账户
断开并进入 REST 对账。沙盒使用当前官方 Spot Testnet 与 USDⓈ-M Demo 地址。
常驻 worker 会为每个已配置的 Binance 沙盒或实盘账户创建该连接，并与 60 秒 REST 对账循环并行运行；
`worker --once` 不创建无法持续保活的 WebSocket。
OKX 使用单条生产或模拟盘私有 WebSocket，按官方
`timestamp + GET + /users/self/verify` 规则登录，并在返回成功后订阅 `orders(ANY)`、
`positions(ANY)` 和 `account`。普通订单频道包含成交更新；专用 `fills` 频道仅向特定 VIP 等级开放，
因此不能把它作为普通账户的就绪前提。三个通用频道全部确认后才登记订单、成交和仓位订阅就绪；空闲时
使用 OKX 要求的文本 `ping`/`pong`，频道连接数错误或任一通用错误均触发断线阻断。常驻 worker 已装配
OKX。
Bybit 使用生产或测试网 V5 私有 WebSocket，以 `GET/realtime + expires` 的 HMAC-SHA256 签名认证；
认证成功后一次订阅全品类 `order`、`execution`、`position` 和 `wallet`。订单、独立成交及仓位主题
分别满足三类健康条件，钱包主题提供账户余额变化；整个订阅请求明确成功后才登记就绪。空闲连接发送
Bybit 应用层 JSON `ping` 并等待读循环收到 `pong`，任一失败响应或连接错误都会触发断线阻断。常驻
worker 已装配 Bybit。
Bitget 私有流连接前复用交易适配器的只读账户代际探测：UTA 使用 V3 域名及 `UTA` 的 `order`、
`fill`、`position`、`account` 主题；Classic 使用 V2 域名并分别订阅现货/USDT 永续订单与成交、
USDT 永续仓位及两类账户频道。模拟盘使用对应 `wspap` V2/V3 域名。所有实际请求频道逐项确认后才
登记就绪，文本 `ping`/`pong` 用于空闲保活；代际不明、升级/切换中、登录或任一订阅失败均整条断开，
绝不在 V2/V3 之间失败回退。常驻 worker 已装配 Bitget。
Gate LIVE 使用现货与 USDT 永续两条私有连接。现货订阅 `spot.orders`、`spot.usertrades` 的全标的更新；
永续先通过签名 REST 账户接口读取并验证正整数用户 ID，再订阅 `futures.orders`、
`futures.usertrades`、`futures.positions` 的全合约更新，连接显式发送
`X-Gate-Size-Decimal: 1` 以保留十进制合约数量。两条连接都必须通过 WebSocket 协议 ping/pong；
任一通道断开即使整个 Gate 连接失败并由监督器重连。Gate 沙盒不满足同所现货和 USDT 永续要求，
因此明确拒绝且不得回退到实盘。常驻 worker 已装配 Gate。
MEXC LIVE 现货先用 API Key 创建 60 分钟 listenKey，再分别确认
`spot@private.orders.v3.api.pb`、`spot@private.deals.v3.api.pb` 和
`spot@private.account.v3.api.pb` 三个 Protobuf 频道；每 30 分钟续期，续期失败或返回不同 key 即断线，
正常关闭时主动释放 key。合约连接按 `apiKey + reqTime` 做 HMAC-SHA256 登录；官方登录成功后默认推送
订单、成交、仓位和资产等全部私有数据。现货 `PING`/`PONG` 与合约 `ping`/`pong` 都必须验证，
任一通道失败即整条连接重连。MEXC 没有受支持的合约沙盒，因此明确拒绝且不得回退到实盘。常驻 worker
已装配 MEXC。
所有已认证私有流收到事件后，监督器只提交一次进程内对账唤醒信号，不在读取任务中直接修改金融账本。
同一 worker 持有的执行器锁内按 250 毫秒窗口合并突发事件，再串行执行既有严格 REST 对账：按客户端
订单 ID 找回 ACK、刷新订单终态、分页获取成交、幂等写入成交并核对远端仓位。这样事件能快速驱动
账本更新，同时避免不同交易所推送格式、重复事件或推送与周期任务并发造成双写；60 秒周期仍负责恢复
漏事件和断线期间状态。
每轮对账对每个账户独立汇总阻断原因；只有全部已配置账户都没有原因且没有请求失败，才把账户状态写为
`ready`。写入全局 `ready` 前会再次检查每个账户的私有流心跳，避免把本轮处理中已经陈旧的连接放行。
任一账户为 `blocked`/`error` 或已有补偿失败等安全暂停时，全局状态不会进入 `ready`。
全局 `paused` 时 worker 仍执行只读账户与成交对账，并对每个远端活动订单调用对应交易所撤单接口；
返回值必须确认市场、标的及已有订单标识，没有确认或调用失败都记录为账户阻断原因。撤单受理不会直接
篡改本地订单终态，下一轮仍须按客户端订单 ID 查单并拉取完整成交。管理员恢复只把控制状态改为
`reconciling`，后续完整检查通过才允许 worker 写回 `ready`。真实下单的最终数据库提交事务也会锁定
并重新检查该控制行，关闭“预检开始后管理员刚好暂停”的竞态窗口。
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
`trade_previews` 保存真实开平仓预览的动作、管理员、交易所/环境、标的、名义金额、杠杆、最大滑点、
行情指纹、15 秒有效期及可空的确认 UUID；平仓预览还以外键绑定配对仓位。首次确认在行锁内原子预留
该 UUID；同一票据不能被另一管理员确认，也不能换幂等键、换动作或换仓位生成意图。未确认票据永远
不会被 worker 执行。
`strategy_versions` 以 UUID 和单调版本号保存完整 JSON 配置、环境、创建者和时间；版本写入后不可修改。
`automation_control` 是 ID=1 的独立单例，初始状态固定为 `disabled`，保存当前策略 UUID、
`disabled/enabled/paused`、原因和操作者。启用前 API 必须重新读取账户执行 `ready`、策略内容和目标
凭据；MEXC/Gate 不允许出现在 sandbox 策略。暂停不会覆盖账户级 `execution_control`，因此后续仍可
对账和人工平仓。每次创建版本、启用、暂停、恢复和禁用都写审计事件。
`latest_opportunities` 以 `exchange:base_asset` 为主键保存最新完整机会 JSON、交易所、标的、行情时间和
写入时间。API 行情进程每轮覆盖更新，不追加高频历史；`opportunity_snapshots` 继续承担分钟级历史。
唯一 worker 通过该表和 `instruments` 目录获得跨进程一致的机会与精度，仍以行情 `observed_at` 而不是
`updated_at` 判断 15 秒新鲜度。
`pnl_realizations` 为每个已完成的平仓意图保存一条不可重复的实现事件，包括共同平仓数量、毛盈亏、
本次分摊的开仓费、实际平仓/补偿费、净盈亏和实现时间。`closing_intent_id` 唯一约束使 worker 在
结算后崩溃并重试时不会重复计入；仓位上的 `realized_pnl_usdt` 继续提供全生命周期累计值，自动每日
止损则按实现事件、环境、目标交易所及 UTC 日界求和，部分平仓可以准确归属到实际发生日期。
`notification_outbox` 为每个事件和目标通道保存独立投递记录；`(dedupe_key, channel)` 唯一约束阻止
重启或重复业务事件产生重复通知。worker 只认领到期的 pending/retry 记录；PostgreSQL 使用
`FOR UPDATE SKIP LOCKED`，已认领但进程崩溃的 sending 记录在 5 分钟后可回收。失败按 30 秒起步、
最高 1 小时的指数退避重试，默认第 8 次失败进入 dead。数据库仅保存预定义的小写错误码，禁止存储
可能包含 Bot token、SMTP 凭据、请求 URL 或远端响应正文的异常字符串。邮件和 Telegram 互不阻塞，
通知失败也不得回滚或阻塞交易状态机。
真实意图额外固化 1–10 倍请求杠杆，旧记录迁移为安全默认值 1；
`order_legs` 同一意图固定一条现货腿和一条永续腿，并在提交交易所前生成唯一客户端订单 ID；每条腿还
保存严格为正的 `base_multiplier`，使交易所原生数量及成交量可以无歧义换算成基础币。已有纸面订单腿
迁移时乘数为 1；后续真实永续腿必须使用标的目录的合约乘数。
非终态意图可由 worker 按创建时间恢复。纸面 worker 在单一数据库事务中更新双腿、写入 `fills` 并创建
`paired_positions`；唯一交易 ID 和开仓意图约束保证重复运行不会重复成交。真实成交仍必须以交易所
私有流或 REST 查询为准。
真实执行器只读取 `planned` 的 `sandbox`/`live` 开平仓意图，并要求全局执行状态已经是 `ready`。
开仓发单前重新读取余额、权限、持仓模式以及完整远端挂单/仓位，当前安全阶段要求账户没有任何挂单或
仓位，再确认目标永续逐仓杠杆。平仓不要求远端仓位为空，而是要求没有远端挂单，并把所有本地
open/closing 配对空仓按标的、原生数量、杠杆和逐仓模式与远端完整快照精确匹配；目标双腿还必须恰好
覆盖仓位剩余数量，现货卖出且永续 reduce-only 买回。两条主订单腿必须在同一数据库事务中由
`created` 变为 `submitted`，意图进入
`executing` 后才允许并行调用交易所。两条 ACK 分别核对市场、标的和客户端 ID 后持久化；网络异常、
响应不匹配或 ACK 落库失败均把对应腿置为 `unknown` 并原子暂停全局执行。进程在事务提交后的任意位置
崩溃，都只能通过客户端订单 ID 查单恢复，执行器不会再次提交 `executing` 意图。
常驻 worker 在持有唯一执行器锁的每轮开始先运行纸面执行器，再运行真实执行器，然后进入完整 REST
对账。真实执行器只会消费已经持久化为 `planned` 的确认意图；未处于全局 `ready` 时返回且不改订单。
发单发生后同一轮对账立即尝试找回 ACK、成交和仓位，私有事件仍可继续唤醒后续对账。
本轮所有配置账户再次通过 REST、私有流及本地账本核对并进入 `ready` 后，worker 才读取当前生效的
不可变策略、最新机会、交易规则、全部仓位、当前远端清算价快照和 UTC 当日实现 PnL，最多规划一个
自动动作。新建意图会设置进程内对账事件，使下一轮立即由真实执行器重新预检，而不等待普通 60 秒周期。
策略 UUID、动作、仓位/标的及行情时间共同生成确定性 UUID 幂等键；同一行情重放或 worker 重启不会
重复规划，已有未完成开仓意图也阻止同标的再次规划。
每轮最多提交一个真实意图；提交后必须先完成远端对账，不能在上一组 IOC 状态尚未确认时继续发下一组。
真实执行器发起任何私有请求前再次检查意图的 `market_observed_at`；超过 15 秒或来自未来超过 5 秒的
`planned` 意图直接标为 `failed`。过期平仓在同一事务中把仍由该意图预留的仓位恢复为 `open` 并清空
`closing_intent_id`，因此崩溃恢复既不会按旧价格下单，也不会永久锁死仓位。
`trade_previews` 和 `trade_intents` 以受检查约束的 `emergency` 布尔值区分紧急配对平仓，开仓不能带
该标记。普通预览最大滑点仍由 API/规划器限制为 0.1，只有紧急平仓可使用 0.25。全局暂停时真实执行器
不消费普通开仓或平仓，只消费显式紧急平仓；最终提交事务也仅对此组合接受 `paused`，其他意图仍要求
`ready`。紧急路径复用相同的仓位行锁、双腿规则、客户端订单 ID、reduce-only、成交结算和幂等恢复。
REST 成交对账完整且两条主腿都进入 `filled`、`canceled` 或 `failed` 终态后，worker 才尝试结算真实开仓。
两腿原生成交量分别乘以 `base_multiplier`；基础币数量相等且非零时，使用真实加权均价创建
`paired_positions`。USDT 费用直接计价，基础币费用按该笔成交价折算；其他折扣币费用当前无法可靠估值，
因此进入 `manual_review` 并全局暂停。两腿均为零成交时意图安全失败且不创建仓位。
新产生的暂停会在本轮对账结束时重新读取并保留，不能被通用 `blocked` 状态覆盖。
两条真实主腿终态后数量失衡时不再立即伪造结果或永久停在人工状态：结算事务以多余腿相反方向创建唯一
`spot_compensation` 或 `perp_compensation`，原生数量按该腿 `base_multiplier` 精确换算，意图进入
`compensating` 并全局暂停。worker 只在最新机会不超过 15 秒、当前规则完整、补偿名义额不超过对应
最优档容量时，按生效策略 `emergency_max_slippage`（没有匹配策略时为 1%）更新保护价并原子把补偿腿
置为 `submitted`；随后才调用交易所。要求和提交分别写入脱敏审计事件。
补偿腿与普通腿使用相同的唯一客户端订单 ID、ACK 不确定状态、按 ID 查单、成交分页及幂等填充规则。
完整补偿量必须等于原主腿基础币差额；不足、零成交、费用不可折算或价格缺失都进入 `manual_review`，
且不会清除安全暂停。完整补偿后，开仓只按共同数量创建仓位，并把补偿往返损失及所有三腿费用计入
开仓成本；平仓只按共同数量递减仓位，把补偿往返损益及所有费用写入唯一 PnL 实现事件。补偿成功仍保持
暂停，必须由管理员请求恢复并通过新一轮完整账户对账。
真实平仓同样只在两腿终态且成交分页完整后结算。两腿按乘数折算后的基础币成交量相等时，以真实加权
均价更新仓位数量、累计平仓费、剩余开仓费和已实现净盈亏；全量成交关闭仓位，等量部分成交恢复 open
以允许继续关闭剩余量，双零成交恢复 open 并把意图标为失败。失衡、超量或费用无法折算保持 closing
并进入 `manual_review` 和全局暂停，当前不会伪造已对冲结果。
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
