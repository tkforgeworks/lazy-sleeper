"""Lock public.alembic_version away from Supabase's PostgREST roles (RLS on, grants revoked).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-19

Supabase exposes every table in `public` through its REST API and flags any without row-level
security. The version table has no business being reachable that way; the `postgres` role that
runs migrations owns it and bypasses RLS, so this is invisible to Alembic. Harmless on plain
Postgres (the roles may not exist — guarded).
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_ROLES = ("anon", "authenticated")


def upgrade() -> None:
    op.execute("ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY")
    for role in _ROLES:
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"REVOKE ALL ON TABLE public.alembic_version FROM {role}; END IF; END $$"
        )


def downgrade() -> None:
    op.execute("ALTER TABLE public.alembic_version DISABLE ROW LEVEL SECURITY")
