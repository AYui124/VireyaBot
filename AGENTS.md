# AGENTS.md

## Project

VireyaBot is a Python AI agent platform centered on QQ through SnowLuma / OneBot v11. The product includes OpenAI Responses-compatible models, image understanding, configurable personas, PostgreSQL-backed long-term memory, web search, controlled proactive conversations, a web administration console, visual workflow orchestration, and installable Python plugins.

## Working language

- Product plans, issue descriptions, and user-facing documentation: Simplified Chinese.
- Source identifiers, API fields, database identifiers, and code comments: English unless an external protocol requires otherwise.
- Do not translate protocol-defined names such as OneBot actions or OpenAI Responses fields.

## Engineering priorities

1. Correctness and user-data isolation.
2. Six-month maintainability.
3. Observable behavior and actionable failures.
4. Minimal moving parts and boring designs.
5. Performance only after behavior is correct, except avoidable I/O, allocations, and copies in hot paths.

## Product boundaries

- SnowLuma is an external OneBot v11 protocol implementation. Development and deployment use the user's already deployed, QQ-connected SnowLuma instance through connection details supplied at integration time.
- Never add SnowLuma, QQ login, QR-code login, or QQ session persistence to VireyaBot's Docker Compose stack. Do not restart, reconfigure, upgrade, or otherwise administer the external SnowLuma instance unless the user explicitly requests it.
- Prefer a maintained OneBot v11 Python adapter over reimplementing the protocol. SnowLuma-specific behavior belongs behind a narrow gateway.
- PostgreSQL is the source of truth. Use pgvector for semantic memory unless a task explicitly changes this decision.
- Runtime business configuration is database-backed. LLM keys, SnowLuma tokens, search credentials, provider endpoints, personas, workflow settings, and later feature configuration must be read through the PostgreSQL configuration service, not directly from environment variables.
- `compose.yaml` owns service topology, project-scoped networks, volumes, ports, and health checks. Committed YAML configuration contains non-secret startup defaults only.
- Environment variables are limited to bootstrap values required before PostgreSQL configuration can be read: database connection, configuration-encryption master key, initial administrator bootstrap, and optional YAML path.
- PostgreSQL values override YAML defaults after initialization. Application restart must never overwrite an administrator-saved database value from YAML.
- Persona prompts, memories, workflow versions, plugin versions, model profiles, and secrets are separate resources with explicit references.
- Private-chat memories must never leak into groups or other users.
- Proactive messages require explicit enablement, quiet hours, cooldowns, frequency limits, and a global kill switch.
- Search answers may cite only URLs returned by the search tool.
- Uploaded or remote media is untrusted. Enforce type, size, timeout, redirect, and network-address restrictions.

## Architecture rules

- Keep protocol adapters, agent orchestration, workflow execution, plugins, persistence, and web administration behind explicit interfaces.
- Workflow drafts are mutable; published workflow versions are immutable. A running execution stays pinned to the version it started with.
- Plugins are installation and permission units. A plugin may register multiple workflow components.
- Custom plugin code must not execute in the web or core agent process. Use an isolated runner with bounded resources and explicitly granted capabilities.
- Plugins do not receive raw database connections, all secrets, or unrestricted host filesystem access.
- Configuration schemas generate standard forms. Do not allow plugins to inject arbitrary frontend JavaScript in the initial product stage.
- Do not introduce Redis, Celery, Kafka, microservices, or an additional database unless a demonstrated requirement cannot be met by PostgreSQL and the application runtime.

## Development environment

