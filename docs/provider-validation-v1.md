# Provider 协议验证 v1

## 接口边界

`app.providers.ModelProvider` 是 Agent 的唯一模型边界；`OpenAIResponsesProvider` 负责 Responses-compatible `/responses`、`/embeddings` 和可选原生 Web Search。跨边界请求、结果、流事件和向量对象均带 `schema_version: "v1"`；成功结果保留调用方 `trace_id`。`ResponseRequest` 在构造时拒绝未知 schema 版本，调用时拒绝与方法参数冲突的 trace，拒绝路径不出站。

上游响应字典只在适配器内部存在，离开适配器后使用 `ProviderOutput`、`ToolCallResult`、`StreamEvent`、`EmbeddingResult` 等冻结契约。标准 `output[].content[].text` 会归一化为正文；`refusal`、failed/incomplete 响应和流式异常均转换为固定、脱敏 Failure。工具流事件保留 `id`/`call_id`，可用于提交 `function_call_output`。

Provider 只负责端点协议、鉴权头、超时和上游错误分类。可信 actor、conversation/scope、workflow version、component ref、capabilities 和权限顺序由 Core/Workflow/Runner 按 `docs/architecture-contract-v1.md` 校验；Provider 不宣称代替这些隔离或锁定版本检查。

业务秘密只从 PostgreSQL 配置服务的 `read_provider_runtime_settings()` 读取。管理 API 通过通用设置接口保存字段，`provider.api_key` 仅返回遮盖值；YAML 不能覆盖秘密字段，启动时会把历史错误 `is_secret=false` 记录重新加密并修复属性。Provider 运行时构建入口为 `build_provider()`。日志/样例使用 `redact_provider_payload()`，不得记录 API key、Authorization、token、密码或完整私有内容。

## 首期支持矩阵

| 能力 | 接口/配置 | 当前证据 | 结论 |
| --- | --- | --- | --- |
| 文本 Responses | `POST /responses` | 本地 HTTP 与 MockTransport 合成协议仿真 | 适配器已实现；端点仍待真实授权验证 |
| 图片输入 | Responses `input_image` 内容项 | 有效 1×1 PNG data URL（PNG 签名 `iVBOR`）本地 HTTP 仿真 | 接口已实现；媒体下载白名单仍属 Gateway 契约 |
| 标准正文 | `output[].content[].text` | 无顶层 `output_text` 的 `standard answer` 仿真 | 正文不会因缺少便利字段而丢失 |
| 流式事件 | Responses SSE `data:` | 本地 HTTP `created`/tool item/delta/`completed`/`DONE` | 只返回带 schema/trace 的事件；failed、incomplete、异常截断和网络超时转稳定 Failure |
| 结构化输出 | `ResponseRequest.response_format` → `text.format` | 合成请求断言 | 适配器已实现，不把 schema 扩散到核心 |
| 工具调用往返 | `function_call` → `function_call_output` | 本地 HTTP/MockTransport 合成请求与响应，保留 `call_id` | 适配器已实现；工具权限仍由 Runner/Workflow 控制 |
| 原生 Web Search | `provider.native_search_enabled`、`web_search_preview` | 合成请求与 `content[].annotations[].url_citation` 解析 | 可选；未启用时稳定返回 `provider.unavailable` |
| Embedding | `POST /embeddings`，可选 `dimensions` | 合成批量输入与二维向量响应 | 批量数量、连续索引、统一维度、配置维度和有限数值均 fail closed |
| 401/403 | `provider.auth_failed` | 隔离 HTTP 仿真 | 不可重试，保留 trace |
| 429 | `provider.rate_limited` | 隔离 HTTP 仿真 | 可重试，保留 trace；未压测真实服务 |
| 网络超时 | `provider.timeout` | MockTransport 实际抛出 `httpx.ReadTimeout` | 有界重试；不以 HTTP 504 冒充网络超时证据 |
| 408/504 | `provider.timeout` | 隔离 HTTP 状态仿真 | 稳定分类，但与网络超时证据分开 |
| 内容过滤/422 | `provider.content_filtered` | 仅上游 error code/type 含 content/safety/filter 时分类 | 其他 422 为 `provider.invalid_request`，不无条件等同过滤；流式 422 先读取 body 再分类 |
| 拒绝 | `provider.refused` | completed + refusal 内容仿真 | 不作为空成功返回，保留 trace 且不暴露上游理由 |

能力声明通过 `capability_matrix()` 区分 `adapter_supported`、配置 `enabled` 和 `endpoint_verified`。本地仿真不会把端点标记为已验证；当前 `endpoint_verified` 全部为 false。

## 配置字段

| PostgreSQL key | 类型 | 秘密 |
| --- | --- | --- |
| `provider.base_url` | string | 否 |
| `provider.api_key` | string | 是，Fernet 加密 |
| `provider.responses_model` | string | 否 |
| `provider.embedding_model` | string | 否 |
| `provider.embedding_dimensions` | integer/null | 否；返回向量必须匹配 |
| `provider.timeout_seconds` | number | 否 |
| `provider.organization` | string | 否 |
| `provider.native_search_enabled` | boolean | 否 |

默认值仅在 PostgreSQL 首次初始化时写入；YAML 的 `provider.api_key` 永不覆盖秘密默认值；重启不会覆盖管理员保存值。管理员配置入口是已认证的 `PUT /api/settings`，当前通用设置页面尚未提供 Provider 专用表单，不能把页面未显示误称为已提供 UI 配置。

## 真实验证边界

当前返修仍未读取或使用 Agent 自身凭据，也未从评论、Git 或业务环境变量取得 Provider 密钥。若数据库没有用户授权的 Provider 配置，真实图文、结构化输出、工具调用、原生搜索、流式和 Embedding 请求不能宣称完成；上述本地 HTTP/MockTransport 测试是协议忠实仿真证据，不是真实服务验收。真实验证需要管理员登录后通过配置入口保存 `provider.base_url`、`provider.api_key`、模型名及必要搜索开关，然后使用合成无敏感数据执行每类一次最小请求。