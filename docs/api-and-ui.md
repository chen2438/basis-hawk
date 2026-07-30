# API 与界面

生产服务在容器网络监听 `0.0.0.0:8000`，只通过 Caddy 域名 HTTPS 暴露。健康检查与静态登录页公开；
其余 HTTP 和 WebSocket API 均要求管理员会话。当前市场接口为 `/api/opportunities`、单机会
`/history`、`/top-book`、`/api/exchanges/status`、`/api/settings` 和 `/api/ws/opportunities`。
所有比例使用小数值字符串，例如 `0.001` 表示 `0.1%`。
`GET /api/opportunities`、`/api/exchanges/status` 及单机会 `/history`、`/top-book` 接受可选
`environment=live|sandbox`，省略时保持 `live`。`sandbox` 聚合 Binance Demo、OKX Demo、
Bybit Testnet、Bitget Demo 与 Gate TestNet；机会与状态使用同一份最多缓存 5 秒的只读快照，每所
独立降级，MEXC 状态明确返回不支持。历史入口返回空列表且前端明确说明未持久化，报价只访问所选
测试环境，不得回退正式网。
`/api/ws/opportunities` 继续只推送 LIVE 正式扫描器数据。
生产 SPA 回退只返回解析后仍位于 `frontend/dist` 内的普通文件；URL 编码父目录、绝对路径和解析到
目录外的符号链接都回退到入口页，不能读取镜像中的源码、依赖清单或其他配置文件。
机会对象同时返回开仓方向 `spot_ask`/`perp_bid` 与平仓方向 `spot_bid`/`perp_ask`，
以及开仓方向现货卖一、永续买一各自的名义容量和两腿较小的可执行容量。右侧详情选中机会后调用
`GET /api/opportunities/{exchange}/{base_asset}/top-book` 刷新该标的一档；Gate 因批量 ticker
缺少现货数量时也能显示真实两腿容量。请求期间明确显示“读取中”，失败不会沿用未知容量冒充可执行。
`/api/opportunities` 的 `page_size` 上限为 3,000，匹配六所各 500 个候选；行情首页初始读取完整
3,000 项，再由浏览器本地搜索和筛选，不能只取全局前 300 项而漏掉某所已扫描标的。

管理员使用密码与 TOTP 登录。服务通过 Secure、HttpOnly、SameSite=Strict Cookie 保存会话；
所有修改请求还必须提供与 Cookie 会话绑定的 `X-CSRF-Token`。连续失败登录受限流保护。
默认登录有效期为 7 天，服务端数据库会话和会话/CSRF Cookie 同时到期；已有会话保留其创建时写入的
到期时间，重新登录后才获得新的 7 天有效期。前端不会把会话令牌写入 localStorage。
账户设置页提供密码修改和 TOTP 轮换；两者都要求再次验证当前密码与当前 TOTP，成功后立即撤销全部
浏览器会话。密码至少 12 个字符且不能与原密码相同。TOTP 轮换还要求显式勾选确认，新 provisioning
URI 只在成功响应中返回一次，页面不会把它写入浏览器持久存储。

认证接口为：

- `POST /api/auth/password`：提交当前密码、当前 TOTP 与新密码，成功返回 204 并清除当前 Cookie；
- `POST /api/auth/totp/rotate`：提交当前密码、当前 TOTP 和 `confirmed=true`，成功返回一次性
  `provisioning_uri` 并清除当前 Cookie。

