# 存储与运维

数据库 URL 由 `BASIS_HAWK_DATABASE_URL` 指定，默认当前目录 SQLite。启动时启用 WAL 与 busy timeout 并创建当前 schema。分钟快照批量写入；每天分批删除超过设置保留期的数据。

CLI 只有 `basis-hawk doctor` 与 `basis-hawk serve`。doctor 创建并检查数据库，依次探测四所公共目录/行情接口，不调用私有接口。

CI 对提交信息、后端 Ruff/Pytest 和前端 Vitest/TypeScript/Vite 分别验收。实时交易所探测不进入 CI，避免网络与地区限制造成不稳定结果。
