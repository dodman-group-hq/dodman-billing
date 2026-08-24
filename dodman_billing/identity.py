"""
Convenience resolver for the common case: every product shares one `users`
table (id, email). BillingEngine only ever needs two lookups - email to
user_id, and user_id back to email - so wrap them once here instead of every
product hand-writing the same two queries. Column names are configurable in
case a product's users table doesn't match the default shape exactly.

If a product's identity model doesn't fit this (no shared table, a
different key type, etc.), it can just pass its own plain async functions
to BillingEngine directly - resolve_user_by_email and resolve_email_by_user_id
are ordinary callables, this class is only a shortcut for the common shape.
"""
import asyncpg


class SharedUsersResolver:
    def __init__(self, pool: asyncpg.Pool, *, table: str = "users", id_column: str = "id", email_column: str = "email"):
        self._pool = pool
        self._table = table
        self._id_col = id_column
        self._email_col = email_column

    async def user_id_by_email(self, email: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._id_col} FROM {self._table} WHERE {self._email_col} = $1",
                email.lower().strip(),
            )
        return str(row[self._id_col]) if row else None

    async def email_by_user_id(self, user_id: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._email_col} FROM {self._table} WHERE {self._id_col} = $1",
                user_id,
            )
        return row[self._email_col] if row else None
