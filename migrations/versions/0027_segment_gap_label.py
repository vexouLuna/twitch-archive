"""vod_segments.gap_label: the name of the gap chapter before a segment (additive); gap chapters get their kind.

``gap_label`` NULL keeps the default ("Technical difficulties"). Before ``kind: "gap"`` existed the name was the
marker, so every stored restricted chapter with that name and no kind gets ``kind: "gap"`` (idempotent; the
downgrade drops the column and leaves the kind, which is harmless).

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vod_segments", sa.Column("gap_label", sa.Text))
    op.execute(
        """
        UPDATE vods SET chapters = (
            SELECT jsonb_agg(
                CASE WHEN c->>'name' = 'Technical difficulties' AND c->>'restricted' = 'true' AND NOT c ? 'kind'
                     THEN c || '{"kind": "gap"}'::jsonb ELSE c END
                ORDER BY ord)
            FROM jsonb_array_elements(chapters) WITH ORDINALITY AS t(c, ord))
        WHERE jsonb_typeof(chapters) = 'array' AND chapters @> '[{"name": "Technical difficulties"}]'::jsonb
        """
    )


def downgrade() -> None:
    op.drop_column("vod_segments", "gap_label")
