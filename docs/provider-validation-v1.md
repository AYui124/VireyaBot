# Provider 协议验证 v1

## 接口边界

`app.providers.ModelProvider` 是 Agent 的唯一模型边界；OpenAI Responses/Chat Completions 由官方 Python SDK `openai` 的异步客户端和流接口执行，Anthropic Messages 由官方 Python SDK `anthropic` 执行。适配器只负责注入 `providerId` 解析出的配置、统一契约归一化和错误边界；跨边界请求、结果、流事件和向量对象均带 `schema_version: "v1"`。

上游响应字典只在适配器内部存在，离开适配器后使用 `ProviderOutput`、`ToolCallResult`、`StreamEvent`、`EmbeddingResult` 等冻结契约。标准 `output[].content[].text` 会归一化为正文；`refusal`、failed/incomplete 响应和流式异常均转换为固定、脱敏 Failure。工具流事件保留 `id`/`call_id`，可用于提交 `function_call_output`。

Provider 只负责选择官方 SDK、注入端点/密钥、统一契约和 SDK 异常分类。可信 actor、conversation/scope、workflow version、component ref、capabilities 和权限顺序由 Core/Workflow/Runner 按 `docs/architecture-contract-v1.md` 校验；Provider 不宣称代替这些隔离或锁定版本检查。

业务秘密只从 PostgreSQL 配置服务的 `read_provider_runtime_settings(session, provider_id)` 读取。管理员通过 `/providers` 中文管理页新增、编辑和启停多条资源；`providerId` 是稳定引用，管理 API 读取只返回遮盖/是否配置。连接资源、`ProviderSecret` 和 `ProviderModelConfig` 分表并显式关联，插件宿主只能获得按 ID 构建的 Provider，不获得 Key 或数据库连接。旧单套 `provider.*` 设置由 bootstrap 一次性迁移到 `0002_provider_resources` 后删除，空库保持空状态。YAML 不能覆盖秘密字段，启动时会把历史错误 `is_secret=false` 记录重新加密并修复属性。运行时构建入口为 `build_provider(session, provider_id)`。日志/样例使用 `redact_provider_payload()`，不得记录 API key、Authorization、token、密码或完整私有内容。

## 首期支持矩阵

| 协议/能力 | 非流式 | 流式 | 能力边界 |
| --- | --- | --- | --- |
| `openai_responses` | 官方 `openai.AsyncOpenAI.responses` 文本、图片、结构化输出、工具、Embedding | 官方 Responses stream 增量、用量、完成、失败/截断 | 原生搜索仅此协议且显式启用 |
| `openai_chat_completions` | 官方 `openai.AsyncOpenAI.chat.completions` 文本、结构化输出、工具、Embedding | 官方 Chat Completions stream 增量、工具增量、用量、完成；SDK 流结束异常会失败 | 不接受 Responses `input_image`；不支持原生搜索 |
| `anthropic_messages` | 官方 `anthropic.AsyncAnthropic.messages` 文本、工具 | 官方 Messages stream 文本/工具增量、输入/输出用量、工具完成、完成；SDK 流结束异常会失败 | 不声明 Embedding、图片、结构化输出或原生搜索 |

三类协议都由 `build_provider(session, provider_id)` 按持久化协议字段选择，禁止根据 URL 猜测或跨 Provider 回退。流事件统一为 `StreamEvent`；用量事件先于完成事件。工具增量保留 `id`/`call_id`/名称，便于 Runner 继续处理。

通用错误证据覆盖官方 SDK 的认证、限流、超时、连接、请求拒绝、失败响应和异常截断；适配器不再手写 HTTP 鉴权头或 SSE 分帧。真实端点未使用本任务凭据验证。

能力声明通过 `capability_matrix()` 区分 `adapter_supported`、配置 `enabled` 和 `endpoint_verified`。Chat Completions 与 Anthropic 不会因 `native_search_enabled` 配置错误声明原生搜索；Anthropic 的 Embedding 字段对外返回空且管理 API 不要求填写。

## 配置字段

| PostgreSQL resource field | 类型 | 秘密/说明 |
| --- | --- | --- |
| `Provider.protocol_type` | enum | `openai_responses`、`openai_chat_completions` 或 `anthropic_messages` |
| `Provider.base_url` | string | 否；禁止 URL userinfo |
| `ProviderSecret.api_key` | string | 是，Fernet 加密，错误响应不回显 |
| `ProviderModelConfig.responses_model` | string | 否；Anthropic Messages 模型字段 |
| `ProviderModelConfig.embedding_model` | string | 否；Anthropic 不要求且对外返回空 |
| `ProviderModelConfig.embedding_dimensions` | integer/null | 否；向量必须匹配 |
| `Provider.timeout_seconds` | number | 否 |
| `Provider.organization` | string | 否；OpenAI 兼容协议使用 |
| `ProviderModelConfig.native_search_enabled` | boolean | 否；仅 Responses 适配器支持 |

迁移 `0001` 的旧单套 `provider.*` 配置时固定为 `openai_responses`，保留稳定默认 `providerId`、密钥关联和管理员配置；`0002_provider_resources` 删除旧设置行。重启不会用 YAML 覆盖 PostgreSQL 值。

管理页 `/providers` 提供三类协议选择、字段按协议显示、切换提示、未配置/已配置密钥状态、留空保留、刷新和取消交互。通用设置与 SnowLuma 页面读取未完成或失败时禁用保存。

## 真实验证边界

当前返修仍未读取或使用 Agent 自身凭据，也未从评论、Git 或业务环境变量取得 Provider 密钥。官方 SDK 的本地 MockTransport 协议测试不是真实服务验收；真实验证需要管理员登录后通过配置入口保存 `provider.base_url`、`provider.api_key`、模型名及必要搜索开关，然后使用合成无敏感数据执行每类一次最小请求。