登录后的应用使用固定深色左侧菜单和窄状态栏组织工作区；市场总览与执行状态、交易所账户、手动交易、
配对持仓、交易账本、内部划转、自动策略、审计与通知均为一级页面，窄屏时菜单收拢为图标栏。页面
使用 11px 作为显式最小字号，覆盖辅助说明、字段标签、表头、状态胶囊和侧栏元信息；正文与标题维持
原有层级，避免高密度策略表单以 7–10px 显示。页面直接
读取下列 API 真相源，而不是复制内存状态：
执行页展示全局与逐账户对账、私有流、挂单/仓位/成交计数，并提供普通确认后的暂停和重新对账；
`blocked`、`paused` 或 `ready` 都可请求全量重新对账，只有已经处于 `reconciling` 时暂时禁用。
进入 `reconciling` 后，执行页每 3 秒只刷新一次 `GET /api/system/execution`，使全局状态和逐账户结果
自动更新；返回 `ready`、`blocked` 或 `paused` 后立即清除定时器，不持续产生后台轮询；账户页
管理加密凭据且只展示掩码，可按需读取余额和权限；持仓页列出配对仓位；交易账本页列出最近 100 条
交易意图、订单腿、成交和已实现 PnL；三张交易明细表统一展示同一意图的前 8 位短编号，完整 UUID
保留在单元格提示中；订单腿同时展示创建时间和状态更新时间，避免把后续对账确认误解为新下单。
失败或预检阻断的意图额外显示由服务端预定义代码翻译的中文原因，可由旧账本
可靠判定的“两腿均未提交”失败会迁移为行情过期，其他历史空值明确标注为升级前未记录；划转页只接受同所 USDT
现货↔永续并明确提示会暂停执行，同时随交易所与环境选择读取账户快照，展示现货可用、永续可用、
永续权益、独立/共享余额结构、账户模式和读取时间，并标记本次划出的来源账户。选择变化时旧快照
立即清空，迟到响应不会覆盖新选择；读取失败只显示在余额卡片中，仍可查看已有划转记录。自动策略页
展示不可变版本配置并提供启用、暂停、恢复和禁用。实盘
凭据输入使用密码控件，保存后立即清空，页面和浏览器状态不保存明文。手动交易页为真实开仓、普通
平仓和紧急平仓生成 60 秒票据，展示双腿保护价/数量、资金需求、费用、基差与预计盈亏，并在实盘确认
前醒目标记；确认只持久化 UUID 幂等意图，浏览器不直接向交易所发单。自动策略编辑器覆盖后端全部
必填风控参数，保存仅创建不可变新版本；启用最新版本和恢复原生效版本是两个独立动作，且都要求全局
执行 ready。启用/恢复请求若因内部划转、对账或其他并发状态变化被拒绝，界面会保留中文错误并立即
重新读取全局执行状态，避免继续显示过期的“执行就绪”；非 ready 时同时展示中文状态与当前阻断原因。
审计与通知页分别展示最新 100 条管理员动作和投递状态；审计详情由服务端递归脱敏，
通知不返回正文或去重键。执行页列出全部加密备份，最新项不可选择或删除；旧备份支持单个删除、全选
旧项和批量删除已选，批量操作只提交明确选中的文件名。审计与通知页可设置天数清理终态通知日志，并
明确标识管理员审计不可删除。执行页的软件更新卡片展示当前/远端提交和代理状态，可检查
锁定分支或在发现新版本后显式确认更新；全局执行不是 `ready` 时按钮禁用并显示中文原因，只有同一次
更新失败留下的专属软件更新暂停允许重试。更新期间页面会轮询脱敏状态，服务短暂离线属于预期行为。
市场总览右侧机会详情把开仓报价正确标为“现货卖一”和“永续买一”，并在下一行分别展示两腿 USDT
名义容量，再展示取小后的双腿可执行容量，便于在进入手动交易前识别限制腿。
市场页顶栏提供 LIVE/SANDBOX 分段切换。切换会关闭旧详情、重置交易所筛选并从目标环境重新读取，
SANDBOX 显示六所状态卡，其中五所读取官方 Demo/Testnet，MEXC 明确标为不支持，并醒目标注当前费率
回退估算；运营页始终保留 LIVE 机会作为手动交易候选，不能因市场展示环境变化而混入测试网数据。
机会表每个标的和详情面板均提供新窗口永续页面链接，链接由前端按受支持交易所、原生永续 symbol
和当前环境生成，不接受 API 返回的任意 URL；Binance、Bybit 与 Gate Sandbox 分别指向
`demo.binance.com`、`testnet.bybit.com` 与 `testnet.gate.com`，OKX 与 Bitget 使用交易所网页内的
Demo 模式入口。

交易所凭据接口为：

- `GET /api/accounts/credentials`：仅返回交易所、环境、标签、更新时间和 API Key 掩码；
- `PUT /api/accounts/{exchange}/{sandbox|live}/credentials`：保存或替换 API Key、Secret，
  OKX/Bitget 还必须提供 passphrase；成功写入前先请求全量安全对账，worker 会在运行中自动替换对应
  私有流，不需要重启；
- `DELETE /api/accounts/{exchange}/{sandbox|live}/credentials`：删除本地加密凭据，并自动停止对应
  私有流后重新对账。
- `GET /api/accounts/{exchange}/{sandbox|live}/snapshot`：按需解密凭据并从交易所读取 USDT
  现货可用余额、永续可用余额/权益、账户类型、持仓模式和永续保证金模式；响应仍不包含任何凭据。
  Gate 快照还返回可空的 `spot_buy_fee_in_base`：只有官方账户费率明确显示 GT 折扣和点卡抵扣均未
  启用时为 `true`，任一替代抵扣启用时为 `false`，接口不可确认时为 `null`。账户页把 Gate
  组合保证金明确显示为“组合保证金（跨仓）”。
- `PUT /api/accounts/bybit/{sandbox|live}/position-mode`：要求 `confirmed=true`，只更新既有加密
  凭据中的单向/双向模式声明，不需要重新输入 Key，也不会修改交易所设置；用于 Bybit 空子账户不返回
  `positionIdx` 时完成只读能力声明；更新后同样热重载 Bybit 私有流并重新对账。

通用多账户接口位于 `/api/v2/accounts`：

- `GET /api/v2/accounts` 返回稳定账户 ID、交易/扫描默认标记、能力声明和账户级 Maker/Taker 费率；
- `POST /api/v2/accounts` 新增具名账户，`PUT /api/v2/accounts/{account_id}` 替换凭据；同一交易所
  与环境内标签唯一，首个账户强制成为交易和扫描默认，避免产生无默认组；
- `POST /api/v2/accounts/{account_id}/defaults` 分别切换交易默认和扫描默认；服务先清除旧默认并
  flush，再设置新默认，满足部分唯一索引且不依赖 ORM 更新顺序；
- `PUT /api/v2/accounts/{account_id}/fees` 写入账户实际费率或人工覆盖值及来源；
- `GET /api/v2/accounts/{account_id}/snapshot` 按账户 ID 读取余额快照；
  `DELETE /api/v2/accounts/{account_id}` 拒绝删除已被任务或策略引用的账户，删除默认账户时提升同组
  最早的剩余账户。

旧 `/api/accounts/...` 接口只操作交易默认账户，现有私有流、对账和双腿执行器也只消费该默认账户，
直到通用账户级 worker 接管。所有列表、审计和 API 响应都只返回掩码与脱敏元数据，不返回明文密钥。

多腿任务控制面位于 `/api/v2/execution-tasks`：

