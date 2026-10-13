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

验收环境停止时使用 `docker compose stop`，不删除 `vireyabot-postgres` 数据卷。
