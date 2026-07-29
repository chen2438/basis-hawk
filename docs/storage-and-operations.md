# 存储与运维

生产数据库固定使用 PostgreSQL，URL 由 `BASIS_HAWK_DATABASE_URL` 指定。Alembic 是生产 schema
的唯一迁移入口；应用只为 SQLite 测试数据库自动建表，禁止在 PostgreSQL 启动时隐式 `create_all`。
`20260729_0034` 开始建立通用多腿 v2 schema。迁移在任何 DDL 前检查旧 `paired_positions`、
`order_legs` 和 `trade_intents`：只要存在非零/未关闭仓位、活动或未知订单、仍可执行或人工复核意图，
整次迁移立即失败。管理员必须先平掉全部仓位、撤销全部挂单并终结旧意图，不能依靠迁移替代交易所
平仓。通过检查后，旧意图、腿、成交、已关闭仓位和 PnL 复制到新历史表；旧表在 v2 首次切换后的
一个备份周期内保持只读，后续再用独立迁移删除。
`20260729_0035` 为每条任务腿增加非空交易所。迁移先按 `account_id` 回填，再按旧 `trade_intents`
回填迁移历史，最后只接受能由 `instruments` 唯一匹配的无账户纸面腿；仍无法唯一确定时在添加非空和
六所检查约束前中止。不能仅凭 `BTCUSDT` 等多所重名 symbol 选择交易所。
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
worker 会拒绝运行。隔离容器验收在启动竞争 worker 前会轮询同一 advisory lock，直到 PostgreSQL
明确报告主 worker 已持锁；主 worker 提前退出或超时未持锁会单独失败，不能用固定延时制造 CI 竞态。

