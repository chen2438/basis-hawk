# Basis Hawk

Basis Hawk 是可部署在单台 VPS 上的同所现货—USDT 永续套利平台，支持六所监控、真实配对交易、
自动策略、实际资金费账本、Telegram/邮件通知、审计和加密备份。
权威架构、实施边界与交付顺序见 [DOCS.md](DOCS.md)。

## VPS 基础部署

要求 64 位 Linux、已解析到 VPS 的域名和稳定的系统时间。Ubuntu/Debian 空 VPS 不需要手动 clone，
直接运行：

```bash
curl -fsSL \
  https://raw.githubusercontent.com/chen2438/basis-hawk/main/scripts/bootstrap_vps.sh \
  | sudo bash -s -- \
  --domain hawk.example.com \
  --install-docker \
  --enable-ufw
```

执行远程脚本前应先在浏览器检查上述 GitHub Raw 内容。bootstrap 会安装 Git、把官方仓库 clone 到
`/opt/basis-hawk`，然后执行部署；重复运行只接受 origin/分支一致且没有本地改动的 checkout，并只做
fast-forward 更新。通过 `curl | bash` 运行时，bootstrap 会把部署确认和首次管理员创建重新连接到
当前 SSH 终端；非交互环境仍须显式使用 `--yes`，并在已有管理员或有意跳过创建时使用 `--skip-admin`。
Docker 已安装时省略 `--install-docker`；非 22 SSH 端口请同时传入
`--ssh-port PORT`。部署脚本首次运行
会创建权限为 600 的 `.env`，生成互不相同的数据库、凭据和备份密钥，然后隐藏输入管理员密码并输出
TOTP provisioning URI。密码输入前会提示至少 12 个字符，长度不足或两次输入不一致时会原地重试，
不会中止整个部署。URI 会暂存到进程内，并在全部健康检查完成后作为部署脚本的最后一段醒目输出；如果
管理员创建后的启动步骤失败，退出前仍会输出。应立即把 TOTP 加入身份验证器并妥善保存恢复信息。重复
运行不会覆盖 `.env`；已有
数据库会先停止 API、worker 和定时备份、创建加密备份，之后才更新镜像、迁移和启动。完整选项见
远程 `--help`：

```bash
curl -fsSL \
  https://raw.githubusercontent.com/chen2438/basis-hawk/main/scripts/bootstrap_vps.sh \
  | bash -s -- --help
```

脚本只让 Compose 对外发布 80/443，PostgreSQL 不发布主机端口。DNS、云厂商安全组、固定出口 IP、
异地备份和交易所 API Key 仍必须由管理员配置。
首次通过远程命令升级到包含宿主机更新代理的版本后，运营控制台会提供“检查更新”和“立即更新”。
后续更新只允许锁定 Git 远端/分支的快进提交，确认时先暂停交易，再复用同一备份、迁移和健康检查流程；
更新完成后仍需管理员重新对账。Web 容器不会获得 Docker Socket 或任意宿主机命令权限。
备份校验会先完整验证 SHA-256 与 AES-GCM，再静默解析 PostgreSQL archive；`pg_restore --list`
成功读取 TOC 后提前关闭输入属于正常成功，不会输出整份对象清单或误报 `Broken pipe`。

管理员 TOTP 泄露或设备丢失时，只能在 VPS 终端验证当前管理员密码后轮换；成功会立刻注销全部浏览器
会话并输出一次性新 URI：

```bash
cd /opt/basis-hawk
sudo docker compose --env-file .env run --rm api \
  basis-hawk admin-rotate-totp --username admin
```

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
