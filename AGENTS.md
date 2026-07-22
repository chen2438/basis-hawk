# Basis Hawk Agent 协作约定

- `DOCS.md` 是权威文档入口；开始任务时必须完整阅读本文件和 `DOCS.md`，并按文档路由阅读受影响专题。
- 行为、接口、配置、验证方式或安全边界变化时，同一提交必须更新入口摘要和所有受影响专题。
- 每个可独立验收的功能使用单独 Git 提交。提交信息必须包含 Conventional Commit 标题、空行、说明改了什么和为什么的正文，以及实际 Agent 模型的共同作者 trailer。
- 本地仓库必须配置 `git config core.hooksPath .githooks`。提交后、推送前运行 `python3 scripts/check_commit_messages.py --commit HEAD`。
- 后端命令从仓库根目录运行；仅 pnpm 前端命令使用 `frontend/` 工作目录。
- 修改 `.env.example` 时只补充本地 `.env` 缺失项，不得读取、输出或提交 `.env`。
- 提交前执行 `DOCS.md` 指定的全量检查；前端变化还要完成浏览器验证。
- 提交通过检查后立即推送当前上游分支；失败时明确报告，不能把本地提交描述为已交付。