- `POST /api/v2/execution-tasks` 要求 UUID `Idempotency-Key`，原子创建任务及 2–16 条有序腿；同一键
  同一请求返回原任务，不同请求返回 409。每条腿必须显式选择六所之一；非纸面腿的账户必须存在，
  并同时与腿交易所和任务环境匹配；
- `GET /api/v2/execution-tasks` 和 `GET /api/v2/execution-tasks/{task_id}` 返回任务、乐观锁版本、
  有序腿和脱敏预检摘要；
- `GET /api/v2/execution-tasks/{task_id}/activity` 返回该任务的全部 run、订单尝试、Maker
  追价父子关系和逐笔成交；每条订单同时返回实际 `side`、`reduce_only` 与
  `primary/compensation` 用途，用于区分原始腿和失败后的反向保护，并支持重启恢复审计；
- `POST /api/v2/execution-tasks/{task_id}/preflight` 逐个账户读取余额快照与完整订单/仓位状态，永续
  账户还必须能确定持仓模式，并逐腿验证 Maker 真实第 N 档深度。结果有效 60 秒，仅保存档位价格、
  方向和观测时间等脱敏摘要，响应和审计均不包含 Key、Secret 或交易所原始错误；
- `POST /api/v2/execution-tasks/{task_id}/start` 要求 `confirmed=true` 和最新 `expected_version`。
  真实/沙盒任务还要求全局执行状态为 ready；校验和 `preflight_ready → queued` 在同一锁定事务完成；
- `POST /api/v2/execution-tasks/{task_id}/cancel` 只允许取消 draft、preflight_ready 或 queued，
  不允许把已运行任务直接标记结束，以免跳过敞口补偿。

组合读接口位于 `/api/v2/strategies`：

- `GET /api/v2/strategies` 返回按开仓时间倒序的组合，可用逗号分隔的 `status` 过滤
  `running/closing/ended/manual_review`；
- `GET /api/v2/strategies/{strategy_id}` 返回组合及 2–16 条有序策略腿，包含交易所、角色、账户、
  方向、入场/出场价、剩余基础币数量、手续费、资金费和净 PnL；响应不包含凭据或远端错误正文。

通知控制面继续复用持久化 outbox，并增加只读能力摘要：

- `GET /api/operations/notifications` 可按通道和投递状态筛选，返回严重级别、事件、主题、尝试次数、
  脱敏错误码与时间，不返回正文或去重键；
- `GET /api/v2/notifications/settings` 只返回邮件/Telegram 是否配置、SMTP 安全模式、端口以及认证、
  发件人与收件人字段是否完整，不返回 Host、地址、用户名、密码、Token 或 Chat ID；
- `POST /api/operations/notifications/test` 仍要求显式确认，并只把测试事件写入 outbox，由 worker
  异步投递。预警页读取 warning/critical 记录，邮件页限定 email 通道并展示待投递、重试、送达和
  dead-letter 状态。

三类机会统一由 `GET /api/v2/opportunities` 返回：

- `type=funding` 生成“现货买入 + 永续卖出”候选，两腿可以来自不同交易所；
- `type=cross_funding` 生成“高 funding 永续卖出 + 低 funding 永续买入”的跨所候选；
- `type=basis` 使用与 funding 相同的可执行腿，但按入场基差、`holding_days` 内预计 funding 和
  往返手续费后的预计收益排序。

接口支持 `exchanges`、`search`、`minimum_annualized_return`、`holding_days`、
`maximum_age_seconds` 和 `limit` 过滤。每条腿返回默认实盘交易 `account_id`、所用 Taker 费率和
`actual/manual/default` 来源；账户费率只要某一市场缺失，就只对该市场回退行情内的默认费率。
候选只组合 `healthy` 且未超过最大年龄的行情，响应同时提供稳定候选 ID、可执行名义额及共同观测时间。

ADL 接口位于 `/api/v2/adl`：

- `GET /api/v2/adl` 返回每个账户、symbol 和方向的最新持久化快照；
- `POST /api/v2/adl/refresh` 解密每个具名账户并调用对应交易所只读 ADL 接口，保存后返回最新快照。

等级统一为 1–5 且 5 为最高风险；响应同时保留脱敏原生值用于核对。MEXC 没有可轮询的实时等级，
返回 `event_only=true` 和空等级，前端显示“私有事件监听”而不是虚构风险数字。

- `GET /api/system/execution`：读取 worker 的全局执行阻断状态，以及各账户最近一次启动对账状态、
  私有流就绪状态、远端结果完整性、挂单数和仓位数。真实订单预检失败时 `reason` 使用
  `live_order_preflight:{exchange}:{safe_code}`，前端翻译为带交易所和失败阶段的中文说明；该值不含
  交易所原始异常、请求 URL、签名或凭据。
- `GET /api/exchanges/status`：除目录数和行情刷新延迟外，按交易所返回
  `history_ready`、`history_progress_percent`、`history_download_rate_per_minute` 和
  `history_syncing`。预热比例按已覆盖至少 6 天历史的候选数计算；下载速度是本轮成功下载的历史标的数
  每分钟，不是网络字节吞吐。市场总览每 5 秒刷新并用百分比、完成数量和进度条展示，同时按
  `history_syncing` 区分正在检查/下载、上轮速度、预热完成和等待下轮。
- `POST /api/trades/paper/open`：使用当前健康机会持久化纸面开仓意图和现货买入/永续卖出双腿；
  必须提供 UUID `Idempotency-Key`，随后由唯一 worker 原子模拟双腿 taker 成交。
