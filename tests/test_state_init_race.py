"""init_db must survive concurrent first runs and heal a half-migrated file."""

import asyncio

import aiosqlite
import pytest

from harvey.state import MIGRATIONS, StateManager


def test_concurrent_init_db_on_fresh_file(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        await asyncio.gather(*(StateManager(str(db)).init_db() for _ in range(12)))
        async with aiosqlite.connect(str(db)) as conn:
            async with conn.execute("PRAGMA user_version") as cur:
                (version,) = await cur.fetchone()
        return version

    assert asyncio.run(go()) == len(MIGRATIONS)


def test_init_db_heals_half_applied_migration(tmp_path):
    """Simulate the broken state: column exists, version not bumped."""
    db = tmp_path / "harvey.db"

    async def go():
        await StateManager(str(db)).init_db()
        async with aiosqlite.connect(str(db)) as conn:
            # Roll the version back to before email_status was added, but
            # leave the column in place — exactly what a lost race produces.
            await conn.execute("PRAGMA user_version = 3")
            await conn.commit()
        await StateManager(str(db)).init_db()  # used to raise "duplicate column name"
        async with aiosqlite.connect(str(db)) as conn:
            async with conn.execute("PRAGMA user_version") as cur:
                (version,) = await cur.fetchone()
            async with conn.execute("PRAGMA table_info(prospects)") as cur:
                cols = [r[1] for r in await cur.fetchall()]
        return version, cols

    version, cols = asyncio.run(go())
    assert version == len(MIGRATIONS)
    assert cols.count("email_status") == 1
