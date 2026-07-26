# 存储与运维

生产数据库固定使用 PostgreSQL，URL 由 `BASIS_HAWK_DATABASE_URL` 指定。Alembic 是生产 schema
的唯一迁移入口；应用只为 SQLite 测试数据库自动建表，禁止在 PostgreSQL 启动时隐式 `create_all`。

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
六所私有适配器可以按本地订单腿的交易所订单 ID 查询成交，并明确报告分页是否完整；需要交易所订单 ID
但 ACK 尚未关联时必须继续阻断。远端成交通过
`(order_leg_id, exchange_trade_id)` 唯一约束幂等写入，避免不同交易所可能重复的数字成交 ID 冲突；
写入前强制核对市场、标的、方向和订单 ID，随后从完整本地成交集合重算订单腿累计数量及加权均价。
每个账户最近的 `fill_reconciliation_complete` 和 `fill_count` 随启动快照持久化。

`trade_intents` 在执行前保存幂等键、请求指纹、市场时间、配置哈希、金融数量、状态与乐观锁版本；
`order_legs` 同一意图固定一条现货腿和一条永续腿，并在提交交易所前生成唯一客户端订单 ID。
非终态意图可由 worker 按创建时间恢复。纸面 worker 在单一数据库事务中更新双腿、写入 `fills` 并创建
`paired_positions`；唯一交易 ID 和开仓意图约束保证重复运行不会重复成交。真实成交仍必须以交易所
私有流或 REST 查询为准。
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