- `GET /api/trades/intents/{uuid}`：读取交易意图、版本、双腿状态及可空的脱敏 `failure_code`。
- `GET /api/trades/intents/{uuid}/fills`：读取该意图的成交和手续费。
- `GET /api/trades/intents`：按创建时间倒序读取交易意图、双腿及可空的脱敏 `failure_code`，允许精确
  `status` 过滤。
- `GET /api/trades/orders`：按实际状态更新时间倒序读取现货、永续及补偿订单腿，同时返回
  `created_at` 及可空的脱敏 `failure_code`，供界面区分创建、状态变化和交易所明确拒绝原因；允许
  精确 `status` 过滤。
- `GET /api/trades/fills`：按成交时间倒序读取本地已核对成交，并附带交易所、环境、标的、动作和订单腿。
- `GET /api/trades/pnl`：按结算时间倒序读取已实现 PnL，并分别返回毛盈亏、分摊开仓费、平仓费和净盈亏。
- `GET /api/trades/funding-income`：按实际结算时间倒序读取交易所私有账户资金费收支，可按交易所和
  环境过滤；正数表示收到、负数表示支付。
  五个全局账本入口默认返回 100 条、`limit` 范围为 1–500，所有金融值均为十进制字符串。
- `GET /api/trades/positions?status=open&include_valuation=true`：读取配对仓位；显式请求估值时，开放仓位
  同时返回可空的
  `spot_exit_price`、`perp_exit_price`、`unrealized_pnl_usdt`、
  `estimated_closing_fees_usdt`、`estimated_final_pnl_usdt` 和
  `valuation_observed_at`。`funding_income_usdt` 只累计同交易所、环境和标的自该仓位开仓以来已经
  写入私有资金费账本的记录；预计平仓费使用不可变开仓意图保存的现货/永续费率。
  每条仓位始终返回 `notional_usdt` 和 `leverage`：前者按剩余共同基础币 `quantity ×
  spot_entry_price` 计算，表示按现货实际开仓均价折算的剩余名义额；后者来自对应不可变开仓意图，
  表示真实下单前已确认的永续杠杆。原始 `quantity` 继续以基础币计价供精确平仓和审计使用，但前端
  配对持仓表只显示 USDT 名义额和合约杠杆。
  `live`/`paper` 使用扫描器最新行情，Gate `sandbox` 直接读取 TestNet 行情；超过 15 秒或读取失败时
  估值字段为 `null`，持久化仓位仍正常返回。
- `POST /api/trades/paper/positions/{uuid}/close`：使用现货 bid 卖出及永续 ask reduce-only
  买回计划纸面平仓，同样要求 UUID `Idempotency-Key`。
- `POST /api/trades/open/preview`：为已配置凭据的 `sandbox`/`live` 账户生成 60 秒真实开仓预览票据，
  返回双腿参考价/保护价、原生数量、合约乘数、预计费用、现货余额需求、永续保证金需求及最坏基差。
  名义金额小于当前共同数量网格和两腿交易所最低规则所要求的动态金额时返回
  `notional_below_minimum` 及 `minimum_notional_usdt`；前端把它显示为带精确建议值的中文提示。
  名义金额超过当前双腿一档取小容量时返回 `notional_exceeds_top_book` 及
  `capacity_notional_usdt`；前端显示当前最大金额并提示盘口会实时变化。常见的凭据、行情、机会状态和
  交易规则预览错误也由前端映射为中文。
  确认时重新取得不超过 15 秒的盘口并复核容量、交易规则和配置。正常报价变化只要仍能由预览固化的
  双腿绝对保护价成交即可确认，实际订单继续使用该保护价；超出原最大滑点边界、规则/配置变化或
  60 秒票据过期则拒绝。前端显示对应中文说明并立即移除失效票据；采集时间不参与指纹。
  Gate 因批量 ticker 不返回现货最优档数量，会在预览和确认时只为当前所选标的即时读取现货与
  USDT 永续一档订单簿；两腿较小容量不足、订单簿为空或读取失败时不会创建或确认票据。
  Gate 最近账户快照明确确认买入手续费扣基础币时，预览中的现货原生数量仍受请求名义额约束，永续
  原生数量则按“预计现货净到账量向下取整到合约网格”生成；`base_quantity` 表示计划完整对冲的净
  基础币数量。无法确认扣费方式时保持原等量规划，不作猜测。
- `POST /api/trades/open/confirm`：请求体必须对预览票据显式发送 `confirmed=true`，同时提供 UUID
  `Idempotency-Key`；仅在全局执行状态为 `ready` 且票据仍匹配当前行情时持久化真实 `planned` 意图。
- `POST /api/trades/positions/{uuid}/close/preview`：为既有 `sandbox`/`live` 配对仓位生成 60 秒
  平仓预览，展示现货卖出与永续 reduce-only 买回的参考价、保护价、原生数量、合约乘数、预计费用、
  毛盈亏及扣除剩余开仓费后的预计净盈亏。
- `POST /api/trades/positions/{uuid}/close/confirm`：要求显式 `confirmed=true`、UUID
  `Idempotency-Key`、全局 `ready` 及未变化的仓位/行情指纹；只持久化 `planned` 平仓意图。
- 同一平仓预览请求可显式设置 `emergency=true` 和最高 `0.25` 的紧急保护滑点。紧急预览仍要求价格
  不超过 15 秒、双腿盘口可完整覆盖仓位和规则完整，但可在资金费历史 `warming/stale` 时使用；确认
  会把全局执行保持为 `paused`，随后仅该带持久化紧急标记的配对平仓可在暂停状态执行。普通平仓仍限
  `0.1` 滑点、healthy 机会及全局 `ready`。
