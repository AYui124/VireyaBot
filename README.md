# VireyaBot

VireyaBot 是面向外部 SnowLuma / OneBot v11 的 AI Agent 平台。本阶段交付可部署的管理框架，以及不自动启动的 OneBot v11 接入边界：管理员 Web 登录、React + TypeScript + shadcn/ui 管理界面、PostgreSQL 配置存储、秘密加密和 Docker Compose 健康检查。Agent、模型、搜索、记忆、工作流和插件不在本阶段范围内。

## 本地启动

需要 Python 3.12、Node.js 22+、uv 和 Docker Compose。先准备本地引导配置：

```sh
cp .env.example .env
uv sync --frozen
(cd frontend && npm ci && npm run typecheck && npm run build)
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
# 将上一个命令输出写入 .env 的 CONFIG_MASTER_KEY，并修改 ADMIN_PASSWORD

docker compose up --build -d
docker compose ps
```

应用地址为 <http://localhost:8000>。初始管理员由 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 引导；迁移由 Compose 应用容器执行。数据库配置一旦写入，重启不会被 YAML 默认值覆盖。生产镜像构建时会在 Docker 多阶段构建中生成前端静态资源，不需要单独运行 Node 服务。

## 管理界面

React 单页管理控制台使用仓库内锁定的 React、TypeScript、Tailwind CSS、class-variance-authority 和 `cn` 工具构建 shadcn/ui 风格组件，提供：

- 登录、登出、刷新和未登录拒绝；
- 系统状态和数据库健康信息；
- 通用设置的加载、保存中、成功、失败和校验反馈；
- SnowLuma / OneBot v11 模式、地址和 Token 配置，Token 不回传明文；
- 桌面与窄屏布局、键盘焦点样式和清晰字段标签。

真实浏览器回归覆盖桌面/窄屏、焦点、加载、成功、失败和重复提交路径。测试只使用合成账号与 Token。

`/`、`/settings` 和 `/snowluma` 均由 FastAPI 提供同一个前端入口，API 仍由 `/api/*` 提供。React Flow 画布属于后续工作流任务，本阶段不引入无用途依赖或假画布。

## 配置边界

- `DATABASE_URL`、`CONFIG_MASTER_KEY`、初始管理员和 YAML 路径是启动引导项。
- 普通设置和秘密设置均写入 PostgreSQL；秘密使用 Fernet 加密，API 只返回遮盖值。
- `config/defaults.yaml` 只提供首次初始化的非秘密默认值。
- `GET /health/live` 是进程存活检查；`GET /health/ready` 执行数据库连接检查。
- `/api/login`、`/api/logout`、`/api/settings` 提供 JSON 管理接口；`/snowluma` 与 `/api/snowluma` 提供独立的 SnowLuma / OneBot v11 配置入口。
- SnowLuma 支持 `reverse_ws`、`forward_ws` 和 `http` 三种模式。服务端只接受与模式匹配的地址，保存不会触发连接验证；Token 保留或替换时均不回传明文。

## OneBot 接入契约

`app.integrations.onebot.SnowLumaGateway` 是外部 SnowLuma 的显式生命周期边界：配置保存和应用启动不会连接；调用方必须显式执行 `start()`，结束时执行 `stop()`。配置由 `build_snowluma_gateway(session)` 从 PostgreSQL 配置服务读取，不从环境变量读取业务参数。

- `forward_ws`：VireyaBot 使用 Bearer Token 主动连接并在断线后指数退避重连。
- `reverse_ws`：VireyaBot 监听配置地址，握手必须携带匹配的 Bearer Token；SnowLuma 可重新连接。
- `http`：只支持 OneBot action 请求，不提供事件接收或断线恢复语义。
- `next_event()` 只返回结构化 `OneBotEvent`；原始载荷停留在网关边界。`call()` 使用唯一 `echo` 关联并返回 `OneBotResponse`。
- 支持 `get_login_info`、`get_image`、`send_private_msg`、`send_group_msg` 等标准 action；消息段原样保存在事件边界内，媒体读取拒绝重定向、私网地址、非图片类型和超限响应。

协议仿真验证入口：`uv run pytest -q tests/test_onebot.py`。该测试使用本地 WebSocket 仿真，不代表真实 SnowLuma/QQ 验收；真实联调仍需用户在页面保存参数、授权并指定私聊对象和群。

### NoneBot2 兼容性结论

已在隔离环境验证 `nonebot2==2.5.0` 与 `nonebot-adapter-onebot` 可导入。该官方适配器要求由 NoneBot `ASGIMixin`/WebSocket driver 托管连接：反向入口固定为 `/onebot/v11/ws`（并要求 `X-Self-ID`），HTTP action 使用 `{api_root}/{action}`，正向 WebSocket 通过 `onebot_ws_urls` 连接。当前 FastAPI 配置页的地址是外部实例地址，且应用尚未托管 NoneBot driver 生命周期；因此本任务不把 NoneBot 运行时嵌入核心，也不冒充已采用官方适配器。当前网关复用相同的 OneBot action/echo/Auth 线格式，作为后续正式 NoneBot driver 接入前的可重复隔离验证边界。

## 设计原则

- QQ 协议层与 Agent 核心解耦；
- PostgreSQL 作为持久化事实来源；
- 人格、记忆、工作流和插件全部版本化并可追踪；
- 默认保护用户数据与密钥；
- 自定义插件在核心进程之外运行；
- 优先交付可验证的完整用户流程，避免提前建设无使用证据的基础设施。
- 插件、工作流、统一消息和数据隔离的首期契约见 [`docs/architecture-contract-v1.md`](docs/architecture-contract-v1.md)，对应 JSON Schema 为 [`schemas/contracts-v1.json`](schemas/contracts-v1.json)。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。SnowLuma 及其他第三方依赖分别受其自身许可证约束。
