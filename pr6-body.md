## 范围

### PRIV-15：架构与数据隔离契约

- 固定 Core、SnowLuma Gateway、Configuration Service、Plugin Registry/Runner、Workflow Engine、Web API 和 Worker 的边界。
- 提供 v1 Pydantic 契约与 JSON Schema，覆盖统一消息、媒体、会话、人格、工具、记忆、插件、工作流、密钥和审计。

### PRIV-20：Provider 协议边界、官方 SDK 与管理页面

- OpenAI Responses 与 Chat Completions 使用官方 `openai` Python SDK 的 `AsyncOpenAI`；Anthropic Messages 使用官方 `anthropic` Python SDK 的 `AsyncAnthropic`。
- 使用 SDK 请求模型、异步流接口和异常类型；生产路径删除手写 HTTP 鉴权、响应解析和 SSE 分帧。VireyaBot 仅保留 `providerId` 配置注入、能力拒绝、统一契约归一化和脱敏错误边界。
- 保留三类协议的非流式/流式文本、用量/完成、工具增量身份与完成、截断/超时/取消关闭、能力拒绝和 Provider 隔离回归。
- 修复 SnowLuma 运行配置读取缺少返回值，验证非空配置可构造 Gateway。
- 继续覆盖秘密校验脱敏、Provider/通用设置/SnowLuma 页面交互、迁移和旧配置兼容。

## 交付验证

- 官方 SDK 依赖已锁定在 `pyproject.toml` 与 `uv.lock`：`openai`、`anthropic`。
- 集成 dev 提交：`19f6cf2`，已推送 `origin/dev`。
- 后端 35 项测试与 Ruff 通过；前端类型检查和构建通过。
- 官方 SDK MockTransport 协议测试通过；SDK 版本：openai 1.109.1、anthropic 0.125.0。
- 镜像：`vireyabot:test-priv20-sdk-c782923`；镜像内 SDK import smoke、隔离 PostgreSQL 空库迁移、健康检查、未认证 401、重启后健康检查通过。
- Docker 构建使用冻结 `uv.lock` 导出依赖并通过镜像源安装，避免运行时未安装官方 SDK。

真实外部 Provider 凭据未使用；未合并 main、未更新长期实例、未操作外部 SnowLuma。