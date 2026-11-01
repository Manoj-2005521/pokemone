import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from telegram import Update


logger = logging.getLogger(__name__)

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    name TEXT,
    score INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0,
    quizzes INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS favorites(
    user_id INTEGER,
    pokemon_id INTEGER,
    PRIMARY KEY(user_id, pokemon_id)
);

CREATE TABLE IF NOT EXISTS history(
    user_id INTEGER,
    pokemon_id INTEGER,
    seen_at INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS cache(
    url TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    saved_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shinies(
    user_id INTEGER,
    pokemon_id INTEGER,
    PRIMARY KEY(user_id, pokemon_id)
);

CREATE TABLE IF NOT EXISTS dex_discoveries(
    user_id INTEGER,
    pokemon_id INTEGER,
    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, pokemon_id)
);

CREATE TABLE IF NOT EXISTS daily_discoveries(
    user_id INTEGER,
    day TEXT,
    pokemon_id INTEGER,
    PRIMARY KEY(user_id, day, pokemon_id)
);

CREATE TABLE IF NOT EXISTS teams(
    user_id INTEGER,
    pokemon_id INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, pokemon_id)
);

CREATE TABLE IF NOT EXISTS challenge_progress(
    user_id INTEGER,
    challenge_key TEXT,
    period_key TEXT,
    progress INTEGER DEFAULT 0,
    PRIMARY KEY(user_id, challenge_key, period_key)
);

