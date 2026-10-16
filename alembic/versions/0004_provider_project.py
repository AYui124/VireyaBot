"""Add the official OpenAI project connection field.

Revision ID: 0004_provider_project
Revises: 0003_provider_runtime_options
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_provider_project"
down_revision = "0003_provider_runtime_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("project", sa.String(160), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("providers", "project")
