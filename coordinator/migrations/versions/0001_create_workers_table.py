"""create workers table

Revision ID: 0001
Revises:
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("credential_hash", sa.String(), nullable=True),
        sa.Column("registration_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("agent_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="REGISTERED"),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("workers")
