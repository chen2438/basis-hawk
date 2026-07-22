# Basis Hawk 权威文档入口

> 本文件与下方 `docs/*.md` 共同构成项目权威文档。最后更新：2026-07-23。

## 产品边界

Basis Hawk 使用四家交易所的公共接口，扫描同一交易所内做多 USDT 现货、做空 USDT 线性永续的资金费机会。系统只读、仅监听 localhost，不使用私有凭据、不下单、不实现跨所或反向套利。

默认每所选择两腿 24h USDT 成交额较小值最高的 100 个共同标的，且两腿成交额均不低于 100 万 USDT。价格每 5 秒、当前资金费每 60 秒、目录及历史资金费每 15 分钟刷新。

## 架构

- Python 3.12、FastAPI、Pydantic、SQLAlchemy async、SQLite WAL。
- React、TypeScript、Vite；REST 初始化与查询，WebSocket 增量刷新。
- 所有金融数值在后端用 `Decimal` 计算；API 以十进制字符串传输。
- SQLite 保留 30 天分钟级机会快照，并保存资金费历史、目录、设置及运行状态。

## 文档路由

| 任务 | 必读专题 |
|---|---|
| 交易所适配、符号归一化、收益计算、调度 | [行情与计算](docs/market-and-calculation.md) |
| HTTP/WebSocket、CLI、前端交互 | [API 与界面](docs/api-and-ui.md) |
| SQLite、配置、运行、验证、提交规范 | [存储与运维](docs/storage-and-operations.md) |

## 验证

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
pnpm --dir frontend test
pnpm --dir frontend build
.venv/bin/python scripts/check_commit_messages.py --commit HEAD
```