- `GET /api/automation`：读取独立于账户执行状态的自动交易状态、当前生效策略和最新草稿版本。
- `PUT /api/automation/config`：保存新的不可变完整策略版本；不会自动启用或修改旧版本。
- `POST /api/automation/enable`：要求策略 UUID、`confirmed=true`、全局执行 `ready`，并确认所有目标
  交易所在策略环境中已配置凭据。
- `POST /api/automation/pause`、`/resume`、`/disable`：暂停、重新验证后恢复或禁用自动交易；
  不把暂停等同于紧急清仓。
- `POST /api/system/execution/pause`：要求 `confirmed=true`，立即持久化全局安全暂停；worker 随后撤销
  所有账户的远端活动订单，已对冲仓位保持不动。
- `POST /api/system/execution/resume`：要求 `confirmed=true`，只把状态改为 `reconciling`；不能由
  HTTP 请求直接宣称 `ready`，必须等待 worker 完成全量安全对账。
- `GET /api/operations/audit`：按 `limit`/`offset` 读取不可修改的管理员审计，可用完整
  `event_type` 过滤；响应中的敏感键递归替换为 `[redacted]`，长字符串受限。
- `GET /api/operations/notifications`：按 `limit`/`offset` 读取投递历史，可按 `status` 和
  `channel` 过滤；只返回事件类型、主题、级别、通道、尝试次数、时间和脱敏错误码，不返回消息正文
  或内部去重键。
- `POST /api/operations/notifications/test`：要求 `confirmed=true` 并明确选择 Telegram、邮件或两者；
  只对已完整配置的通道创建独立 `notification.test` outbox 项，随后仍由 worker 按普通重试规则投递。
  未配置通道返回冲突，不直接从 API 进程发送，也不在响应中返回正文。
- `GET /api/operations/backup`：返回全部归档及最新归档的文件名、大小、修改时间和校验文件状态；
  不读取备份密钥、不解密归档，也不把恢复能力暴露为 HTTP 接口。
- `DELETE /api/operations/backups/{archive_name}`：要求 `confirmed=true`，只接受严格的 Basis Hawk
  归档文件名并拒绝删除最新备份；成功时同时删除旁路校验文件并写入请求与完成审计。
- `POST /api/operations/backups/batch-delete`：要求 `confirmed=true` 和 1–100 个归档文件名，拒绝
  重复项、非法名称、缺失文件及最新备份；删除前先验证整批目标，成功时同步删除每份校验文件并以
  批次计数和文件名写入请求与完成审计。
- `POST /api/operations/logs/prune`：要求 `confirmed=true` 和 1–3650 天保留期，只删除截止时间前
  已经 `sent/dead` 的通知投递日志；`pending/sending/retry` 和管理员审计永不由该接口删除。
- `GET /api/operations/update`：读取宿主机代理写入的当前提交、远端提交、检查/完成时间、状态和
  预定义错误码；不返回 Git 输出、部署日志或宿主机路径。VPS 显式启用自动更新后，同一状态也会反映
  定时代理最终排队并执行的版本；CI 未成功时定时代理不会生成更新请求。
- `POST /api/operations/update/check`：创建一次固定的检查请求，由宿主机代理对部署时锁定的 HTTPS
  origin/branch 执行 fetch 和快进关系校验；API 容器本身不接触 checkout。
- `POST /api/operations/update/apply`：要求 `confirmed=true`，且目标必须等于最近一次检查或失败状态
  中记录的远端提交。入口使用数据库行锁，只能从 `ready` 原子切换为专属更新暂停；同一目标失败后可
  从该专属暂停重试，其他人工、对账或故障暂停返回 409 且保持原样。成功取得更新暂停后再排队；排队
  失败也保持安全暂停。宿主机代理会再次 fetch 并要求远端头未变化，只允许快进后调用既有部署流程；
  部署失败时可对同一远端提交重试部署，但不能换成未检查的目标。部署迁移完成后，更新代理只把原因精确为
  `software update requested` 的暂停切换为 `reconciling`，新 worker 自动执行全量安全对账；其他
  人工或故障暂停保持不变，且只有对账完整通过后才会回到 `ready`。
- `GET /api/transfers`：返回最近内部划转及提交前/预期余额、远端 ID、状态和脱敏错误码；金额均为
  十进制字符串。
- `GET /api/transfers/limits`：返回当前数据库中生效的全局单次限额、UTC 日累计限额、启用状态及
  最近更新人/时间。首次访问没有持久化值时，才使用两个 `BASIS_HAWK_TRANSFER_*` 环境变量初始化。
- `PUT /api/transfers/limits`：要求 `confirmed=true`；两项必须同时为 0（禁用）或同时大于 0，且
  单次限额不得超过日累计限额。更新与脱敏审计在同一事务提交，并立即影响所有交易所和环境的新划转。
- `POST /api/transfers`：仅允许 USDT 现货↔USDT 永续，要求 `confirmed=true` 和 UUID
  `Idempotency-Key`。新请求要求全局 `ready`、凭据存在、账户不是共享余额模式且数据库额度非零；
  成功后只创建 `planned` 账本并立即暂停交易，由唯一 worker 提交。相同键与相同请求在暂停后仍可
  安全重试，换参数复用同一键会冲突。计划事务锁定限额设置行后再检查单次和 UTC 日累计用量，因此
  管理员并发修改限额不会绕过边界。请求模型不存在地址、链、UID 或跨所目标。Gate 的
  `POST /wallet/transfers` 成功响应会返回 `tx_id` 并表示本次交易账户划转已完成，worker 随即在同一
  轮刷新账户余额确认到账；不会调用仅供主子账户划转使用的 `GET /wallet/order_status`。
