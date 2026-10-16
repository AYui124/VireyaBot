已完成返修并提交 `66f1cd2`。

交付：
- Provider 持久化协议类型支持 `openai_responses`、`openai_chat_completions`、`anthropic_messages`；按 `providerId` 精确构造适配器，不猜测、不跨 Provider 回退。
- 三类适配器提供非流式文本调用；Chat Completions 与 Anthropic 提供 SSE 增量流，统一输出增量、完成、用量/工具事件，并将超时、传输、中途错误映射为稳定 `ProviderFailure`。
- 能力边界显式拒绝：Anthropic 的 Embedding、原生搜索、结构化输出和图片输入；Chat Completions/Anthropic 不接受 Responses 图片格式。
- Provider 页面新增三类协议选择、端点提示、模型/密钥字段；密钥仍只显示配置状态，地址拒绝内嵌凭据，非法 API Key 限制长度且不会回显。
- 恢复通用设置与 SnowLuma 设置表单；读取失败或未完成读取时禁止保存，密钥留空保留旧值。

已验证：
- `uv run pytest -q`：33 passed。
- `uv run ruff check app tests`：通过。
- `npm ci && npm run typecheck && npm run build`：通过。
- 仿真协议烟测：三类非流式调用通过；Chat/Anthropic 流式均收到两个独立增量，且完成事件晚于增量。
- Chrome 实际打开构建页面并确认管理控制台导航与系统概览可见；未连接真实 PostgreSQL/Provider 服务，真实外部凭据验收仍需项目经理在授权实例上执行。

未修改长期部署实例。工作区自动生成的 `AGENTS.md` 与 `.omp/` 未纳入提交。