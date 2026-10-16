# Provider 协议验证 v1

## 接口边界

`app.providers.ModelProvider` 是 Agent 的唯一模型边界；OpenAI Responses/Chat Completions 由官方 Python SDK `openai` 的异步客户端和流接口执行，Anthropic Messages 由官方 Python SDK `anthropic` 执行。适配器只负责注入 `providerId` 解析出的配置、统一契约归一化和错误边界；跨边界请求、结果、流事件和向量对象均带 `schema_version: "v1"`。

上游响应字典只在适配器内部存在，离开适配器后使用 `ProviderOutput`、`ToolCallResult`、`StreamEvent`、`EmbeddingResult` 等冻结契约。标准 `output[].content[].text` 会归一化为正文；`refusal`、failed/incomplete 响应和流式异常均转换为固定、脱敏 Failure。工具流事件保留 `id`/`call_id`，可用于提交 `function_call_output`。

Provider 只负责选择官方 SDK、注入端点/密钥、统一契约和 SDK 异常分类。可信 actor、conversation/scope、workflow version、component ref、capabilities 和权限顺序由 Core/Workflow/Runner 按 `docs/architecture-contract-v1.md` 校验；Provider 不宣称代替这些隔离或锁定版本检查。

业务秘密只从 PostgreSQL 配置服务的 `read_provider_runtime_settings(session, provider_id)` 读取。管理员通过 `/providers` 中文管理页新增、编辑和启停多条资源；`providerId` 是稳定引用，读取 API 只返回秘密是否配置和 Header 名称。连接资源、`ProviderSecret` 和 `ProviderModelConfig` 分表并显式关联。未来插件只能通过宿主标准调用引用 ID，不能获得 Key、原始上游流或数据库连接。旧单套 `provider.*` 由 bootstrap 迁移后删除，空库保持空状态。YAML 不覆盖管理员保存值。运行时入口为 `build_provider(session, provider_id)`，未知、停用和不支持的能力均拒绝，不自动回退。

## 首期支持矩阵

| 协议/能力 | 非流式 | 流式 | 能力边界 |
| --- | --- | --- | --- |
| `openai_responses` | 官方 `openai.AsyncOpenAI.responses` 文本、图片、结构化输出、工具、Embedding | 官方 Responses stream 增量、用量、完成、失败/截断 | 原生搜索仅此协议且显式启用 |
| `openai_chat_completions` | 官方 `openai.AsyncOpenAI.chat.completions` 文本、结构化输出、工具、Embedding | 官方 Chat Completions stream 增量、工具增量、用量、完成；SDK 流结束异常会失败 | 不接受 Responses `input_image`；不支持原生搜索 |
| `anthropic_messages` | 官方 `anthropic.AsyncAnthropic.messages` 文本、工具 | 官方 Messages stream 文本/工具增量、输入/输出用量、工具完成、完成；SDK 流结束异常会失败 | 不声明 Embedding、图片、结构化输出或原生搜索 |

三类协议都由 `build_provider(session, provider_id)` 按持久化协议字段选择，禁止根据 URL 猜测或跨 Provider 回退。流事件统一为 `StreamEvent`；用量事件先于完成事件。工具增量保留 `id`/`call_id`/名称，便于 Runner 继续处理。

`responses(request)` 返回统一聚合结果；`request.stream` 显式布尔值优先于模型 `default_stream`，未传才采用默认。`stream_responses(request)` 始终返回真正增量的统一事件，不向宿主暴露 SDK 原始流。关闭默认流式不删除流式能力。

事件以 `response_id` 标识响应，用量位于 `usage` 字段。正文仅由 `response.output_text.delta` 拼接，工具参数只位于 `output.arguments`，以 `response.tool_call.delta` / `response.tool_call.completed` 区分增量与完成；稳定调用身份位于 `output.call_id`。聚合保留响应 ID、文本、工具列表与用量。Chat 支持合法 finish reason 后不带 usage 的结束；Anthropic 汇总 message_start 输入用量与 message_delta 输出用量。截断或中途错误不产生成功完成；调用方必须取消消费任务或 `aclose()` 关闭下游断开的流。

映射后的异常通过标准 traceback/`logger.exception` 渲染时不包含原始 SDK 异常链。调试脱敏覆盖官方 SDK、`httpx` 与 `httpcore` 的连接、HTTP/1.1、HTTP/2、代理日志，在源 Logger 的 Handler 处理前过滤。SDK/HTTPX 保留静态模板和数值状态码；httpcore 已拼入消息的响应 Header、异常和其他动态内容被移除，保留传输事件及阶段。不能只过滤 `LogRecord.args` 或只过滤父 Logger：前者遗漏 httpcore 的预格式化消息，后者不能保护子 Logger 自有 Handler。不要记录运行时配置、私有提示词或原始 SDK 对象；脱敏不是允许记录业务内容的授权。

