"""added agent_execution_permissions

Revision ID: 95d10d481cda
Revises: 3356a2f89a33
Create Date: 2023-06-13 07:59:57.183252

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '95d10d481cda'
down_revision = '3356a2f89a33'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('agent_execution_permissions',
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=True),
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('agent_execution_id', sa.Integer(), nullable=True),
                    sa.Column('agent_execution_feed_id', sa.Integer(), nullable=True),
                    sa.Column('agent_id', sa.Integer(), nullable=True),
                    sa.Column('status', sa.Boolean(), nullable=True),
                    sa.Column('tool_name', sa.String(), nullable=True),
                    sa.Column('response', sa.Text(), nullable=True),
                    sa.PrimaryKeyConstraint('id')
                    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('agent_execution_permissions')
    # ### end Alembic commands ###