- `POST /api/integrations/telegram/webhook`：唯一免管理员 Cookie/CSRF 的集成入口；必须携带与环境
  配置恒定时间匹配的 `X-Telegram-Bot-Api-Secret-Token`，消息 chat ID 也必须匹配管理员白名单。
  只接受最多 32 KiB 的 Telegram Update，并仅提供 `/status`、`/positions`、`/alerts`、`/health`
  四个只读命令；update ID 通过 outbox 去重，任何交易或配置命令均不存在。

写入接口只接受已认证且 CSRF 校验通过的请求。明文只在单次请求内进入内存，随后使用绑定交易所与环境的
AES-GCM 关联数据加密；响应、审计事件和日志均不得包含 API Secret、passphrase 或完整 API Key。
`paper` 环境不接受交易所凭据。

账户快照使用各所官方只读接口和签名规则。签名错误、超时及 HTTP 错误统一映射为不带请求 URL、
签名参数或响应原文的脱敏错误。Binance `sandbox` 使用一组 Demo Trading Key，同时访问
`https://demo-api.binance.com` 的现货账户和 `https://demo-fapi.binance.com` 的 USDⓈ-M 永续账户；
不会把该 Key 发往独立且凭据不互通的 Spot Testnet。Gate `sandbox` 使用独立 TestNet API Key 和
`https://api-testnet.gateapi.io`，可与 Gate `live` 凭据同时保存及对账，任一环境都不会回退到另一
环境；MEXC 没有满足同所现货+USDT 永续完整验收要求的沙盒，其 `sandbox` 快照仍明确返回不支持。
Bybit V5 使用 `settleCoin=USDT` 时不会返回
空仓标的，因此聚合结果无法识别模式时会再以 `symbol=BTCUSDT` 只读查询有效 `positionIdx`；若该空
子账户仍返回空列表，快照只使用管理员明确保存的模式声明，未声明则继续返回 `unknown` 并阻断。
声明不修改 Bybit 设置，目标标的在配置杠杆前还会重新读取自身模式，不能仅凭声明直接发单。
UTA 2.0 逐仓模式不使用账户级 `totalAvailableBalance`，永续可用 USDT 按币种 `walletBalance` 扣除
`totalPositionIM`、`totalOrderIM`、`locked` 和 `bonus` 计算并限制为非负数。
Binance 快照从现货账户的 `canTrade` 和独立的永续 `/fapi/v1/accountConfig` 配置共同确认双腿权限；
永续 V3 账户响应只用于余额/权益，不从其已移除的配置字段猜测权限。OKX 快照从账户配置的 `perm`
确认 `trade`；Bybit 从当前 API Key 信息同时确认非只读、SpotTrade
和 ContractTrade Order 权限。缺少权限返回 `false`，接口未提供字段则保持 `unknown`。
Bitget UTA 从 `/api/v3/account/info` 要求 `permType=read-and-write`，并同时具有 `uta_trade` 与
`uta_mgt`；Classic 从 `/api/v2/spot/account/info` 同时要求现货交易、合约订单和合约持仓写权限。
Gate 从 `/api/v4/account/main_keys` 对当前 Key 做唯一的完整或脱敏前缀匹配，要求 Key 正常、没有
交易对白名单且 spot/futures 均非只读；Gate 组合保证金还要求 unified 非只读。组合保证金必须同时由
期货账户 `margin_mode=2`、`/api/v4/unified/unified_mode` 的 `portfolio` 与其
`settings.usdt_futures=true` 确认。缺少 USDT 永续开关时仍返回可审计余额，但交易权限明确为 false，
对账页显示中文修复提示，并在任何现货腿下单前阻断。余额来自
`/api/v4/unified/accounts` 的 USDT available、总可用保证金和统一账户总权益；多币种或单币种统一
保证金模式暂不受支持。无法读取主 Key 清单、匹配不唯一或存在交易对白名单时保持
`unknown`，不会把失败的权限探测扩大成余额接口失败。MEXC 只有在现货账户明确 `canTrade=true`，且
官方标注需要 Trading 权限的合约持仓模式查询成功时确认双腿权限。
当前 worker 会核对余额、权益、账户模式、挂单、成交、仓位、实际资金费账单和私有流健康；任一交易
安全检查不完整时全局执行状态保持 `blocked`，该状态不能由 API 绕过。实际资金费账单完整性单独报告，
不参与订单和仓位的安全放行。

