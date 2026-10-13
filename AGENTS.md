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

- SnowLuma is an external OneBot v11 protocol implementation. Do not copy or modify its source without an explicit license review.
- Prefer a maintained OneBot v11 Python adapter over reimplementing the protocol. SnowLuma-specific behavior belongs behind a narrow gateway.
- PostgreSQL is the source of truth. Use pgvector for semantic memory unless a task explicitly changes this decision.
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
