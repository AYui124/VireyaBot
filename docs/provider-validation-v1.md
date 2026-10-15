# Provider 协议验证 v1

## 接口边界

`app.providers.ModelProvider` 是 Agent 的唯一模型边界；`OpenAIResponsesProvider` 负责 Responses-compatible `/responses`、`/embeddings` 和可选原生 Web Search。Provider-specific JSON 只在适配器内解析为 `ResponseResult`、`StreamEvent`、`EmbeddingResult`；失败统一为带 `trace_id` 的 `Failure`。

业务秘密只从 PostgreSQL 配置服务的 `read_provider_runtime_settings()` 读取。管理 API 通过通用设置接口保存字段，`provider.api_key` 仅返回遮盖值；Provider 运行时构建入口为 `build_provider()`。日志/样例使用 `redact_provider_payload()`，不得记录 API key、Authorization、token、密码或完整私有内容。

## 首期支持矩阵

| 能力 | 接口/配置 | 当前证据 | 结论 |
| --- | --- | --- | --- |
| 文本 Responses | `POST /responses` | `tests/test_providers.py` 合成协议仿真 | 接口已定；真实端点待授权参数 |
| 图片输入 | Responses `input_image` 内容项 | 合成请求断言 | 接口已定；媒体下载白名单仍属 Gateway 契约 |
| 流式事件 | Responses SSE `data:` | 合成 SSE 仿真 | 已支持事件透传与 `[DONE]` |
| 结构化输出 | `ResponseRequest.response_format` → `text.format` | 合成请求断言 | 已支持，不把 schema 扩散到核心 |
| 工具调用 | `ResponseRequest.tools`、`ResponseResult.tool_calls` | 合成 function call 响应 | 已支持；工具权限仍由 Runner/Workflow 控制 |
| 原生 Web Search | `provider.native_search_enabled`、`web_search_preview` | 合成请求与 citation 解析 | 可选；未启用时返回稳定 `provider.unavailable` |
| Embedding | `POST /embeddings`，可选 `dimensions` | 合成批量输入与二维向量响应 | 已支持批量；真实模型维度由配置/端点确认 |
| 401/403 | `provider.auth_failed` | 隔离 HTTP 仿真 | 不可重试 |
| 429 | `provider.rate_limited` | 隔离 HTTP 仿真 | 可重试；未压测真实服务 |
| 超时/408/504 | `provider.timeout` | 隔离分类路径 | 有界重试 |
| 内容过滤 | 422 | 隔离分类路径 | `provider.content_filtered`，不可重试 |

## 配置字段

| PostgreSQL key | 类型 | 秘密 |
| --- | --- | --- |
| `provider.base_url` | string | 否 |
| `provider.api_key` | string | 是，Fernet 加密 |
| `provider.responses_model` | string | 否 |
| `provider.embedding_model` | string | 否 |
| `provider.embedding_dimensions` | integer/null | 否 |
| `provider.timeout_seconds` | number | 否 |
| `provider.organization` | string | 否 |
| `provider.native_search_enabled` | boolean | 否 |

默认值仅在 PostgreSQL 首次初始化时写入；重启不会覆盖管理员保存值。

## 真实验证边界

当前交付未读取或使用 Agent 自身凭据，也未从评论、Git 或环境变量取得 Provider 密钥。若数据库没有用户授权的 Provider 配置，真实图文、结构化输出、工具调用、原生搜索和 Embedding 请求不能宣称完成；上述测试是协议忠实仿真证据，不是真实服务验收。真实验证需要管理员在配置入口保存 `provider.base_url`、`provider.api_key`、模型名及必要的搜索开关，然后使用合成无敏感数据执行每类一次最小请求。