私有适配层已能统一读取六所当前现货/永续挂单及 USDT 永续仓位。原始远端快照不作为公开 HTTP 接口
返回，运营 API 只暴露对账状态、计数和脱敏阻断原因，本地已核对的交易账本另由受认证接口读取。
Bybit 游标会读取到末页；其余接口一旦达到单页上限或交易所声明的总数超过本页，统一标记结果不完整，
不得用截断结果通过启动对账。worker 会持久化这些远端明细，并将任何未匹配的挂单或仓位列为阻断原因；
成交仍需按本地客户端订单 ID 和时间窗口关联后才能构成完整真相。
挂单匹配同时使用交易所订单 ID 与客户端订单 ID，并核对市场、标的、方向、原生数量和 reduce-only；
已匹配但尚未终结的 IOC 仍禁止新交易。仓位匹配将本地配对仓位基础币数量按开仓腿合约乘数还原为交易所
原生数量，并核对空头方向、账户快照要求的逐仓/跨仓模式和杠杆；完全匹配的既有套利仓位不再仅因“账户有仓位”被误判为未知，
且真实开仓预检允许在这些已核对仓位之外继续建立不同标的配对仓位；额外、缺失或冲突仓位仍会在
`/api/system/execution` 的账户原因中阻断。
六所私有客户端现已提供逐订单成交 REST 查询，统一输出交易所成交/订单 ID、客户端订单 ID、市场、
标的、方向、价格、数量、费用资产、标准化费用和 maker/taker 时间。OKX、Bitget 等原始负数扣费统一
转换为正数成本、正数返佣转换为负数；触及单页上限或交易所要求的订单 ID 尚未知时返回
`complete=false`，不得据此把订单标记为已完整对账。原始交易所响应仅在私有适配层使用；本地核对后的
成交可通过 `GET /api/trades/fills` 读取。worker 会对非终态真实订单腿调用该接口，校验市场、标的、方向、
客户端 ID 和交易所订单 ID 后幂等写入本地 `fills`，再由全部成交重算订单腿累计数量、加权均价及状态。
`GET /api/system/execution` 的账户项包含 `fill_reconciliation_complete` 和 `fill_count`；分页不完整
或缺少必需的交易所订单 ID 时前者为 `false`。账户项同时包含 `private_stream_ready`；它只在认证完成、
订单/成交/仓位三类订阅全部成功且最近心跳不超过 30 秒时为 `true`。Binance、OKX、Bybit、Bitget、
Gate LIVE/SANDBOX 与 MEXC LIVE 的认证连接均已装配到常驻 worker。任一私有事件会合并唤醒同一
执行器锁内的
严格 REST 对账，快速更新订单、成交和仓位账本；固定周期对账仍作为漏事件与断线恢复路径。
worker 还会每秒比较当前凭据的交易所、环境和更新时间：新凭据启动新连接，替换凭据先关闭旧连接再用
新密钥建立连接，删除凭据关闭并移除连接。凭据变化先把非暂停状态置为 `reconciling`，连接成功或断开
都会立即唤醒对账；既有 `paused` 原因不会被凭据操作覆盖。
当全部已配置账户的余额、交易权限、持仓模式、远端挂单/成交/仓位关联以及私有流新鲜度都通过时，
worker 才会把账户项和全局状态置为 `ready`；任一账户失败或阻断都会保持 `blocked`，已有的
`paused` 安全状态优先且不会被普通对账清除。
真实执行器在网络预检完成、两腿订单进入提交事务前会再次对 `execution_control` 加锁并要求仍为
`ready`，因此管理员暂停与正在进行的预检并发时也不会在暂停之后把订单腿改为 `submitted`。
固化了 Gate 基础币扣费规划的开仓意图还要求最新私有账户快照继续确认 GT 与点卡替代抵扣均未启用；
若设置已变化，意图以 `spot_fee_mode_changed` 预检代码暂停，双腿保持未提交，管理员必须重新生成
预览。
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
Gate Sandbox 在任何双腿写请求前用 TestNet 标记价格、价格偏离限制、价格步长和新鲜一档校验永续
IOC 保护价。允许的价格只可在原策略滑点内收紧；若价格保护带无法触及盘口，自动策略不创建意图，
已确认意图则以 `market_unexecutable` 失败且双腿保持 `created`。平仓预留会释放，账户执行状态保持
`ready`，也不会投射故障通知；操作账本以中文说明“Gate 测试网盘口超出交易所价格保护范围，双腿均
未提交”。真正送达交易所后的拒绝仍沿用全局暂停与反向保护边界。
常驻 worker 已在每轮严格 REST 对账前调用该真实执行器；现有 HTTP 确认只安全持久化 `planned`
意图，不需要也不得由 API 进程直接访问交易所写接口。
预览票据绑定管理员、交易参数、规则与配置指纹，并保存双腿绝对保护价，只在 60 秒内允许原子预留一个
UUID 幂等键；票据本身不是交易意图，worker 不会读取它。API 确认时刷新 15 秒内盘口和容量，并只在
原保护价仍可成交时持久化 `planned` 意图，不持有交易所写职责。
平仓票据还绑定 `paired_position_id`，确认事务用仓位行锁把仓位置为 `closing`，从而阻止对同一仓位并发
确认。平仓数量必须与仓位剩余基础币数量完全一致，并使用开仓永续腿持久化的合约乘数；当前目录的乘数或
步长发生不兼容变化时拒绝生成预览。重复使用同一票据和幂等键返回原意图，换键、换仓位或行情变化均拒绝。
唯一 worker 会消费已确认的真实平仓意图；发单前要求没有远端挂单，并把全部本地配对空仓按标的、原生
数量、杠杆和账户要求的逐仓/跨仓模式与远端完整快照精确匹配。现货腿只能卖出且不得 reduce-only，永续腿只能
reduce-only 买回。任一不一致都在接触写接口前暂停。
真实成交只在两腿 IOC 均终结且 REST 成交分页完整后结算；原生成交量先按订单腿乘数换算为基础币。
现货买入以基础币扣费时先从毛成交量扣除手续费，再进行等量判断；净等量非零成交创建配对仓位，双零
成交将意图置为失败。无法用 USDT 或基础币成交价折算的手续费进入 `manual_review` 并保持全局暂停。
若交易意图已固化基础币扣费规划、两腿均完整成交、永续数量等于计划净数量，且实际净现货只多出小于
规划预留的一张合约网格尘埃，则直接按永续数量建仓，不再提交必然多买一张的保护单；任何超界、部分
成交或扣费方式偏差仍进入原补偿流程。
单腿或净数量失衡会先按多余基础币数量持久化唯一反向补偿腿，worker 使用新鲜盘口与紧急滑点提交
保护性 IOC；补偿 ACK、订单和成交继续复用严格 REST 对账，不能直接由 HTTP API 触发或重发。
补偿提交前按最新标的规则量化原生数量：买入保护向上取整，卖出保护向下取整。订单腿保留原始目标
基础币数量、实际可执行数量和一个数量步长的基础币容差；只有完整成交后剩余方向表现为现货尘埃且
严格小于该容差时才可结算，永续侧不能留下未配对裸敞口。账户对账失败原因使用
`reconciliation_failed:{private_request|ledger|internal}:{safe_code}`，前端翻译已知账本代码，
不会返回交易所响应正文、签名 URL 或凭据。
交易账本的父意图、订单腿与成交响应都提供同一 `trade_intent_id` 关联语义；父意图额外返回
`activity_at=max(意图更新时间, 所有订单腿更新时间)`，并按该值倒序读取。前端同时展示创建时间、
最近活动时间和短意图编号，因此远端成交延迟补记时父意图会与更新后的订单腿一起浮到顶部；零成交的
失败腿显示订单状态及脱敏失败码，不伪造成交明细；前端把已知 Gate 失败码翻译为中文，未知安全码
原样展示以便排查。
真实平仓使用相同的终态和分页完整性门槛。等量成交按真实加权均价计算现货与永续价差，扣除按数量分摊
的剩余开仓费及本次真实平仓费；完整成交关闭仓位，等量部分成交递减仓位并解除本次 closing 锁，以便
再次预览剩余数量。双零成交恢复仓位为 open 并安全失败；失衡、超量或费用无法折算保持仓位 closing、
进入 `manual_review` 并暂停。补偿完整成交时仅按主腿共同数量减仓；多余主腿与反向补偿的实际往返
价差及补偿费用一并进入本次实现 PnL。若共同数量为零，仓位保持不变，但真实往返损益仍写入实现事件。
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

