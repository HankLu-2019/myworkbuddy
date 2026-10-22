"""documents.source_type may be 'change' (a merged job, as knowledge)

Revision ID: 0019
Revises: 0018
Design: docs/design/08-follow-through.md §F7
"""

from alembic import op
from app.enums import SourceType, check

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Widening a CHECK is safe for existing rows: every old value is still allowed.
    op.drop_constraint("ck_documents_source_type", "documents", type_="check")
    op.create_check_constraint("ck_documents_source_type", "documents", check("source_type", SourceType))
