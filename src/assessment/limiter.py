import hashlib
import time
from uuid import uuid4

import aiosqlite


class RateLimited(Exception):
    pass


class TokenLimiter:
    def __init__(self, path: str, limit: int = 50_000, clock=time.time):
        self.path, self.limit, self.clock = path, limit, clock

    async def initialize(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, timestamp REAL NOT NULL,
                tokens INTEGER NOT NULL CHECK(tokens >= 0), pending INTEGER NOT NULL)""")
            await db.execute("CREATE INDEX IF NOT EXISTS usage_tenant ON usage(tenant, timestamp)")
            await db.commit()

    async def reserve(self, api_key: str, tokens: int) -> str:
        if tokens < 1 or tokens > self.limit:
            raise RateLimited
        tenant = hashlib.sha256(api_key.encode()).hexdigest()
        reservation = str(uuid4())
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            # Requests are bounded to 13 seconds; keep crashed reservations charged.
            await db.execute(
                "UPDATE usage SET pending=0, timestamp=? WHERE pending=1 AND timestamp<=?",
                (now, now - 120),
            )
            await db.execute("DELETE FROM usage WHERE pending=0 AND timestamp<=?", (now - 60,))
            row = await (
                await db.execute(
                    "SELECT COALESCE(SUM(tokens),0) FROM usage WHERE tenant=?", (tenant,)
                )
            ).fetchone()
            if row[0] + tokens > self.limit:
                await db.commit()
                raise RateLimited
            await db.execute(
                "INSERT INTO usage VALUES (?,?,?,?,1)", (reservation, tenant, now, tokens)
            )
            await db.commit()
        return reservation

    async def settle(self, reservation: str, tokens: int):
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute(
                "UPDATE usage SET tokens=?, timestamp=?, pending=0 WHERE id=?",
                (max(0, tokens), self.clock(), reservation),
            )
            await db.commit()