配对持仓页在可见期间每 5 秒重新读取仓位估值。“未实现 PnL（价格）”按
`数量 × [(现货买一 - 现货开仓价) + (永续开仓价 - 永续卖一)]` 计算，代表立即按一档价格平掉
现货多头和永续空头时的价格浮盈亏。“预估最终收益”按
`既有已实现净 PnL + 未实现价格 PnL + 已入账实际资金费 - 剩余开仓费 - 预计双腿平仓费`
计算；它只使用已经同步入账的资金费和当前一档价格，不预测未来资金费、盘口滑移或费率等级变化。
行情不可用时该估值为空。主表显示两个收益指标，现货/永续开仓价与平仓估值、资金费、费用费率及
费用组成放在每行可展开的二级详情中。

WebSocket 首帧为 `snapshot`，后续帧为带单调 `sequence` 的 `update`；客户端发现序号断层或重连时
重新读取 REST 快照。浏览器关闭与服务端发送并发时，服务端把 Starlette 已关闭状态与普通断线同等
处理，立即取消订阅并安静退出，不继续向已关闭连接发送。

自动策略配置没有危险默认值：环境和至少一个目标交易所、1–10 倍杠杆、单笔最大名义额、单所/全局敞口、
最大并发、当前/24h/7d 年化阈值、最低净收益、最低/最大开仓基差、最低两腿名义额、盘口容量倍数、正常/紧急
滑点、每日最大亏损、重入间隔、最长持有、最低清算缓冲、资金费/净收益/基差平仓阈值及止盈止损必须
全部提交并通过交叉校验。保存配置只创建新版本。唯一 worker 在完整账户对账重新进入 `ready` 后调用
确定性的单动作自动评估器；开仓金额按单笔上限、双腿一档容量除以盘口安全倍数、单所剩余额度和全局
剩余额度取小，低于最低两腿名义额则跳过。生成的开平仓仍只是 `planned` 意图，由下一轮既有真实执行器重新读取账户、
校验余额/持仓/权限并发单。自动意图写入审计事件；达到每日已实现亏损限额时自动状态转为 `paused`。

`minimum_opening_basis` 与 `maximum_opening_basis` 必须分别位于 `(-1, 1)`，且最低值不得高于最大值；
前端新策略默认使用 `[0, 0.02]`，即拒绝永续折价并把可接受溢价限制在 2% 内。不可变的升级前策略
JSON 缺少最低值时，服务端只在读取时补入 `-0.999999999999` 兼容值，不修改历史载荷；管理员保存
新版本后才会持久化前端选择的最低值。

界面使用简体中文和全浅色主题，固定侧栏、顶栏、表格、表单与空状态均使用白色/浅灰层级；API 字段、
代码枚举和交易所名保持英文。正收益、健康状态和当前选中机会使用绿色语义，负收益使用红色，次级说明
使用深灰色。v2 机会页提供三类 tab，候选可直接带入任务编辑器；编辑器支持 2–16 腿，并只在 Maker
模式显示盘口档位、追价次数和回退方式。旧控制面统一位于“系统管理”，不与 v2 主导航混排。

扫描设置中的每所候选数允许 10–500，默认 500；保存后由后端统一校验。趋势历史按小时采样，保留期
允许 1–7 天并由前后端共同限制；该设置不改变 5 秒实时排名、worker 决策或资金费率历史下载。
