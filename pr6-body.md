## PRIV-20 官方 SDK 与动态 Provider 配置

- OpenAI Responses/Chat 使用官方 `openai.AsyncOpenAI`；Anthropic Messages 使用官方 `anthropic.AsyncAnthropic`。
- Provider 持久化并加密保存 `default_stream`、`temperature`、`max_output_tokens`、`extra_headers`；API 对未知字段和非法 Header/温度/输出限制返回 422，响应不回显秘密。
- SDK 入参接收动态流式、温度、最大输出 Token 和附加 Header；Anthropic 输出限制不再硬编码 4096。
- Provider 页面展示协议相关字段、默认流式、Temperature、最大输出 Token 和附加 Header JSON；密钥与 Header 仅显示配置状态，留空保留。
- 新增迁移 `0003_provider_runtime_options`，保留旧库配置并为既有 Provider 设置安全默认值。
- 修复 SnowLuma 运行配置返回值、官方 SDK 工具格式转换、工具增量身份与完成事件。

## 验证与交付

- `35 passed`，Ruff、前端 typecheck/build 通过。
- 集成 dev：`a199e11`，已推送 `origin/dev`，PR #6 保留 PRIV-15/PRIV-20。
- 镜像：`vireyabot:test-priv20-dynamic-523ae50`。
- 官方 SDK MockTransport 流式/非流式、动态字段、工具、用量/完成、截断、错误和能力拒绝回归通过。
- 隔离 PostgreSQL 迁移、空库初始化、未知字段 422、保存动态配置后重启保持，以及镜像内 SDK smoke 已验证。

真实外部 Provider 凭据未使用；未合并 main、未更新长期实例、未操作外部 SnowLuma。