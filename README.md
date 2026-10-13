# VireyaBot

VireyaBot 是一个以 QQ 为主要入口的 Python AI Agent 平台，计划通过 SnowLuma / OneBot v11 接入 QQ，并提供：

- OpenAI Responses 及兼容模型服务；
- 文本与图片理解；
- 可配置、可版本化的人格提示词；
- 基于 PostgreSQL 与 pgvector 的长期记忆；
- Web 搜索和来源引用；
- 受授权、安静时段和频率限制约束的主动会话；
- Web 配置、运行状态和可视化日志；
- 可视化工作流编排；
- 可安装的 Python 插件及工作流组件。

## 当前状态

项目处于需求规划与工程基线阶段，尚未发布可运行版本。开发任务和阶段目标在 Multica 的 VireyaBot 项目中维护。

## 设计原则

- QQ 协议层与 Agent 核心解耦；
- PostgreSQL 作为持久化事实来源；
- 人格、记忆、工作流和插件全部版本化并可追踪；
- 默认保护用户数据与密钥；
- 自定义插件在核心进程之外运行；
- 优先交付可验证的完整用户流程，避免提前建设无使用证据的基础设施。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。SnowLuma 及其他第三方依赖分别受其自身许可证约束。
