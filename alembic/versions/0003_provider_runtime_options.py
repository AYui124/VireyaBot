"""add provider runtime options

Revision ID: 0003_provider_runtime_options
Revises: 0002_provider_resources
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_provider_runtime_options"
down_revision = "0002_provider_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_secrets", sa.Column("extra_headers", sa.Text(), nullable=False, server_default=""))
    op.add_column("provider_model_configs", sa.Column("default_stream", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("provider_model_configs", sa.Column("temperature", sa.Float(), nullable=True))
    op.add_column("provider_model_configs", sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default="4096"))


def downgrade() -> None:
    op.drop_column("provider_model_configs", "max_output_tokens")
    op.drop_column("provider_model_configs", "temperature")
    op.drop_column("provider_model_configs", "default_stream")
    op.drop_column("provider_secrets", "extra_headers")
