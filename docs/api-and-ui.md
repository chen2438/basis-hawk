# API 与界面

服务仅监听 `127.0.0.1`。公开接口为 `/api/opportunities`、单机会 `/history`、`/api/exchanges/status`、`/api/settings`、健康检查和 `/api/ws/opportunities`。所有比例使用小数值字符串，例如 `0.001` 表示 `0.1%`。

WebSocket 首帧为 `snapshot`，后续帧为带单调 `sequence` 的 `update`；客户端发现序号断层或重连时重新读取 REST 快照。

界面使用简体中文，API 字段、代码枚举和交易所名保持英文。费用仅是可编辑估算，页面必须提示地区、账户等级和活动费率可能不同。
