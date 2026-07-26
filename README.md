# Basis Hawk

Basis Hawk 正在从本机只读资金费扫描器重构为可部署在单台 VPS 上的同所现货—USDT 永续套利平台。
权威架构、实施边界与交付顺序见 [DOCS.md](DOCS.md)。

## VPS 基础部署

要求 Docker Engine、Docker Compose v2、已解析到 VPS 的域名和稳定的系统时间。

```bash
cp .env.example .env
# 替换 .env 中所有 replace-me，并生成 BASIS_HAWK_CREDENTIAL_MASTER_KEY。
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose run --rm api basis-hawk admin-create --username admin
docker compose up -d
```

只对外开放 80/443；PostgreSQL 不发布主机端口。管理员创建命令会输出 TOTP provisioning URI，
应立即加入身份验证器并妥善保存恢复信息。

## 本地开发

测试继续使用临时 SQLite，不需要本地 PostgreSQL：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
pnpm --dir frontend install
.venv/bin/ruff check .
.venv/bin/pytest -q
pnpm --dir frontend test
pnpm --dir frontend build
```
