# 存储与运维

生产数据库固定使用 PostgreSQL，URL 由 `BASIS_HAWK_DATABASE_URL` 指定。Alembic 是生产 schema
的唯一迁移入口；应用只为 SQLite 测试数据库自动建表，禁止在 PostgreSQL 启动时隐式 `create_all`。

Docker Compose 当前提供 PostgreSQL、FastAPI 和 Caddy。Caddy 自动管理 TLS，只暴露 80/443；
数据库只在 Compose 网络可见。生产启动顺序为数据库健康检查、`alembic upgrade head`、API 健康检查、
Caddy 接入。

首次部署必须配置 32 字节 URL-safe Base64 主密钥
`BASIS_HAWK_CREDENTIAL_MASTER_KEY`，再运行：

```bash
docker compose run --rm api basis-hawk admin-create --username admin
```

管理员密码使用 Argon2id，TOTP 密钥和后续交易所凭据使用 AES-256-GCM；关联数据绑定管理员或交易所环境，
数据库只保存密文、nonce 和密钥版本。主密钥不得写入仓库、日志或数据库。
同一交易所、同一环境只保存一个账户配置；替换和删除都会写入不含秘密值的审计事件。API 读取只能返回
Key 掩码，私有适配器在进程内按需解密，不能把解密结果缓存到数据库或发送给前端。

`.env.example` 只包含占位符。修改它时不得读取、输出或提交本地 `.env`；交易所 Key 必须禁止提现并绑定
VPS 出口 IP。

CI 对提交信息、后端 Ruff/Pytest 和前端 Vitest/TypeScript/Vite 分别验收。容器层另执行
`docker compose --env-file .env.example config --quiet`；PostgreSQL 可用时还必须实际运行 Alembic
upgrade/current。
