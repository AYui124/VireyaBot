"""add multi-provider resources

Revision ID: 0002_provider_resources
Revises: 0001_initial
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_provider_resources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("protocol_type", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("organization", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider_id"),
    )
    op.create_index("ix_providers_provider_id", "providers", ["provider_id"], unique=False)
    op.create_index("ix_providers_enabled", "providers", ["enabled"], unique=False)
    op.create_table(
        "provider_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("provider_id"),
    )
    op.create_index("ix_provider_secrets_provider_id", "provider_secrets", ["provider_id"], unique=False)
    op.create_table(
        "provider_model_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("responses_model", sa.String(length=160), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("native_search_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("provider_id"),
    )
    op.create_index("ix_provider_model_configs_provider_id", "provider_model_configs", ["provider_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_provider_model_configs_provider_id", table_name="provider_model_configs")
    op.drop_table("provider_model_configs")
    op.drop_index("ix_provider_secrets_provider_id", table_name="provider_secrets")
    op.drop_table("provider_secrets")
    op.drop_index("ix_providers_enabled", table_name="providers")
    op.drop_index("ix_providers_provider_id", table_name="providers")
    op.drop_table("providers")