仓库根目录的 `scripts/deploy_vps.sh` 把首次安装和后续幂等升级固化为一个入口。Ubuntu/Debian 新机可
显式使用 `--install-docker`，脚本按
[Docker 官方 apt 仓库步骤](https://docs.docker.com/engine/install/)安装 Engine、Buildx 和 Compose
插件；已有 Docker 的其他 Linux 发行版可直接运行而不使用该参数。典型首次部署为：

```bash
sudo ./scripts/deploy_vps.sh \
  --domain hawk.example.com \
  --install-docker
```

空 VPS 无需预先 clone。`scripts/bootstrap_vps.sh` 可从 GitHub Raw 远程执行，缺少 Git 时先通过系统
apt 安装，然后把官方仓库 clone 到 `/opt/basis-hawk` 并调用上述部署脚本：

```bash
curl -fsSL \
  https://raw.githubusercontent.com/chen2438/basis-hawk/main/scripts/bootstrap_vps.sh \
  | sudo bash -s -- \
  --domain hawk.example.com \
  --install-docker
```

执行前应先检查远程脚本内容。重复运行会验证现有 checkout 的 origin、当前分支和工作区；仅同一
`main` 分支的干净工作区允许 `fetch` 后 fast-forward，分叉、detached HEAD、本地修改、错误远端或
非 Git 的目标目录全部失败，不删除或强制覆盖文件。可用 `--install-dir`、`--repository` 和
`--branch` 显式选择受信任的安装位置或 fork，其余参数原样交给部署脚本。以 `curl | bash` 从交互式
SSH 会话启动时，bootstrap 会在读取完脚本后把部署脚本的标准输入重新连接到控制终端，因此最终确认、
管理员密码和 TOTP 创建都可以正常交互。没有控制终端的自动化环境必须传入 `--yes`，且仅在已有管理员
或明确接受无法登录时传入 `--skip-admin`。

首次运行从 `.env.example` 创建权限为 600 的 `.env`，生成 URL-safe 数据库密码、32 字节凭据主密钥
和另一把独立的 32 字节备份密钥，全程不向终端输出秘密。已有 `.env` 时只校验域名、密钥、数据库 URL
和权限，绝不覆盖或补写；域名参数与现有配置不一致会失败。管理员密码仍只经 `getpass` 的隐藏终端输入，
不能通过命令参数或环境变量注入；输入前会提示至少 12 个字符，长度不足或确认不一致时原地重新输入，
不会退出部署。管理员创建返回的 TOTP URI 只暂存在部署脚本进程内，不写文件；脚本在成功完成全部检查
或后续步骤失败退出时，都会把 URI 作为最后一段醒目输出，随后清除进程状态。无交互初始化可用
`--skip-admin`，但在以后创建管理员前无法登录。
`--prepare-env-only` 只生成或检查配置，不安装或启动任何服务。

每次部署都从当前同一 Git checkout 重建 API、worker 和 backup 三个本地镜像，禁止复用较早版本的
worker。重复部署识别已有 Alembic schema 后，先停止 API、worker 和定时 backup，并用刚重建的独立
备份镜像创建认证加密归档；只有备份成功后才拉取 PostgreSQL/Caddy 镜像、刷新数据库容器并执行迁移。
随后启动全部服务，依次验证 PostgreSQL、API liveness、六所行情目录 readiness 和最新加密备份；失败
会保留容器与日志供排查，不删除数据库或卷。已有部署在停止应用服务后的备份、拉取、迁移或更新后
对账步骤失败时，退出钩子会尽力用 `docker compose start` 重启原有 API、worker 和备份容器；恢复
失败会明确告警，避免静默留下 Caddy 502。全部健康检查成功后，脚本把 Docker build cache 压到
1 GB，并清理已经失去标签的旧应用镜像；清理失败只告警，不把健康部署误报为失败。使用
systemd-journald 的宿主机还会安装
`/etc/systemd/journald.conf.d/basis-hawk.conf`，把持久日志上限设为 200 MB、至少保留 1 GB 空闲空间并
限制为 7 天，然后压缩既有 journal。该设置不改变 SSH 密码登录，也不启用或修改 UFW。
脚本默认不启用或修改 UFW。只有显式传入 `--enable-ufw` 时才会先放行当前 SSH 服务端口及
80/443，再启用 UFW；非 22 端口应显式传入 `--ssh-port`。Docker 官方说明发布的容器端口可能绕过
部分 UFW 规则，因此还必须在云厂商安全组只开放 SSH、80 和 443；当前 Compose 本身只发布 80/443。
脚本不修改 DNS、云安全组、交易所 Key 或异地备份目标。

从 Git checkout 部署时，部署脚本还会安装 root 所有的 `basis-hawk-update.path` 和
`basis-hawk-update.service`。API 只对 `/var/lib/basis-hawk-updater/request` 拥有写权限，只读挂载
`status`，不挂载 Docker Socket、`/opt/basis-hawk` 或 updater 配置。systemd 代理从 root-only 配置
读取部署时锁定的项目目录、HTTPS origin 和分支；请求格式只有版本、UUID、`check|update` 以及受
严格十六进制校验的目标提交，不存在命令、参数、路径或仓库字段。API 的代理可用状态与实际请求入队
使用相同边界，都会拒绝符号链接请求目录；代理使用独占锁，拒绝符号链接、脏工作区、
origin/branch 不一致、非快进历史及目标不再等于远端头的请求。检查只 fetch；更新在 API
已经持久化全局暂停后快进，并调用同一
`deploy_vps.sh --skip-admin --reconcile-after-update --yes`，因此仍执行升级前加密备份、迁移和健康
检查。迁移后、启动新 worker 前，受限的 `basis-hawk update-reconcile` 命令用行锁检查当前暂停必须
精确来自 `software update requested`，才切换为 `reconciling` 并写入系统审计；命令支持同一更新
部署的幂等重试，但拒绝解除人工暂停、补偿失败或任何其他安全原因。部署脚本即使由升级前的旧代理
调用，也会在迁移后执行一次非强制条件探测，因此安装本功能的首次更新已经能够自动对账；新版代理
传入严格参数，预期的更新暂停不存在时会把部署标为失败。新 worker 启动后立即运行完整
账户、订单、成交、仓位和私有流对账，全部通过才写回 `ready`，不再要求管理员手动点击重新对账。
状态文件只含提交 ID、时间和预定义错误码；完整输出留在
`journalctl -u basis-hawk-update.service`。首次获得该功能必须仍用远程 bootstrap 升级一次以安装
宿主机代理，此后才能在前端检查和更新。

需要无人值守更新时，在一次受控部署中显式加入 `--enable-auto-update`。安装器会启用 root 所有的
`basis-hawk-auto-update.timer`，开机两分钟后开始、之后约每 5 分钟检查一次；后续部署未传开关时
保留当前设置，可用 `--disable-auto-update` 明确关闭。自动检查只支持锁定的 GitHub HTTPS 仓库，
并通过 GitHub Actions API 查找远端头对应的 `ci.yml`、`push`、`completed/success` workflow run。
未找到成功结果（包括 CI 尚未完成或失败）时不排队，也不会先拉取运行代码。

CI 通过后，自动代理仍先验证干净 checkout、固定 origin/branch、快进关系及没有其他更新请求，再在
数据库行锁下要求全局执行精确为 `ready`。安全暂停成功后，自动代理先释放更新锁、清除更新服务的
历史失败计数，再原子发布路径监听器可见的请求文件；这样路径单元不会在检查阶段抢跑并争抢同一把
锁。人工暂停、安全暂停、blocked/reconciling 或准备命令失败时不会发布请求并会等待下一周期。
实际更新继续由同一个 `basis-hawk-update.service` 二次验证远端头并执行备份、快进、重建、迁移、
健康检查和更新后自动对账。VPS 主动访问 GitHub，GitHub 不需要保存 VPS SSH 私钥或其他服务器秘密。
可用以下命令检查定时器和最近一次自动检查；完整部署输出仍查看原更新服务：

```bash
systemctl status basis-hawk-auto-update.timer
journalctl -u basis-hawk-auto-update.service -u basis-hawk-update.service
```

Compose 还提供独立非 root `backup` 服务。它使用与 PostgreSQL 17 服务端同版本的 `pg_dump`；没有
归档的首次启动会立即生成一份 custom archive，已有归档时则从最近一份每日归档的 UTC 时间续算默认
86400 秒周期，避免部署前安全备份与随后容器重启连续生成两份。归档在写入命名卷时直接使用独立
`BASIS_HAWK_BACKUP_KEY` 做 AES-256-GCM 认证加密，明文数据库不会落盘；每份归档另有 SHA-256 文件，
恢复验证还会校验 GCM tag 并让 `pg_restore --list` 解析完整归档。每日归档只保留最新 7 份，每周日
另保留一份周归档并只保留最新 4 份。构建镜像时运行时代码显式归属固定的非 root 用户，即使远程
checkout 因严格 umask 使用 0600 文件也能读取，不会在升级前安全备份阶段中断。备份密钥必须独立于
凭据主密钥生成：

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
docker compose up -d --build
docker compose exec backup python3 -m basis_hawk.backup verify \
  /backups/basis-hawk-YYYYMMDDTHHMMSSZ-daily.bhbk
```

校验先把完整归档解密到空设备以验证 AES-GCM tag，再把同一归档交给静默的 `pg_restore --list` 解析
TOC。对于包含实际表数据的大归档，`pg_restore --list` 成功取得 TOC 后可能提前关闭标准输入；只有其
退出码为 0 时才把该 `BrokenPipe` 视为正常，非零退出仍使校验和部署失败。对象清单不会刷满终端。
API 以同一非 root UID 对备份卷拥有删除权限，但不持有备份密钥。Web 单个及批量删除入口只接受严格
命名的 `basis-hawk-YYYYMMDDTHHMMSSZ-(daily|weekly).bhbk`，拒绝符号链接、路径片段、重复项、缺失
归档和最新归档。批量请求最多 100 项，服务端会在删除任何文件前验证整批目标；成功后同步删除每份
`.sha256`，删除请求和成功结果分别进入不可修改审计。

生产恢复必须先进入维护暂停并停止 `api`、`worker` 和 `backup`，先验证目标归档，再恢复到一个新的空
数据库。工具默认拒绝非空目标；只有灾难恢复明确要清理当前数据库时，才可同时提供 `--confirmed`
和 `--clean`。恢复后运行迁移、启动服务并等待完整账户对账，不能直接宣称可交易：

```bash
docker compose stop api worker backup
docker compose run --rm backup verify /backups/basis-hawk-YYYYMMDDTHHMMSSZ-daily.bhbk
docker compose run --rm backup restore \
  /backups/basis-hawk-YYYYMMDDTHHMMSSZ-daily.bhbk --confirmed
docker compose up -d
docker compose run --rm worker basis-hawk worker --once
```

命名卷仍只在单台 VPS 上；必须另行把加密归档和校验文件复制到受控异地存储，且把备份密钥与归档分开
保管。丢失 `BASIS_HAWK_BACKUP_KEY` 无法恢复，泄露该密钥则应视为全部历史备份泄露。

首次部署必须配置 32 字节 URL-safe Base64 主密钥
`BASIS_HAWK_CREDENTIAL_MASTER_KEY`，再运行：

```bash
docker compose run --rm api basis-hawk admin-create --username admin
```

管理员密码使用 Argon2id，TOTP 密钥和后续交易所凭据使用 AES-256-GCM；关联数据绑定管理员或交易所环境，
数据库只保存密文、nonce 和密钥版本。主密钥不得写入仓库、日志或数据库。
`BASIS_HAWK_SESSION_HOURS` 默认及生产示例均为 `168`，即 7 天；登录时同一值同时写入数据库
`admin_sessions.expires_at` 和两个 Cookie 的 `Max-Age`。修改该配置只影响此后创建的新会话，
不会延长或缩短数据库中已经存在的会话。
TOTP 泄露或身份验证器丢失时，在 VPS 上运行：

```bash
cd /opt/basis-hawk
sudo docker compose --env-file .env run --rm api \
  basis-hawk admin-rotate-totp --username admin
```

命令只通过隐藏终端提示读取当前管理员密码。密码错误时不修改任何认证状态；验证成功后在一个数据库
事务中写入新的加密 TOTP、删除该管理员全部会话并记录不含密钥的 `auth.totp_rotated` 审计事件，然后
只显示一次新 provisioning URI。所有浏览器需要使用新 TOTP 重新登录。
同一交易所、同一环境只保存一个账户配置；替换和删除都会写入不含秘密值的审计事件。API 读取只能返回
Key 掩码，私有适配器在进程内按需解密，不能把解密结果缓存到数据库或发送给前端。
私有请求的签名查询串必须和实际发送顺序完全一致；异常消息禁止包含完整 URL，因为查询参数可能带签名。
Binance 账户快照用现货账户的 `canTrade` 与永续 `/fapi/v1/accountConfig` 的 `canTrade` 共同确认
双腿权限；`/fapi/v3/account` 继续只用于余额和权益，不能读取其已移除的配置字段。OKX 账户快照读取
当前 Key 的 `perm`，只有包含 `trade` 才确认可交易；Bybit 额外调用当前 Key 信息，
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
其中账户快照单独保存 `perp_margin_mode=isolated|cross`，不能只凭账户名称推断仓位保证金模式。
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
常驻 worker 每秒读取不含明文的凭据摘要，以交易所、环境和 `updated_at` 管理连接生命周期。新增凭据
会启动对应私有流，替换会等待旧连接安全关闭后用新凭据重建，删除会停止并移除连接；连接建立或断开
都会唤醒严格 REST 对账。凭据服务在写入、模式声明更新或删除前先把非 `paused` 执行状态切换为
`reconciling`，关闭旧连接时因此不会把一次明确的配置变更误判为意外断线；已有人工或安全暂停保持原样。
Binance 私有连接由两条通道组成：现货使用
`userDataStream.subscribe.signature`，USDT 永续使用 `/fapi/v1/listenKey` 后连接私有
WebSocket，并每 30 分钟续期。只有现货签名订阅返回成功且永续 listenKey 与连接都建立后，通用监督器
才可登记三类订阅就绪；任一通道关闭、ping/pong 失败或续期返回不同 listenKey 都使整个 Binance 账户
断开并进入 REST 对账。沙盒统一使用 Binance Demo Trading 环境：现货 REST/WebSocket 分别连接
`demo-api.binance.com` 与 `demo-ws-api.binance.com`，USDⓈ-M REST/WebSocket 分别连接
`demo-fapi.binance.com` 与 `demo-fstream.binance.com`；四条通道共享同一组 Demo Key，不连接独立的
Spot Testnet。
常驻 worker 会为每个已配置的 Binance 沙盒或实盘账户创建该连接，并与 60 秒 REST 对账循环并行运行；
`worker --once` 不创建无法持续保活的 WebSocket。
OKX 使用单条生产或模拟盘私有 WebSocket，按官方
`timestamp + GET + /users/self/verify` 规则登录，并在返回成功后订阅 `orders(ANY)`、
`positions(ANY)` 和 `account`。普通订单频道包含成交更新；专用 `fills` 频道仅向特定 VIP 等级开放，
因此不能把它作为普通账户的就绪前提。三个通用频道全部确认后才登记订单、成交和仓位订阅就绪；空闲时
使用 OKX 要求的文本 `ping`/`pong`，频道连接数错误或任一通用错误均触发断线阻断。常驻 worker 已装配
OKX。
Bybit 账户快照在无 USDT 非零持仓时会以 `BTCUSDT` 做一次只读持仓查询，从返回的 `positionIdx`
识别单向或双向模式；若空子账户仍不返回索引，只接受与凭据一同加密保存的管理员模式声明，未声明继续
阻断。声明入口不接收或回显 Key、不调用 Bybit 写接口，并写脱敏审计；目标标的配置杠杆前仍会重新核对
自身模式。UTA 2.0 逐仓的账户级可用余额字段不适用，因此永续可用额按 USDT 币种钱包扣除仓位初始
保证金、订单初始保证金、锁定额和赠金占用计算；交叉与组合保证金才使用账户级可用余额。
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
Gate LIVE/SANDBOX 各自使用现货与 USDT 永续两条私有连接。LIVE REST 使用
`https://api.gateio.ws`，SANDBOX REST 使用 `https://api-testnet.gateapi.io`；沙盒现货私有流连接
`wss://ws-testnet.gate.com/v4/ws/spot`，沙盒永续私有流连接
`wss://ws-testnet.gate.com/v4/ws/futures/usdt`，绝不回退到实盘地址。现货订阅
`spot.orders`、`spot.usertrades` 的全标的更新；永续先通过对应环境的签名 REST 账户接口读取并验证
正整数用户 ID，再订阅 `futures.orders`、
`futures.usertrades`、`futures.positions` 的全合约更新，连接显式发送
`X-Gate-Size-Decimal: 1` 以保留十进制合约数量。两条连接都必须通过 WebSocket 协议 ping/pong；
任一通道断开即使整个 Gate 连接失败并由监督器重连。Gate LIVE 与 SANDBOX 凭据以
`(exchange, environment)` 分别保存，worker 可同时热重载两套连接；账户快照、私有流状态、订单、
仓位和交易意图始终携带环境，禁止跨环境组成套利双腿。常驻 worker 已装配 Gate。
Gate 账户快照同时签名读取官方 `/wallet/fee`；当 API Key 没有钱包读取权限时，再只读回退到 Gate
仍兼容的 `/spot/fee`，不因权限范围扩大要求而丢失扣费模式。只有 `gt_discount=false` 且
`debit_fee` 明确不为 GT 或点卡抵扣时，系统才把 `spot_buy_fee_in_base=true` 持久化到本轮
`account_snapshots`；任一替代抵扣启用写为 false，两个接口均失败或字段不完整写为 null。只有 true
才允许开仓规划提前按配置的现货 taker 费率折减预计基础币净到账量；其他状态保持原双腿毛数量规划。
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

同一轮还从 Binance、OKX、Bybit、Bitget、Gate 和 MEXC 的私有账单读取实际 USDT 资金费。
`funding_income` 以交易所、环境和远端记录 ID 唯一约束幂等保存金额、可用费率、仓位价值和结算时间；
首次配置后回看最近 24 小时，后续从最后成功保存的结算时间向前重叠 1 分钟增量查询。资金费接口失败或
返回下一页时，账户对账分别记录 `funding_income_complete=false` 和本轮记录数，但该分析账本不参与
订单、成交、仓位的交易安全放行条件。这样不会因报表接口短暂故障中断风险处置，也不会把截断结果伪装
成完整历史。
全局 `paused` 时 worker 仍执行只读账户与成交对账，并对每个远端活动订单调用对应交易所撤单接口；
返回值必须确认市场、标的及已有订单标识，没有确认或调用失败都记录为账户阻断原因。撤单受理不会直接
篡改本地订单终态，下一轮仍须按客户端订单 ID 查单并拉取完整成交。管理员恢复只把控制状态改为
`reconciling`，后续完整检查通过才允许 worker 写回 `ready`。真实下单的最终数据库提交事务也会锁定
并重新检查该控制行，关闭“预检开始后管理员刚好暂停”的竞态窗口。
六所私有适配器可以按本地订单腿的交易所订单 ID 查询成交，并明确报告分页是否完整；需要交易所订单 ID
但 ACK 尚未关联时必须继续阻断。远端成交通过
`(order_leg_id, exchange_trade_id)` 唯一约束幂等写入，避免不同交易所可能重复的数字成交 ID 冲突；
写入前强制核对市场、标的、方向和订单 ID，随后从完整本地成交集合重算订单腿累计数量及加权均价。
订单腿的 `updated_at` 只在交易所订单 ID、累计成交量、加权均价或状态实际变化，或者新增成交时更新；
重复返回完全相同订单和成交集合的周期/事件对账保持原时间，避免历史订单因健康核对被反复置顶。
每个账户最近的 `fill_reconciliation_complete` 和 `fill_count` 随启动快照持久化。
本地订单腿已经处于 `submitted`、`acknowledged`、`partially_filled` 或 `unknown`，但下单 ACK
未能保存交易所订单 ID 时，worker 会先使用持久化的客户端订单 ID 向对应交易所查单；已经有关联 ID 的
非终态 IOC 也在每轮用同一客户端 ID 刷新终态，不能把一次 ACK 当作订单仍然活动。找回结果必须逐项
核对客户端 ID、市场、标的、方向、原始数量和 reduce-only 标记，完全一致才允许关联
`exchange_order_id` 并继续查询成交；单纯查单响应不会把订单标记为已成交，成交状态仍只从幂等成交账本
推导。IOC 在部分成交后撤销时，订单腿保留 `canceled` 终态及真实累计成交量，成交汇总不会把它重新改成
活动的 `partially_filled`。未找到、查询窗口受限或结果不完整时保持阻断，绝不据此重发订单。每个账户同时保存
`order_reconciliation_complete` 和本轮 `recovered_order_count`。

### 通用多腿 v2 表

`execution_tasks` 保存任务级环境、基础资产、数量模式、对冲触发、基础币/USDT 双敞口上限、重试上限、
预检票据和乐观锁版本；`execution_task_legs` 保存有序的主腿/对冲腿、账户、现货/永续、方向、目标及
单次数量、Maker/Taker 模式、滑点、追价策略、保证金模式和杠杆。Pydantic 输入层要求同一任务只有
一个主腿、所有腿使用同一基础资产和 USDT 计价/结算、非纸面腿必须绑定账户，并拒绝目标净 Delta
超过任务基础币上限。

`execution_runs`、`execution_orders` 和 `execution_fills` 分别保存每轮执行、每次追价/重试产生的
真实委托以及交易所成交。客户端订单 ID 全局唯一；同一腿的尝试号和追价号唯一；未确认旧单终态前
不得创建下一条追价订单。`arbitrage_strategies`、`strategy_legs` 和 `strategy_pnl_events` 保存任务
完成后的组合、逐腿剩余数量与不可变已实现 PnL。`funding_income` 增加可空的账户和策略腿关联，
`adl_snapshots` 保存各所原生值及归一化 1–5 级风险。

任务创建先显式 flush `execution_tasks` 父行再写 `execution_task_legs`，不依赖 SQLAlchemy 对无
relationship 模型的排序。UUID 幂等键和规范化请求指纹共同防止重放歧义，并发唯一键冲突回滚后只在
指纹一致时返回原任务。预检只允许 draft/preflight_ready，按乐观锁版本写入脱敏 JSON 和 60 秒到期
时间；启动事务锁定任务并同时检查到期时间、版本、状态及非纸面任务的全局执行控制，然后才切换 queued。
取消只接受尚未开始的三种状态，运行后必须由 worker 的停止/补偿状态机处理。

`exchange_credentials` 取消“交易所+环境唯一”，改为“交易所+环境+标签唯一”，并用部分唯一索引分别
保证每组最多一个默认交易账户和默认扫描账户；既有凭据迁移为两种默认账户。能力矩阵和账户费率以
脱敏 JSON 持久化，密文、nonce 和关联数据规则不变。`/api/v2/accounts` 可通过稳定账户 ID 管理多行；
旧 API、私有流、对账和双腿执行器在通用 worker 接管前只访问交易默认行。删除默认账户时事务内提升
同组最早剩余账户；已被任务或策略外键引用的账户拒绝删除。

`trade_intents` 在执行前保存幂等键、请求指纹、市场时间、配置哈希、金融数量、状态与乐观锁版本；
终态失败还保存受数据库约束的预定义 `failure_code`，当前区分行情过期、双腿零成交、单腿补偿归零和
状态机显式终止。禁止在该字段写入异常字符串、交易所响应或凭据；迁移会把真实/沙盒环境下恰有两条
`created` 订单腿的旧失败意图可靠回填为行情过期，其余无法由账本证据确定的旧失败保持空值，由界面
明确说明无法追溯，而不猜测原因。
真实订单在发单事务前失败时仍保持 `planned`，以便管理员修正账户后安全重试，同时写入受约束的
预检代码；代码区分凭据、客户端初始化、账户快照、远端订单/仓位读取、快照新鲜度、双腿权限、持仓
模式、远端完整性、未匹配订单/仓位、余额、永续保证金/杠杆配置和平仓状态。意图代码和
`execution_control` 的 `paused` 原因在同一事务提交，成功进入 `executing` 时清除旧阻断代码。
全局原因只编码交易所和安全代码，不持久化底层异常正文、交易所响应、签名 URL 或秘密。
`order_legs.failure_code` 只保存明确、不可重试 HTTP 4xx 响应中的标准标签或 HTTP 状态归一化后的
小写安全码。原始消息、响应正文、请求 URL 与签名均不入库；该字段随订单腿账本 API 返回，供界面
显示具体拒绝原因。
Gate Sandbox 已确认意图若在提交前发现永续一档位于 TestNet 价格保护带之外，会在单个事务中以
`market_unexecutable` 终结：两条订单腿保持 `created`，平仓仓位从 `closing` 恢复为 `open`，
`execution_control` 不变。该结果表示市场当前不可执行而非执行故障，因此不生成故障通知；自动策略
在同一条件下不会创建意图。补偿腿只有在保护价可触及一档时才从 `created` 转为提交中。
`trade_previews` 保存真实开平仓预览的动作、管理员、交易所/环境、标的、名义金额、杠杆、最大滑点、
规则/配置指纹、双腿绝对保护价、60 秒有效期及可空的确认 UUID；平仓预览还以外键绑定配对仓位。
确认会刷新 15 秒内盘口、容量和规则，但只要行情仍在原双腿保护价内就不会因正常波动废票；实际意图
继续使用票据中的保护价。首次确认在行锁内原子预留
该 UUID；同一票据不能被另一管理员确认，也不能换幂等键、换动作或换仓位生成意图。未确认票据永远
不会被 worker 执行。
`strategy_versions` 以 UUID 和单调版本号保存完整 JSON 配置、环境、创建者和时间；版本写入后不可修改。
`automation_control` 是 ID=1 的独立单例，初始状态固定为 `disabled`，保存当前策略 UUID、
`disabled/enabled/paused`、原因和操作者。启用前 API 必须重新读取账户执行 `ready`、策略内容和目标
凭据；MEXC 不允许出现在 sandbox 策略，Gate sandbox 策略必须具有独立 Gate TestNet 凭据。暂停不会
覆盖账户级 `execution_control`，因此后续仍可
对账和人工平仓。每次创建版本、启用、暂停、恢复和禁用都写审计事件。
`latest_opportunities` 以交易所为主键，把该所最新完整机会聚合为一份 JSON 数组。约 3,000 条机会因此
只需覆盖 6 行，而不是每 5 秒逐行更新 3,000 行；API 与 worker 仍读取相同的 `Opportunity`，行情时间、
双腿盘口容量和规则语义不变。表使用 50% fillfactor、较小 TOAST tuple target，并对主表与 TOAST 设置
5 条更新阈值的积极 autovacuum，及时回收压缩 JSON 的旧版本。`opportunity_snapshots` 改为每小时至多
一条，保留期硬限制为 1–7 天。升级迁移在服务停止且加密备份完成后，把既有分钟快照按
交易所/标的/小时只保留最后一条并执行 `VACUUM FULL ANALYZE`；旧实时表则通过重建为聚合表直接归还
已经膨胀的关系文件空间。
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
常驻 worker 与对账、私有流任务并行运行通知投递器；`worker --once` 也会在对账后消费一批。Telegram
使用官方 `sendMessage` HTTPS 接口，消息为不解析标记的受保护纯文本且限制为 4096 字符；Bot token
只保留在进程内，请求异常及完整 URL 不进入日志或数据库。SMTP 支持 `starttls` 和连接即 TLS 的
`smtps`，使用系统证书校验，阻塞 SMTP 客户端通过工作线程执行。异常按认证、收件人/发件人拒绝、
TLS 不可用、4xx 暂时不可用、5xx 拒绝、其他 SMTP 协议错误和网络传输错误依次归一化；由于
`SMTPException` 继承 `OSError`，协议异常必须在通用网络异常之前匹配。配置项如下；某一通道的必要
字段未完整提供时该通道不创建发送器，已入队记录会以 `channel_unconfigured` 脱敏失败码退避，交易
循环不受影响：

```text
BASIS_HAWK_TELEGRAM_BOT_TOKEN
BASIS_HAWK_TELEGRAM_CHAT_ID
BASIS_HAWK_TELEGRAM_WEBHOOK_SECRET
BASIS_HAWK_SMTP_HOST
BASIS_HAWK_SMTP_PORT
BASIS_HAWK_SMTP_SECURITY=starttls|smtps
BASIS_HAWK_SMTP_USERNAME
BASIS_HAWK_SMTP_PASSWORD
BASIS_HAWK_SMTP_FROM
BASIS_HAWK_SMTP_TO=owner@example.com,backup@example.com
BASIS_HAWK_NOTIFICATION_BATCH_SIZE
```
`notification_projection_state` 为全局执行、每个交易所账户及交易意图保存最近状态指纹和递增 generation。
投影器每秒检查状态变化：普通 `hedged/closed` 成交只进入 Telegram；补偿中、失败、人工复核、执行暂停
及账户 blocked/error 同时进入 Telegram 和邮件。持续相同状态不会重复入队；状态恢复后再次出现相同
错误时 generation 递增，因此会产生新的唯一去重键。worker 每次启动先以“不发送”模式建立当前状态
基线，避免部署通知功能或重启时回放全部历史事件。通知正文只使用交易所、环境、标的和归一化状态，
不复制管理员自由文本、交易所响应或账户错误详情。
Telegram 入站固定为 `POST /api/integrations/telegram/webhook`，这是唯一不使用管理员 Cookie/CSRF 的
集成入口。必须同时满足 `X-Telegram-Bot-Api-Secret-Token` 与环境中的 webhook secret 恒定时间比较，
以及消息 `chat.id` 与唯一管理员 chat ID 完全一致；请求体最大 32 KiB。官方 setWebhook 应只订阅
`message` 更新并配置上述 secret。重复 update ID 通过 outbox 去重，不会重复回复。机器人只识别
`/status`、`/positions`、`/alerts`、`/health`，所有回复为只读数据库摘要；任何 `/pause`、`/resume`、
`/trade` 或配置命令都不会执行，并只返回只读命令清单。
投影器跨过 UTC 日界时汇总刚结束的完整自然日，并同时入队 Telegram/邮件。日报包含
`pnl_realizations` 的实际净 PnL 与事件数、当日完成开仓/平仓数、失败或人工复核数，以及发送时仍活动
的配对仓位和非 ready 账户数量。日期也是持久化投影指纹，所以同一日报只发送一次；进程启动时只建立
最近已结束日期的基线，不补发可能已经人工处理的历史日报。
`internal_transfers` 保存 USDT 同所现货↔USDT 永续划转的 UUID 幂等键、请求指纹、方向、金额、远端
划转 ID、提交前来源/目标余额、预期目标余额及状态时间。数据库约束不允许其他资产、方向或任意目标；
模型完全不存在地址、链、UID、邮箱或提现字段。`settings.transfer_limits` 是运行时全局限额的唯一
事实来源；首次访问尚无该设置时，才用 `BASIS_HAWK_TRANSFER_PER_REQUEST_LIMIT_USDT` 和
`BASIS_HAWK_TRANSFER_DAILY_LIMIT_USDT` 初始化，默认均为 0。管理员可在 Web 内部划转页修改，两项
必须同时为 0（禁用）或同时为正且单次不得超过日限额；更新人、更新时间随值持久化，并在同一事务写入
`transfer.limits_updated` 脱敏审计。计划事务锁定该设置行及全局执行控制，按 UTC 日累计所有非明确
failed 金额，超限则不落库；因此运行中修改会立即生效且不能与新计划并发绕过。成功计划会立即暂停
新交易并写入不含凭据的审计事件。私有适配层现已支持 Binance、Bitget Classic、Gate 和
MEXC LIVE 的提交；Binance、Bitget Classic 和 MEXC 使用各自官方历史/单号接口查询远端状态，
Bitget 还把本地 UUID 作为交易所防重 ID。Gate `POST /wallet/transfers` 的成功响应和 `tx_id`
已经表示同一账户内交易余额划转完成，随后只需确认目标余额；`GET /wallet/order_status` 仅用于
主子账户划转，禁止用于现货↔永续划转。OKX、Bybit 与
Bitget UTA 的当前交易账户共享现货和永续余额，因此明确返回无需划转。
唯一 worker 每轮先处理最早的一笔 `submitted`/`pending`，没有待确认记录时才认领一笔 `planned`。
认领事务要求全局已经暂停，并在调用交易所前写入提交前双侧余额、预期目标余额、`submitted_at` 和
`submitted` 状态。网络调用后持久化远端 ID；若 ACK 不确定，仅 Bitget 可依靠客户端防重 ID
查回远端 ID，Binance、Gate、MEXC 转 `manual_review`，不会重发。Gate 成功返回后会在同一 worker
轮次立即刷新快照；所有交易所都必须确认目标可用余额不低于预期值才置为 `completed`。远端状态或到账
在 15 分钟内仍无法确认会转
`manual_review`；所有完成、失败和人工复核都写脱敏审计，执行状态继续保持暂停，必须由管理员发起
全量对账恢复。
真实意图额外固化 1–10 倍请求杠杆，旧记录迁移为安全默认值 1；开仓意图还固化
`spot_buy_fee_in_base`，旧记录安全迁移为 false。该值为 true 时，现货订单数量仍按请求名义额和
现货/共同网格向下取整，永续基础币数量改为“现货数量 × (1 - 现货 taker 费率)”再向下取整到永续
合约网格；因此计划仓位 `base_quantity` 等于永续净对冲数量，现货预留只允许形成小于一张合约网格
的多头尘埃。发单预检重新读取 Gate 账户费率，不再明确扣基础币时以
`spot_fee_mode_changed` 阻断且不提交任一腿。
`order_legs` 同一意图固定一条现货腿和一条永续腿，并在提交交易所前生成唯一客户端订单 ID；每条腿还
保存严格为正的 `base_multiplier`，使交易所原生数量及成交量可以无歧义换算成基础币。已有纸面订单腿
迁移时乘数为 1；后续真实永续腿必须使用标的目录的合约乘数。创建意图的事务显式先 flush
`trade_intents` 父记录，再插入两条 `order_legs`，不能依赖未声明 ORM relationship 时不稳定的 flush
排序。SQLite 测试连接始终启用 `PRAGMA foreign_keys=ON`，容器验收还会直接在 PostgreSQL 17 上创建
父意图及双腿，防止本地测试绕过生产外键。
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
由于 API 与 worker 位于独立进程，手动确认不能依赖进程内 Event 唤醒执行器。worker 在两次 60 秒
完整对账之间每秒执行一次轻量 PostgreSQL 存在性查询；只有全局 `ready` 下的普通真实意图，或全局
`paused` 下的紧急真实平仓，才会触发立即运行完整一轮。查询不读取订单腿、不调用交易所，也不会在
暂停时因普通开仓意图形成忙循环。这样手动确认通常在 1 秒内进入既有预检，同时保持 15 秒行情过期
硬限制不变。
本轮所有配置账户再次通过 REST、私有流及本地账本核对并进入 `ready` 后，worker 才读取当前生效的
不可变策略、最新机会、交易规则、全部仓位、当前远端清算价快照和 UTC 当日实现 PnL，最多规划一个
自动动作。新建意图会设置进程内对账事件，使下一轮立即由真实执行器重新预检，而不等待普通 60 秒周期。
策略 UUID、动作、仓位/标的及行情时间共同生成确定性 UUID 幂等键；同一行情重放或 worker 重启不会
重复规划，已有未完成开仓意图也阻止同标的再次规划。
每轮最多提交一个真实意图；提交后必须先完成远端对账，不能在上一组 IOC 状态尚未确认时继续发下一组。
真实执行器发起任何私有请求前再次检查意图的 `market_observed_at`；超过 15 秒或来自未来超过 5 秒的
`planned` 意图直接标为 `failed`。过期平仓在同一事务中把仍由该意图预留的仓位恢复为 `open` 并清空
`closing_intent_id`，因此崩溃恢复既不会按旧价格下单，也不会永久锁死仓位。
持仓读取通过 `opening_intent_id` 批量联结不可变开仓意图，向 API 投影其杠杆，并用剩余基础币数量
乘以现货真实开仓均价计算剩余 USDT 名义额；不在 `paired_positions` 重复存储可由父意图和成交价
确定的数据，也不改变用于平仓与远端仓位核对的基础币数量。
`trade_previews` 和 `trade_intents` 以受检查约束的 `emergency` 布尔值区分紧急配对平仓，开仓不能带
该标记。普通预览最大滑点仍由 API/规划器限制为 0.1，只有紧急平仓可使用 0.25。全局暂停时真实执行器
不消费普通开仓或平仓，只消费显式紧急平仓；最终提交事务也仅对此组合接受 `paused`，其他意图仍要求
`ready`。紧急路径复用相同的仓位行锁、双腿规则、客户端订单 ID、reduce-only、成交结算和幂等恢复。
REST 成交对账完整且两条主腿都进入 `filled`、`canceled` 或 `failed` 终态后，worker 才尝试结算真实开仓。
两腿原生成交量分别乘以 `base_multiplier`；现货买入成交若以基础币收取手续费，必须无条件先从现货
毛成交量扣除该费用，再与永续基础币成交量比较。净数量相等且非零时，使用真实加权均价创建
`paired_positions`；净数量失衡时进入既有补偿流程，即使两腿毛成交量原本相等也不能跳过保护。
USDT 费用直接计价，基础币费用按该笔成交价折算；其他折扣币费用当前无法可靠估值，因此进入
`manual_review` 并全局暂停。两腿均为零成交时意图安全失败且不创建仓位。
基础币扣费感知的意图只有在两腿均完整成交、永续成交量等于固化净数量，且现货扣费后剩余多头严格
小于规划时预留的数量差时，才直接按永续数量建仓并保留安全尘埃；若手续费为零、改用其他资产、部分
成交或残差达到预留上限，继续走原失衡补偿和全局暂停，不能用预计费用覆盖真实成交证据。
新产生的暂停会在本轮对账结束时重新读取并保留，不能被通用 `blocked` 状态覆盖。
两条真实主腿终态后数量失衡时不再立即伪造结果或永久停在人工状态：结算事务以多余腿相反方向创建唯一
`spot_compensation` 或 `perp_compensation`，原生数量按该腿 `base_multiplier` 精确换算，意图进入
`compensating` 并全局暂停。worker 只在最新机会不超过 15 秒、当前规则完整、补偿名义额不超过对应
最优档容量时，按生效策略 `emergency_max_slippage`（没有匹配策略时为 1%）更新保护价并原子把补偿腿
置为 `submitted`；随后才调用交易所。要求和提交分别写入脱敏审计事件。
提交事务还会用现行 `spot_quantity_increment` 或 `perp_quantity_increment` 固化实际原生数量：
买入保护向上取整，卖出保护向下取整。`order_legs.compensation_target_base_quantity` 保存量化前需
中和的基础币敞口，`compensation_tolerance_base` 保存一个原生步长折算的基础币界限。结算要求补偿
完整成交，并验证剩余差额只在现货一侧且严格小于该界限；该尘埃不计入配对仓位数量，远端永续仓位仍
能按原生网格与本地配对仓位精确对账。部分成交、反方向尾差或达到一个完整步长仍进入人工复核。
补偿腿与普通腿使用相同的唯一客户端订单 ID、ACK 不确定状态、按 ID 查单、成交分页及幂等填充规则。
旧订单腿仍要求完整补偿量精确等于原主腿基础币差额；带容差的新订单腿按上述安全方向和单步上限验证。
不足、零成交、费用不可折算、补偿价格缺失或任一已成交主腿缺失可靠均价都进入 `manual_review`，
且不会清除安全暂停，也不会用空价格尝试计算损益。完整补偿后，开仓
只按共同数量创建仓位，并把补偿往返损失及所有三腿费用计入
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

CI 对提交信息、后端 Ruff/Pytest、VPS/bootstrap 脚本 Bash 语法和前端 Vitest/TypeScript/Vite 分别
验收。bootstrap 测试使用临时本地 Git 远端验证首次 clone、参数传递、管道输入重新连接控制终端、
干净 checkout 的 fast-forward 和脏目录拒绝。60 秒交易预览票据测试在每个用例实际开始时生成时间，
而不是在 Pytest 收集阶段固定时间，避免较慢 runner 把原本验证管理员或行情指纹冲突的样本误判为过期。
GitHub Action 固定使用官方 Node 24 运行时版本：`actions/checkout@v6`、
`actions/setup-python@v6`、`actions/setup-node@v6` 和 `pnpm/action-setup@v6`；升级 Action
主版本时必须先核对其官方 `action.yml` 的 `runs.using`。
部署
脚本测试使用伪 VPS/Docker 命令验证首次安装顺序、API/worker/backup 同版本重建、秘密不出现在输出、
配置幂等、权限/域名拒绝及升级时“重建三镜像 → 停 API/worker/backup → 加密备份 → 更新外部镜像 →
迁移”的严格先后。容器层另执行
`docker compose --env-file .env.example config --quiet`；PostgreSQL 可用时还必须实际运行 Alembic
upgrade/current。

仓库提供完全隔离且不读取本地 `.env` 的容器验收命令：

```bash
.venv/bin/python scripts/container_acceptance.py
```

该命令只生成一次性数据库密码、凭据主密钥和备份密钥，使用随机后缀创建临时网络、PostgreSQL 17
容器和备份卷；构建正式 API/前端镜像与专用备份镜像后，实际执行全部 Alembic 迁移和 `alembic check`
模型/数据库漂移检查、API ready 检查、真实意图父记录与双订单腿外键写入、API 进程重启、PostgreSQL
重启及连接池恢复、advisory lock 排他
worker、单轮 worker、AES-GCM 归档验证、空库恢复和非空库拒绝覆盖。应用及备份运行均使用只读根文件
系统和 `/tmp` tmpfs。无论成功或失败都只按本次随机名称清理资源，不会连接 Compose 项目、读取真实
凭据或接触交易所。CI 的
`container-acceptance` job 会执行同一命令。

更新相关 shell 文件还必须通过：

```bash
bash -n scripts/install_update_agent.sh
bash -n scripts/update_agent.sh
bash -n scripts/auto_update_agent.sh
```

生产镜像严格从 `requirements.lock` 安装 Python 依赖。修改 `pyproject.toml` 的运行时依赖后，应使用
Python 3.12 和
`pip-compile --upgrade --strip-extras --no-header --no-annotate pyproject.toml`
重新生成锁文件；SQLAlchemy 使用 `asyncio` extra，不能依赖开发机偶然预装 `greenlet`。提交前及 CI
都要运行：

```bash
.venv/bin/pip-audit -r requirements.lock
```

任何已知运行时漏洞必须先升级到无公告的兼容版本并完成后端、容器和认证回归，不能仅忽略审计结果。

API 容器挂载同一 `postgres_backups` 卷，仅用于列出归档以及受控单个或批量删除非最新归档和旁路
SHA-256 文件。
API 不接收 `BASIS_HAWK_BACKUP_KEY`，因此不能解密、验证内容或恢复数据库；实际认证验证和恢复仍只允许
通过专用备份镜像命令执行。管理员通知测试同样只创建 outbox 项，不让 API 进程直接连接 Telegram 或
SMTP。通知日志清理只删除超过管理员指定保留期的 `sent/dead` outbox 行，不触碰待发送、发送中或重试
记录；`audit_events` 继续不可通过应用删除。PostgreSQL、API、worker、backup 和 Caddy 都使用 Docker
`json-file` 的 10 MiB × 5 文件轮转上限，避免宿主机运行日志无限增长，也不向应用暴露 Docker Socket。
