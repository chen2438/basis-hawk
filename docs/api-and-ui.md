# API 与界面

生产服务在容器网络监听 `0.0.0.0:8000`，只通过 Caddy 域名 HTTPS 暴露。健康检查与静态登录页公开；
其余 HTTP 和 WebSocket API 均要求管理员会话。当前市场接口为 `/api/opportunities`、单机会
`/history`、`/api/exchanges/status`、`/api/settings` 和 `/api/ws/opportunities`。
所有比例使用小数值字符串，例如 `0.001` 表示 `0.1%`。

管理员使用密码与 TOTP 登录。服务通过 Secure、HttpOnly、SameSite=Strict Cookie 保存会话；
所有修改请求还必须提供与 Cookie 会话绑定的 `X-CSRF-Token`。连续失败登录受限流保护。
前端不会把会话令牌写入 localStorage。

交易所凭据接口为：

- `GET /api/accounts/credentials`：仅返回交易所、环境、标签、更新时间和 API Key 掩码；
- `PUT /api/accounts/{exchange}/{sandbox|live}/credentials`：保存或替换 API Key、Secret，
  OKX/Bitget 还必须提供 passphrase；
- `DELETE /api/accounts/{exchange}/{sandbox|live}/credentials`：删除本地加密凭据。
- `GET /api/accounts/{exchange}/{sandbox|live}/snapshot`：按需解密凭据并从交易所读取 USDT
  现货可用余额、永续可用余额/权益、账户类型和持仓模式；响应仍不包含任何凭据。
- `GET /api/system/execution`：读取 worker 的全局执行阻断状态，以及各账户最近一次启动对账状态。

写入接口只接受已认证且 CSRF 校验通过的请求。明文只在单次请求内进入内存，随后使用绑定交易所与环境的
AES-GCM 关联数据加密；响应、审计事件和日志均不得包含 API Secret、passphrase 或完整 API Key。
`paper` 环境不接受交易所凭据。

账户快照使用各所官方只读接口和签名规则。签名错误、超时及 HTTP 错误统一映射为不带请求 URL、
签名参数或响应原文的脱敏错误。MEXC 和 Gate 没有满足同所现货+USDT 永续完整验收要求的沙盒，
其 `sandbox` 快照明确返回不支持，不会回退到实盘地址。Bybit V5 不直接返回无持仓标的的全局持仓模式，
因此当前快照如实返回 `unknown`；模式未知时后续状态机必须禁止下单，不能按默认值猜测。
当前 worker 只完成余额、权益及账户模式快照；在挂单、成交和仓位的 REST/私有流对账完成前，
全局执行状态固定为 `blocked`，该状态不能由 API 绕过。

WebSocket 首帧为 `snapshot`，后续帧为带单调 `sequence` 的 `update`；客户端发现序号断层或重连时重新读取 REST 快照。

界面使用简体中文与高对比度浅色主题，API 字段、代码枚举和交易所名保持英文。正收益、健康状态和当前选中机会使用绿色语义，负收益使用红色，次级说明使用深灰色，保证白色背景上的可读性。费用仅是可编辑估算，页面必须提示地区、账户等级和活动费率可能不同。

扫描设置中的每所候选数允许 10–500，默认 500；保存后由后端统一校验。
