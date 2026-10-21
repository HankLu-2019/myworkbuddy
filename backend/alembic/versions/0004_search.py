"""search: multilingual BM25 (ICU + Jieba) on chunks

Revision ID: 0004
Revises: 0003
Design: docs/design/01-hardening.md §H10

`content` is indexed twice:
- ICU: European languages, identifiers such as SPIN-4412 or OOMKilled, partial Chinese segmentation.
- Jieba, under the alias `content_zh`: Chinese word segmentation. Jieba splits words like "driftsättningen" and
  emits whitespace as tokens, so search only queries it when the query contains Han characters, with the
  whitespace removed (app/knowledge/search.py).

pg_search 0.26 takes the key field from the first column; `key_field` is a deprecated no-op.
Rebuilding the index reads every chunk; at the expected size (< 1M chunks) that takes minutes, not hours.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

BM25 = (
    "CREATE INDEX chunks_bm25 ON chunks USING bm25 "
    "(id, (content::pdb.icu), (content::pdb.jieba('alias=content_zh')), owner_id, visibility)"
)


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_bm25")
    op.execute(BM25)
    # embed_missing() scans for chunks that still need a vector and haven't failed too often (design 01 §H10).
    op.execute("DROP INDEX IF EXISTS chunks_missing_embedding")
    op.execute("CREATE INDEX chunks_missing_embedding ON chunks (id) WHERE embedding IS NULL AND embed_attempts < 5")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_missing_embedding")
    op.execute("CREATE INDEX chunks_missing_embedding ON chunks (id) WHERE embedding IS NULL")
    op.execute("DROP INDEX IF EXISTS chunks_bm25")
    op.execute("CREATE INDEX chunks_bm25 ON chunks USING bm25 (id, content, owner_id, visibility)")
