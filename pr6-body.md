## 范围

### PRIV-15：架构与数据隔离契约

- 固定 Core、SnowLuma Gateway、Configuration Service、Plugin Registry/Runner、Workflow Engine、Web API 和 Worker 的边界。
- 提供 v1 Pydantic 契约与 JSON Schema，覆盖统一消息、媒体、会话、人格、工具、记忆、插件、工作流、密钥和审计。
- 补齐可信主体与当前 conversation 绑定的 private/group/global 数据隔离，以及 Engine → Runner 的锁定 `ExecutionContext`。
- 统一 `Failure(trace_id, error_code, retryable, message)` 和失败状态约束。

### PRIV-20：Provider 协议边界、返修与管理页面

- 支持 `openai_responses`、`openai_chat_completions`、`anthropic_messages` 三类协议，按稳定 `providerId` 精确选择，禁止猜测协议或跨 Provider 回退。
- 三类协议提供非流式文本调用；三类流式调用提供增量文本、工具事件、用量和完成事件，并对截断、超时、取消和中途错误 fail closed。
- Anthropic 保留输入/输出 token 用量和工具调用身份；Chat/Anthropic 不声明原生搜索，不发送 Responses 图片格式；Anthropic 不要求 Embedding 字段。
- Provider API 统一清理 Pydantic 校验输入，非法秘密不会进入 422 响应、日志或审计路径；端点拒绝 URL userinfo，密钥加密、遮盖和留空保留。
- Provider、通用设置和 SnowLuma 管理页补齐协议提示、未配置密钥状态、刷新/取消、重复提交保护和读取失败禁用保存。
- 文档与迁移说明更新，新增多协议流式回归测试。

## 交付验证

- 集成提交：`8901f9c`，已推送 `origin/dev`。
- 任务修订提交：`be3b9e6`。
- 35 项后端测试、Ruff、前端类型检查和构建通过。
- 镜像：`vireyabot:test-priv20-be3b9e6`。
- 隔离 PostgreSQL 空库迁移、应用健康检查、未认证 401、镜像重启后健康检查通过。
- 真实外部 Provider 凭据未使用；真实服务验收仍需授权配置。