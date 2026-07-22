# Basis Hawk

Basis Hawk 是一个本机优先、只读的现货—USDT 永续资金费套利扫描器，支持 Binance、OKX、
Bybit 与 MEXC。它使用公开行情，不读取账户、不保存 API Key，也不会下单。

## 本地运行

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
pnpm --dir frontend install
pnpm --dir frontend build
.venv/bin/basis-hawk doctor
.venv/bin/basis-hawk serve
```

打开 <http://127.0.0.1:8000>。完整行为和开发约定见 [DOCS.md](DOCS.md)。
