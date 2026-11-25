"""Add kind and package_tests to test_tasks

Revision ID: 7c3e1a5d9b20
Revises: 9ca5afd67819
Create Date: 2026-11-24 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '7c3e1a5d9b20'
down_revision = '9ca5afd67819'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'test_tasks',
        sa.Column(
            'kind',
            sa.TEXT(),
            nullable=False,
            server_default='package',
        ),
    )
    op.add_column(
        'test_tasks',
        sa.Column(
            'package_tests',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('test_tasks', 'package_tests')
    op.drop_column('test_tasks', 'kind')
