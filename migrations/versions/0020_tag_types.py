"""Tag types (data only): each saved tag gets ``page``, ``pageLabel`` and ``homeCard``.

The site's Playthroughs page was built in; it is now the ``compilation`` tag's page. A tag list saved
before types gets the fields, ``compilation`` with that page and its homepage card, every other tag
none. A never-saved list needs nothing: the site's defaults carry the same.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-18
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

PLAYTHROUGHS = {"page": "playthroughs", "pageLabel": "Playthroughs", "homeCard": True}
NONE = {"page": None, "pageLabel": None, "homeCard": False}


def upgrade() -> None:
    conn = op.get_bind()
    value = conn.execute(sa.text("SELECT value FROM site_settings WHERE key = 'tags'")).scalar()
    if not value:
        return
    tags = [t if "page" in t else {**t, **(PLAYTHROUGHS if t.get("name") == "compilation" else NONE)} for t in value]
    conn.execute(
        sa.text("UPDATE site_settings SET value = CAST(:v AS jsonb) WHERE key = 'tags'"), {"v": json.dumps(tags)}
    )


def downgrade() -> None:
    pass  # the extra keys are ignored by older code