通用错误证据覆盖官方 SDK 的认证、限流、超时、连接、请求拒绝、失败响应和异常截断；适配器不再手写 HTTP 鉴权头或 SSE 分帧。真实端点未使用本任务凭据验证。

能力声明通过 `capability_matrix()` 区分 `adapter_supported`、配置 `enabled` 和 `endpoint_verified`。Chat Completions 与 Anthropic 不会因 `native_search_enabled` 配置错误声明原生搜索；Anthropic 的 Embedding 字段对外返回空且管理 API 不要求填写。

## 配置字段

| PostgreSQL resource field | 类型 | 秘密/说明 |
| --- | --- | --- |
| `Provider.protocol_type` | enum | `openai_responses`、`openai_chat_completions` 或 `anthropic_messages` |
| `Provider.base_url` | string | SDK 基础地址，拒绝 userinfo、query、fragment 和具体 API 端点后缀 |
| `ProviderSecret.api_key` | string | 是，Fernet 加密，错误响应不回显 |
| `ProviderModelConfig.responses_model` | string | 否；Anthropic Messages 模型字段 |
| `ProviderModelConfig.embedding_model` | string | 否；Anthropic 不要求且对外返回空 |
| `ProviderModelConfig.embedding_dimensions` | integer/null | 否；向量必须匹配 |
| `Provider.timeout_seconds` | number | 否 |
| `Provider.organization` | string | 否；OpenAI 兼容协议使用 |
| `Provider.project` | string | OpenAI 兼容协议项目字段，使用 SDK 公开参数 |
| `ProviderModelConfig.native_search_enabled` | boolean | 否；仅 Responses 适配器支持 |
| `ProviderModelConfig.default_stream` | boolean | 调用未显式指定 stream 时使用 |
| `ProviderModelConfig.temperature` | number/null | 空值不传 SDK；OpenAI 0–2，Anthropic 0–1 |
| `ProviderModelConfig.max_output_tokens` | integer | 1–32768；转换为协议对应的输出限制参数 |
| `ProviderSecret.extra_headers` | encrypted JSON | 加密名称/值映射，仅名称可读取 |

Anthropic 基础地址可使用根地址、`/v1` 或 `/v1/`；数据库保留用户保存值，适配器先移除尾部斜杠，再去掉末尾的 `/v1`，交给官方 SDK 添加 `/v1/messages`。例如 `/gateway/v1/` 正确请求 `/gateway/v1/messages`，保留代理路径前缀，不生成 `/v1/v1/messages`。公共创建/更新 → PostgreSQL 解析 → 官方 SDK 实际 TCP 请求覆盖非流式和流式两条路径。

先运行 `alembic upgrade head` 至 `0004_provider_project`，再启动应用执行 bootstrap。旧 `provider.*` 固定迁移为 `openai_responses`，保持确定的默认 ID 和密钥/模型关联；已有 0002 资源 ID 不变。0003 新增的空 Header 默认值不会被当作 Fernet 密文解密；已有密文不会被重复加密。重复初始化和重启不覆盖管理员值。

`GET /api/provider-protocols` 为已认证表单提供协议字段、默认值、温度边界和能力声明。POST/PUT 均支持合法页面字段，拒绝未知、协议不适用字段及非法边界。协议变更要求 `confirm_protocol_change: true`；页面确认列出移除字段，取消不发请求，确认保存后清理专属字段，共用连接、ID、Key、名称保持。温度不支持时调用方应省略；真实模型支持范围仍需端点验收。

`extra_headers` 使用行数组 `[{"name":"X-Tenant","value":"","previous_name":"X-Old"}]`：字段缺省保留全部 Header；提供数组则为完整目标集合，遗漏原行即删除。已有行空值按 `previous_name`（缺省按当前名称）保留，改名可以同时保留秘密值；新增行必须非空。名称大小写不敏感，禁止空名、重复、CR/LF、非法 HTTP 名称及 SDK 鉴权/协议/传输保留头。Key 留空保留。读取仅返回 `extraHeaderNames` / `extraHeadersConfigured` / `apiKeyConfigured`，错误与审计不包含秘密值。管理接口全部要求登录；页面读取或保存不会调用外部服务。

`DELETE /api/providers/{providerId}` 删除连接及关联秘密、模型资源，写入仅含资源 ID 的审计。通用设置与 SnowLuma 页面读取未完成或失败时禁用保存；SnowLuma 保存不启动 Gateway。

## 真实验证边界

本地官方 SDK 协议桩、真实浏览器和隔离 PostgreSQL 验证不等于真实服务验收。PRIV-35 不发布、不重启长期实例。独立复核后由 PRIV-32 补验界面、PRIV-33 按授权发布；用户只在长期 Web 后台配置端点、Key 与模型，并在 PRIV-35 回复“已配置，可以继续”。PRIV-34 交付前不复制长期 `.env`，不直连长期业务库，也不要求在 Worktree 或评论重填密钥。图文、结构化输出、工具往返、Embedding、流式和搜索的真实支持矩阵仍须逐项最小授权调用验证；未实测项不得标为已通过。