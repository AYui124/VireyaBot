# VireyaBot

VireyaBot 是面向外部 SnowLuma / OneBot v11 的 AI Agent 平台。本阶段交付可部署的管理框架：管理员 Web 登录、PostgreSQL 配置存储、秘密加密和 Docker Compose 健康检查。SnowLuma、QQ、模型、搜索、记忆、工作流和插件不在本阶段范围内。

## 本地启动

需要 Python 3.12、uv 和 Docker Compose。先准备本地引导配置：

```sh
cp .env.example .env
uv sync --frozen
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
# 将上一个命令输出写入 .env 的 CONFIG_MASTER_KEY，并修改 ADMIN_PASSWORD

docker compose up --build -d
docker compose ps
```

应用地址为 <http://localhost:8000>。初始管理员由 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 引导；迁移由 Compose 应用容器执行。数据库配置一旦写入，重启不会被 YAML 默认值覆盖。

## 配置边界

- `DATABASE_URL`、`CONFIG_MASTER_KEY`、初始管理员和 YAML 路径是启动引导项。
- 普通设置和秘密设置均写入 PostgreSQL；秘密使用 Fernet 加密，API 只返回遮盖值。
- `config/defaults.yaml` 只提供首次初始化的非秘密默认值。
- `GET /health/live` 是进程存活检查；`GET /health/ready` 执行数据库连接检查。
- `/api/login`、`/api/logout`、`/api/settings` 提供 JSON 管理接口，根路径提供最小 Web 配置页。

## 验证

```sh
uv sync --frozen
uv run ruff check app alembic
uv run python -m compileall -q app alembic
docker compose config
```

- Python 本地开发统一使用 `uv` 和仓库内 `.venv`，不得向系统 Python 或用户全局环境安装项目依赖。
- 项目依赖通过 `pyproject.toml` 与 `uv.lock` 锁定，命令优先使用 `uv run`。
- 集成与部署验收使用本地 Docker Compose 实际构建、启动、迁移和健康检查；仅在宿主机运行不能作为部署完成证据。
- Docker 资源必须使用项目独立名称，不能修改或删除与 VireyaBot 无关的容器、网络和数据卷。
- VireyaBot 的 Compose 不包含 SnowLuma；部署测试连接外部实例，连接地址和 Token 通过未纳入 Git 的环境配置提供。

## 开发分支流程

- `dev` 是项目集成分支，固定集成 worktree 为 `/opt/projects/VireyaBot-worktrees/dev`；手动创建的开发任务 worktree 必须位于 `/opt/projects/VireyaBot-worktrees/<任务或用途>`。
- 任务完成后必须提交全部交付内容，并在本地 `dev` worktree 完成合并与冲突处理。
- 合并后的 `dev` 推送到 `origin/dev`，随后创建或更新 `dev` → `main` Pull Request 供人工审核。
- 禁止直接推送远端 `main`，禁止强制推送 `main` 或 `dev`，未经明确授权不得合并 PR。
- 单独推送任务分支不算交付完成；以本地 `dev`、`origin/dev` 和开放 PR 一致为准。
- `/opt/projects/VireyaBot-worktrees` 是项目受控本地 worktree 的固定根目录，不在其他 `/opt` 路径创建持久 worktree。
- Multica daemon 自动创建的临时任务 worktree 属于平台运行目录，不手动移动或重命名。

## 设计原则

- QQ 协议层与 Agent 核心解耦；
- PostgreSQL 作为持久化事实来源；
- 人格、记忆、工作流和插件全部版本化并可追踪；
- 默认保护用户数据与密钥；
- 自定义插件在核心进程之外运行；
- 优先交付可验证的完整用户流程，避免提前建设无使用证据的基础设施。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。SnowLuma 及其他第三方依赖分别受其自身许可证约束。
验收环境停止时使用 `docker compose stop`，不删除 `vireyabot-postgres` 数据卷。
