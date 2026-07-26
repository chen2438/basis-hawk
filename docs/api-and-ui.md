# API 与界面

生产服务在容器网络监听 `0.0.0.0:8000`，只通过 Caddy 域名 HTTPS 暴露。健康检查与静态登录页公开；
其余 HTTP 和 WebSocket API 均要求管理员会话。当前市场接口为 `/api/opportunities`、单机会
`/history`、`/api/exchanges/status`、`/api/settings` 和 `/api/ws/opportunities`。
所有比例使用小数值字符串，例如 `0.001` 表示 `0.1%`。

管理员使用密码与 TOTP 登录。服务通过 Secure、HttpOnly、SameSite=Strict Cookie 保存会话；
所有修改请求还必须提供与 Cookie 会话绑定的 `X-CSRF-Token`。连续失败登录受限流保护。
前端不会把会话令牌写入 localStorage。

WebSocket 首帧为 `snapshot`，后续帧为带单调 `sequence` 的 `update`；客户端发现序号断层或重连时重新读取 REST 快照。

界面使用简体中文与高对比度浅色主题，API 字段、代码枚举和交易所名保持英文。正收益、健康状态和当前选中机会使用绿色语义，负收益使用红色，次级说明使用深灰色，保证白色背景上的可读性。费用仅是可编辑估算，页面必须提示地区、账户等级和活动费率可能不同。

扫描设置中的每所候选数允许 10–500，默认 500；保存后由后端统一校验。
