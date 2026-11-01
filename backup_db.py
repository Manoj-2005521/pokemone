import asyncio
import os
from pathlib import Path

import aiosqlite

from database import backup_to_directories, configured_backup_directories


DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().with_name("pokedex.db"))).expanduser()
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", DB_PATH.parent / "backups")).expanduser()


async def main() -> None:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    async with aiosqlite.connect(DB_PATH) as db:
        destinations = await backup_to_directories(
            db,
            configured_backup_directories(BACKUP_DIR),
        )
    for destination in destinations:
        print(f"Database backup created: {destination}")


if __name__ == "__main__":
    asyncio.run(main())