- Local Python development must use `uv`; create and use the repository-local `.venv`. Never install project or development dependencies into the system Python or the user's global site-packages.
- Declare dependencies in `pyproject.toml` and commit the `uv.lock` file. Use locked, reproducible commands such as `uv sync --frozen` in verification and automation.
- Run Python commands through `uv run` unless the command executes inside a project Docker image.
- Do not use global `pip install`, `pipx inject`, Poetry global environments, Conda base, or system package mutation for project dependencies.
- Frontend dependencies must remain repository-local through the selected package manager. Never use global npm package installation for project tooling.
- Deployment and integration verification must use Docker Compose on the available local Docker daemon. Host-only startup is useful for focused development but does not prove deployability.
- Docker verification must build the project's images, start the declared services, apply database migrations, exercise health checks and the changed user path, then stop the stack without deleting unrelated volumes or containers.
- Use project-scoped Compose names, networks, volumes, ports, and environment files. Never operate on Docker resources that were not created for VireyaBot.
- Docker deployment tests cover VireyaBot and its owned dependencies only. SnowLuma remains external; use configurable WebSocket/HTTP endpoints and secrets supplied outside Git.
- The first implementation milestone is a vertical framework slice: Web login and configuration, PostgreSQL persistence, encrypted secret settings, YAML/bootstrap configuration, migrations, and a verified Docker Compose deployment. SnowLuma, LLM, workflow, and plugin features build on this foundation.

## Git delivery workflow

- `/opt/projects/VireyaBot` is the primary development repository and permanently checks out the `dev` integration branch. It is the base for all task worktrees and the only place where task branches are merged into `dev`.
- Before editing implementation files, every task must create or reuse a dedicated worktree from `dev` at `/opt/projects/VireyaBot-worktrees/<issue-or-purpose>`. Do not implement a task directly in `/opt/projects/VireyaBot`.
- `/opt/projects/VireyaBot-worktrees` is the only allowed root for project task, policy, and recovery worktrees. Do not create VireyaBot worktrees beside the repository, in another `/opt` path, or in a user home directory.
- Multica's local-directory resource intentionally uses `in_place` mode so the platform enters `/opt/projects/VireyaBot` without creating daemon-managed worktrees elsewhere. Tasks are serialized at the resource boundary and must create their own worktree under the fixed root before editing.
- A task is not complete until all intended changes are committed. Never leave the deliverable only as uncommitted work in a worktree.
- After verification, merge the task branch into `dev` in `/opt/projects/VireyaBot`. Use a non-fast-forward merge when practical so the task boundary remains visible.
- Resolve integration conflicts in `/opt/projects/VireyaBot`; do not rewrite or discard unrelated user changes.
- Push the integrated `dev` branch to `origin/dev`, then create or update a pull request from `dev` into `main`. The pull request is the review handoff and must include the relevant Multica issue key.
- NEVER push directly to `origin/main`. NEVER force-push `main` or `dev`. NEVER merge the pull request into `main` unless the user explicitly authorizes that merge.
- Do not treat pushing an individual task branch as delivery. The canonical delivered state is the committed local `dev` branch, its matching `origin/dev`, and the open `dev` → `main` pull request.
- If another task is running or `/opt/projects/VireyaBot` is not clean, coordinate and wait rather than merging over active work.

## Change discipline

- Read relevant code and existing patterns before editing.
- Make the smallest complete change that satisfies the issue.
- Do not refactor unrelated code or establish a second convention.
- Exported API or schema changes require all callers, migrations, tests, and documentation to change in the same delivery.
- Clean cutovers are preferred. Do not leave deprecated aliases, compatibility shims, dead exports, or unused configuration unless explicitly required.
- Never commit credentials, tokens, private prompts, production logs, downloaded QQ media, database dumps, or local runtime state.

## Verification

Every non-trivial change requires an actual smoke run of the changed behavior. Tests alone are insufficient.

- QQ integration: exercise a real or protocol-faithful OneBot event and outbound action.
- Web UI: open and interact with the affected surface.
- Workflow engine: execute a representative workflow and inspect per-node results.
- Plugin SDK: install and execute a real example plugin outside the core process.
- Persistence: apply migrations to an empty database and verify restart behavior.
- Security-sensitive changes: demonstrate the denied path as well as the allowed path.

Report only commands and scenarios actually exercised. Unverified behavior must be labeled as such.

## Repository documentation

- `README.md`: project purpose, current state, and supported setup.
- `AGENTS.md`: durable repository-wide contributor and agent rules.
- Architecture decisions and implementation plans should be added only when a task requires them; keep them synchronized with delivered behavior.