CREATE INDEX IF NOT EXISTS idx_history_user_seen ON history(user_id, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(user_id);
CREATE INDEX IF NOT EXISTS idx_shiny_user ON shinies(user_id);
CREATE INDEX IF NOT EXISTS idx_dex_user ON dex_discoveries(user_id);
CREATE INDEX IF NOT EXISTS idx_team_user ON teams(user_id, added_at);
"""

SCHEMA = SCHEMA_V1

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS trainers(
    user_id INTEGER PRIMARY KEY,
    coins INTEGER DEFAULT 150,
    pokeballs INTEGER DEFAULT 10,
    greatballs INTEGER DEFAULT 3,
    ultraballs INTEGER DEFAULT 1,
    razz_berries INTEGER DEFAULT 5,
    last_daily_reward TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS caught_pokemon(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    pokemon_id INTEGER,
    nickname TEXT,
    level INTEGER DEFAULT 5,
    nature TEXT DEFAULT 'Hardy',
    iv_hp INTEGER DEFAULT 15,
    iv_atk INTEGER DEFAULT 15,
    iv_def INTEGER DEFAULT 15,
    iv_spa INTEGER DEFAULT 15,
    iv_spd INTEGER DEFAULT 15,
    iv_spe INTEGER DEFAULT 15,
    is_shiny INTEGER DEFAULT 0,
    caught_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_caught_user ON caught_pokemon(user_id, caught_at DESC);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS trainer_eggs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    species_id INTEGER,
    steps_needed INTEGER DEFAULT 20,
    steps_walked INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_listings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER,
    caught_id INTEGER,
    price INTEGER,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trade_offers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER,
    receiver_id INTEGER,
    sender_caught_id INTEGER,
    receiver_caught_id INTEGER,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS gym_badges(
    user_id INTEGER,
    gym_id INTEGER,
    defeated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, gym_id)
);

CREATE TABLE IF NOT EXISTS pvp_battles(
    battle_id TEXT PRIMARY KEY,
    p1_id INTEGER,
    p2_id INTEGER,
    p1_pid INTEGER,
    p2_pid INTEGER,
    p1_hp INTEGER,
    p1_max_hp INTEGER,
    p2_hp INTEGER,
    p2_max_hp INTEGER,
    turn INTEGER DEFAULT 1,
    status TEXT DEFAULT 'waiting',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_VERSION = 4

MIGRATIONS = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
}





async def database_version(db: aiosqlite.Connection) -> int:
    row = await (await db.execute("PRAGMA user_version")).fetchone()
    if row is None:
        raise RuntimeError("Could not read SQLite schema version")
    return int(row[0])


async def initialize_database(db: aiosqlite.Connection) -> None:
    version = await database_version(db)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )

    for target_version in range(version + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS.get(target_version)
        if migration is None:
            raise RuntimeError(f"Missing database migration {target_version}")

        await db.execute("BEGIN")
        try:
            for statement in migration.split(";"):
                if statement.strip():
                    await db.execute(statement)
            await db.execute(f"PRAGMA user_version = {target_version}")
            await db.commit()
            logger.info("Applied database migration %d", target_version)
        except Exception:
            await db.rollback()
            raise


async def backup_database(db: aiosqlite.Connection, destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    target: aiosqlite.Connection | None = None
    try:
        target = await aiosqlite.connect(temporary_path)
        await db.backup(target)
        result = await (await target.execute("PRAGMA integrity_check")).fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
        await target.close()
        target = None
        os.replace(temporary_path, destination)
        logger.info("SQLite backup written to %s", destination)
    except Exception:
        if target is not None:
            await target.close()
        if temporary_path.exists():
            temporary_path.unlink()
        logger.exception("Failed to write SQLite backup to %s", destination)
        raise


async def mirror_backup(source: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        await asyncio.to_thread(shutil.copyfile, source, temporary_path)
        async with aiosqlite.connect(temporary_path) as mirrored:
            result = await (await mirrored.execute("PRAGMA integrity_check")).fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"Mirrored backup integrity check failed: {result}")
        os.replace(temporary_path, destination)
        logger.info("SQLite backup mirrored to %s", destination)
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        logger.exception("Failed to mirror SQLite backup to %s", destination)
        raise


def configured_backup_directories(primary_directory: Path) -> list[Path]:
    directories = [primary_directory.expanduser().resolve()]
    remote_value = os.getenv("BACKUP_REMOTE_DIR", "").strip()
    if remote_value:
        remote_directory = Path(remote_value).expanduser().resolve()
        if remote_directory not in directories:
            directories.append(remote_directory)
    return directories


def backup_retention_count() -> int:
    value = os.getenv("BACKUP_RETENTION", "14")
    try:
        retention = int(value)
    except ValueError as error:
        raise ValueError("BACKUP_RETENTION must be a positive integer.") from error
    if retention < 1:
        raise ValueError("BACKUP_RETENTION must be a positive integer.")
    return retention


def prune_backups(directory: Path, keep_last: int) -> list[Path]:
    if keep_last < 1:
        raise ValueError("At least one backup must be retained.")
    if not directory.exists():
        return []

    backups = [
        path for path in directory.glob("pokedex-*.sqlite3")
        if path.is_file()
    ]
    backups.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    removed = backups[keep_last:]
    for backup in removed:
        backup.unlink()
        logger.info("Pruned old SQLite backup %s", backup)
    return removed


async def backup_to_directories(
    db: aiosqlite.Connection,
    directories: list[Path],
) -> list[Path]:
    if not directories:
        raise ValueError("At least one backup directory must be configured.")
    retention = backup_retention_count()
    filename = backup_filename()
    primary_destination = directories[0] / filename
    await backup_database(db, primary_destination)
    destinations = [primary_destination]
    for directory in directories[1:]:
        destination = directory / filename
        await mirror_backup(primary_destination, destination)
        prune_backups(directory, retention)
        destinations.append(destination)
    prune_backups(directories[0], retention)
    return destinations


def backup_filename(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    return f"pokedex-{timestamp}.sqlite3"


async def ensure_user(db: aiosqlite.Connection, update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    name = user.first_name or "Trainer"
    await db.execute("INSERT OR IGNORE INTO users(id,name) VALUES(?,?)", (user.id, name))
    await db.execute("UPDATE users SET name=? WHERE id=?", (name, user.id))
    await db.commit()


async def record_discovery(
    db: aiosqlite.Connection,
    user_id: int,
    pokemon_id: int,
    day: Optional[str] = None,
) -> bool:
    if user_id <= 0 or pokemon_id <= 0:
        return False

    discovery_day = day or datetime.now().date().isoformat()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO dex_discoveries(user_id,pokemon_id) VALUES(?,?)",
        (user_id, pokemon_id),
    )
    is_new = cursor.rowcount > 0
    await db.execute(
        "INSERT OR IGNORE INTO daily_discoveries(user_id,day,pokemon_id) VALUES(?,?,?)",
        (user_id, discovery_day, pokemon_id),
    )
    await db.commit()
    return is_new


async def set_challenge_progress(
    db: aiosqlite.Connection,
    user_id: int,
    challenge_key: str,
    period_key: str,
    progress: int,
) -> None:
    await db.execute(
        """INSERT INTO challenge_progress(user_id,challenge_key,period_key,progress)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id,challenge_key,period_key)
        DO UPDATE SET progress=excluded.progress""",
        (user_id, challenge_key, period_key, progress),
    )
    await db.commit()


async def cache_get(db: aiosqlite.Connection, url: str, ttl_sec: int) -> Any:
    row = await (await db.execute("SELECT data, saved_at FROM cache WHERE url=?", (url,))).fetchone()
    if not row:
        return None
    data_text, saved_at = row
    if int(time.time()) - int(saved_at) > ttl_sec:
        return None
    try:
        return json.loads(data_text)
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed cached JSON for %s", url.split("?", 1)[0], exc_info=True)
        return None


async def cache_set(db: aiosqlite.Connection, url: str, data: Any) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO cache(url,data,saved_at) VALUES(?,?,?)",
        (url, json.dumps(data), int(time.time())),
    )
    await db.commit()


async def cache_prune(db: aiosqlite.Connection, older_than_sec: int) -> None:
    cutoff = int(time.time()) - older_than_sec
    await db.execute("DELETE FROM cache WHERE saved_at < ?", (cutoff,))
    await db.commit()


async def get_trainer(db: aiosqlite.Connection, user_id: int) -> dict[str, Any]:
    row = await (await db.execute(
        "SELECT coins, pokeballs, greatballs, ultraballs, razz_berries, last_daily_reward FROM trainers WHERE user_id=?",
        (user_id,),
    )).fetchone()
    if row is None:
        await db.execute(
            "INSERT OR IGNORE INTO trainers(user_id, coins, pokeballs, greatballs, ultraballs, razz_berries) VALUES(?, 150, 10, 3, 1, 5)",
            (user_id,),
        )
        await db.commit()
        return {
            "coins": 150,
            "pokeballs": 10,
            "greatballs": 3,
            "ultraballs": 1,
            "razz_berries": 5,
            "last_daily_reward": "",
        }
    return {
        "coins": int(row[0]),
        "pokeballs": int(row[1]),
        "greatballs": int(row[2]),
        "ultraballs": int(row[3]),
        "razz_berries": int(row[4]),
        "last_daily_reward": str(row[5] or ""),
    }


async def update_trainer_inventory(
    db: aiosqlite.Connection,
    user_id: int,
    *,
    coins_delta: int = 0,
    pokeballs_delta: int = 0,
    greatballs_delta: int = 0,
    ultraballs_delta: int = 0,
    razz_berries_delta: int = 0,
    last_daily_reward: str | None = None,
) -> dict[str, Any]:
    trainer = await get_trainer(db, user_id)
    new_coins = max(0, trainer["coins"] + coins_delta)
    new_pb = max(0, trainer["pokeballs"] + pokeballs_delta)
    new_gb = max(0, trainer["greatballs"] + greatballs_delta)
    new_ub = max(0, trainer["ultraballs"] + ultraballs_delta)
    new_rb = max(0, trainer["razz_berries"] + razz_berries_delta)
    new_daily = last_daily_reward if last_daily_reward is not None else trainer["last_daily_reward"]

    await db.execute(
        """UPDATE trainers SET
            coins=?, pokeballs=?, greatballs=?, ultraballs=?, razz_berries=?, last_daily_reward=?
        WHERE user_id=?""",
        (new_coins, new_pb, new_gb, new_ub, new_rb, new_daily, user_id),
    )
    await db.commit()
    return {
        "coins": new_coins,
        "pokeballs": new_pb,
        "greatballs": new_gb,
        "ultraballs": new_ub,
        "razz_berries": new_rb,
        "last_daily_reward": new_daily,
    }


async def add_caught_pokemon(
    db: aiosqlite.Connection,
    user_id: int,
    pokemon_id: int,
    nickname: str,
    level: int,
    nature: str,
    ivs: tuple[int, int, int, int, int, int],
    is_shiny: bool = False,
) -> int:
    cursor = await db.execute(
        """INSERT INTO caught_pokemon(
            user_id, pokemon_id, nickname, level, nature,
            iv_hp, iv_atk, iv_def, iv_spa, iv_spd, iv_spe, is_shiny
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id, pokemon_id, nickname, level, nature,
            ivs[0], ivs[1], ivs[2], ivs[3], ivs[4], ivs[5], 1 if is_shiny else 0
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def get_caught_pokemon_page(
    db: aiosqlite.Connection,
    user_id: int,
    page: int = 1,
    per_page: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    offset = max(0, (page - 1) * per_page)
    count_row = await (await db.execute(
        "SELECT COUNT(*) FROM caught_pokemon WHERE user_id=?", (user_id,)
    )).fetchone()
    total = int(count_row[0]) if count_row else 0

    cursor = await db.execute(
        """SELECT id, pokemon_id, nickname, level, nature,
                  iv_hp, iv_atk, iv_def, iv_spa, iv_spd, iv_spe, is_shiny, caught_at
           FROM caught_pokemon
           WHERE user_id=?
           ORDER BY id DESC
           LIMIT ? OFFSET ?""",
        (user_id, per_page, offset),
    )
    rows = await cursor.fetchall()
    pokemon_list = [
        {
            "id": row[0],
            "pokemon_id": row[1],
            "nickname": row[2],
            "level": row[3],
            "nature": row[4],
            "ivs": (row[5], row[6], row[7], row[8], row[9], row[10]),
            "is_shiny": bool(row[11]),
            "caught_at": row[12],
        }
        for row in rows
    ]
    return pokemon_list, total


async def get_caught_pokemon_by_id(db: aiosqlite.Connection, caught_id: int) -> dict[str, Any] | None:
    cursor = await db.execute(
        """SELECT id, user_id, pokemon_id, nickname, level, nature,
                  iv_hp, iv_atk, iv_def, iv_spa, iv_spd, iv_spe, is_shiny, caught_at
           FROM caught_pokemon
           WHERE id=?""",
        (caught_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "pokemon_id": row[2],
        "nickname": row[3],
        "level": row[4],
        "nature": row[5],
        "ivs": (row[6], row[7], row[8], row[9], row[10], row[11]),
        "is_shiny": bool(row[12]),
        "caught_at": row[13],
    }


async def transfer_caught_pokemon(db: aiosqlite.Connection, caught_id: int, new_owner_id: int) -> bool:
    await db.execute("UPDATE market_listings SET is_active=0 WHERE caught_id=?", (caught_id,))
    cursor = await db.execute(
        "UPDATE caught_pokemon SET user_id=? WHERE id=?",
        (new_owner_id, caught_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def evolve_caught_pokemon(
    db: aiosqlite.Connection,
    caught_id: int,
    new_pid: int,
    new_nickname: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE caught_pokemon SET pokemon_id=?, nickname=?, level=level+2 WHERE id=?",
        (new_pid, new_nickname, caught_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def add_trainer_egg(
    db: aiosqlite.Connection,
    user_id: int,
    species_id: int,
    steps_needed: int = 15,
) -> int:
    cursor = await db.execute(
        "INSERT INTO trainer_eggs(user_id, species_id, steps_needed, steps_walked) VALUES(?,?,?,0)",
        (user_id, species_id, steps_needed),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def get_trainer_eggs(db: aiosqlite.Connection, user_id: int) -> list[dict[str, Any]]:
    cursor = await db.execute(
        "SELECT id, species_id, steps_needed, steps_walked, created_at FROM trainer_eggs WHERE user_id=?",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0],
            "species_id": row[1],
            "steps_needed": row[2],
            "steps_walked": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


async def walk_trainer_eggs(db: aiosqlite.Connection, user_id: int, steps: int = 1) -> list[dict[str, Any]]:
    eggs = await get_trainer_eggs(db, user_id)
    hatched = []
    for egg in eggs:
        new_walked = egg["steps_walked"] + steps
        if new_walked >= egg["steps_needed"]:
            hatched.append(egg)
            await db.execute("DELETE FROM trainer_eggs WHERE id=?", (egg["id"],))
        else:
            await db.execute(
                "UPDATE trainer_eggs SET steps_walked=? WHERE id=?",
                (new_walked, egg["id"]),
            )
    await db.commit()
    return hatched


async def create_market_listing(
    db: aiosqlite.Connection,
    seller_id: int,
    caught_id: int,
    price: int,
) -> int:
    await db.execute("UPDATE market_listings SET is_active=0 WHERE caught_id=?", (caught_id,))
    cursor = await db.execute(
        "INSERT INTO market_listings(seller_id, caught_id, price, is_active) VALUES(?,?,?,1)",
        (seller_id, caught_id, price),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def get_market_listings(db: aiosqlite.Connection, limit: int = 10) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """SELECT m.id, m.seller_id, m.caught_id, m.price,
                  p.pokemon_id, p.nickname, p.level, p.nature, p.is_shiny, p.iv_hp, p.iv_atk, p.iv_def, p.iv_spa, p.iv_spd, p.iv_spe
           FROM market_listings m
           JOIN caught_pokemon p ON m.caught_id = p.id
           WHERE m.is_active = 1
           ORDER BY m.id DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0],
            "seller_id": row[1],
            "caught_id": row[2],
            "price": row[3],
            "pokemon_id": row[4],
            "nickname": row[5],
            "level": row[6],
            "nature": row[7],
            "is_shiny": bool(row[8]),
            "ivs": (row[9], row[10], row[11], row[12], row[13], row[14]),
        }
        for row in rows
    ]


async def buy_market_listing(
    db: aiosqlite.Connection,
    buyer_id: int,
    listing_id: int,
) -> tuple[bool, str]:
    cursor = await db.execute(
        "SELECT seller_id, caught_id, price, is_active FROM market_listings WHERE id=?",
        (listing_id,),
    )
    row = await cursor.fetchone()
    if not row or row[3] == 0:
        return False, "This listing is no longer available."

    seller_id, caught_id, price, _ = row
    if seller_id == buyer_id:
        return False, "You cannot buy your own listing."

    buyer_trainer = await get_trainer(db, buyer_id)
    if buyer_trainer["coins"] < price:
        return False, f"Not enough coins! You have {buyer_trainer['coins']} (needs {price})."

    # Transfer coins
    await update_trainer_inventory(db, buyer_id, coins_delta=-price)
    await update_trainer_inventory(db, seller_id, coins_delta=price)

    # Transfer Pokémon
    await transfer_caught_pokemon(db, caught_id, buyer_id)

    # Mark listing as sold
    await db.execute("UPDATE market_listings SET is_active=0 WHERE id=?", (listing_id,))
    await db.commit()
    return True, "Success!"


async def award_gym_badge(db: aiosqlite.Connection, user_id: int, gym_id: int) -> bool:
    cursor = await db.execute(
        "INSERT OR IGNORE INTO gym_badges(user_id, gym_id) VALUES(?,?)",
        (user_id, gym_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_user_badges(db: aiosqlite.Connection, user_id: int) -> list[int]:
    cursor = await db.execute(
        "SELECT gym_id FROM gym_badges WHERE user_id=? ORDER BY gym_id ASC",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [int(row[0]) for row in rows]


async def update_caught_nickname(db: aiosqlite.Connection, user_id: int, caught_id: int, new_nickname: str) -> bool:
    cursor = await db.execute(
        "UPDATE caught_pokemon SET nickname=? WHERE id=? AND user_id=?",
        (new_nickname, caught_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_caught_pokemon(db: aiosqlite.Connection, user_id: int, caught_id: int) -> bool:
    await db.execute("UPDATE market_listings SET is_active=0 WHERE caught_id=? AND seller_id=?", (caught_id, user_id))
    cursor = await db.execute(
        "DELETE FROM caught_pokemon WHERE id=? AND user_id=?",
        (caught_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0




