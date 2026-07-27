# Basis Hawk

Basis Hawk 是可部署在单台 VPS 上的同所现货—USDT 永续套利平台，支持六所监控、真实配对交易、
自动策略、实际资金费账本、Telegram/邮件通知、审计和加密备份。
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

代码与隔离容器验收完成后，生产交付仍需由管理员在目标环境完成：配置域名、TLS、防火墙、固定出口
IP 和异地备份；保存禁止提现且绑定出口 IP 的交易所 Key；验证 Telegram/SMTP；连续运行 72 小时纸面
模式；在 Binance、OKX、Bybit、Bitget 的受支持沙盒重复开平仓；最后由管理员明确确认最小名义金额的
实盘开仓并立即平仓。Gate/MEXC 没有满足同所现货与 USDT 永续要求的受支持沙盒，MEXC 合约写权限还
必须通过真实账户能力探测，否则系统保持只读。

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
