import os
import logging
import math
from pathlib import Path

def load_dotenv(env_path: Path | None = None) -> None:
    path = env_path or (Path(__file__).resolve().parent / ".env")
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('\'"')
            os.environ.setdefault(key, val)
    except Exception:
        pass

load_dotenv()
import random
import hashlib
import asyncio
import difflib
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

import httpx
import aiosqlite

from telegram import (
    Update,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    MessageHandler,
    ContextTypes,
    ApplicationHandlerStop,
    TypeHandler,
    filters,
)
from api_client import API_BASE, PokeAPI
from database import (
    SCHEMA_VERSION,
    backup_to_directories,
    configured_backup_directories,
    cache_prune,
    database_version,
    ensure_user,
    initialize_database,
    record_discovery,
    set_challenge_progress,
)
from utility_helpers import (
    esc,
    parse_fx_input,
    pretty,
    quote_parts,
    weather_description,
    wiki_summary_parts,
    wiktionary_english_extract,
)
from utility_handlers import (
    _find_apod_fallback_image,
    send_apod,
    send_definition,
    send_fact,
    send_fx_conversion,
    send_quote,
    send_weather,
    send_wikipedia,
)
from inline_handlers import inline_pokemon_search
from wild_handlers import (
    wild_cmd,
    bag_cmd,
    box_cmd,
    on_wild_callback,
    on_shop_callback,
    on_box_callback,
)
from battle_handlers import (
    battle_cmd,
    on_battle_callback,
)
from economy_handlers import (
    market_cmd,
    sell_cmd,
    eggs_cmd,
    evolve_cmd,
    on_market_callback,
    on_egg_callback,
)
from wtp_handlers import (
    wtp_cmd,
    on_wtp_callback,
)
from trade_handlers import (
    trade_cmd,
    on_trade_callback,
)
from meta_handlers import (
    meta_cmd,
)
from gym_handlers import (
    gym_cmd,
    badges_cmd,
    on_gym_callback,
)
from pvp_handlers import (
    duel_cmd,
    on_pvp_callback,
)
from trainer_card_handlers import (
    trainer_cmd,
    statcard_cmd,
    claim_cmd,
    nickname_cmd,
    release_cmd,
)
from group_handlers import (
    pollquiz_cmd,
    spawn_cmd,
)
from rate_limiter import UserRateLimiter


DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent / "pokedex.db")).expanduser()
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", DB_PATH.parent / "backups")).expanduser()
MAX_POKEMON = 1025  # keep your constant; can be made dynamic if you want
QUIZ_GENERATION_ATTEMPTS = 5
ERROR_ALERT_THRESHOLD = 5
ERROR_ALERT_COOLDOWN_SEC = 15 * 60


async def suggest_pokemon_names(api: PokeAPI, query: str) -> list[str]:
    names = await api.pokemon_names()
    if not names:
        return []
    normalized_query = query.strip().casefold().replace(" ", "-")
    matches = difflib.get_close_matches(
        normalized_query,
        names,
        n=3,
        cutoff=0.45,
    )
    return [pretty(name) for name in matches]


# ----------------------------
# Config / Utilities
# ----------------------------
@dataclass(frozen=True)
class Config:
    token: str
    cache_ttl_sec: int = 60 * 60 * 24 * 14  # 14 days
    http_timeout: float = 12.0


def uid(update: Update) -> int:
    return update.effective_user.id if update.effective_user else 0


def update_message(update: Update) -> Optional[Message]:
    message = update.effective_message
    return message if isinstance(message, Message) else None


def context_user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    data = context.user_data
    return data if data is not None else {}


def today_seed_int() -> int:
    return int(hashlib.md5(str(date.today()).encode()).hexdigest(), 16)


def week_period(day: Optional[date] = None) -> str:
    current = day or date.today()
    return (current - timedelta(days=current.weekday())).isoformat()


def calculate_stat(
    base_stat: int,
    individual_value: int,
    effort_value: int,
    level: int,
    stat_name: str,
    nature_multiplier: float = 1.0,
) -> int:
    if base_stat < 1 or not 0 <= individual_value <= 31 or not 0 <= effort_value <= 252:
        raise ValueError("Base stats, IVs, or EVs are outside their valid range.")
    if not 1 <= level <= 100:
        raise ValueError("Level must be between 1 and 100.")
    if nature_multiplier not in (0.9, 1.0, 1.1):
        raise ValueError("Nature multiplier must be 0.9, 1.0, or 1.1.")

    scaled = math.floor(((2 * base_stat + individual_value + effort_value // 4) * level) / 100)
    if stat_name == "hp":
        return scaled + level + 10
    return math.floor((scaled + 5) * nature_multiplier)


def normalize_stat_name(name: str) -> str:
    normalized = name.strip().lower().replace("_", "-").replace(".", "")
    aliases = {
        "hp": "hp",
        "atk": "attack",
        "attack": "attack",
        "def": "defense",
        "defense": "defense",
        "spa": "special-attack",
        "spatk": "special-attack",
        "sp-atk": "special-attack",
        "special-attack": "special-attack",
        "spd": "special-defense",
        "spdef": "special-defense",
        "sp-def": "special-defense",
        "special-defense": "special-defense",
        "spe": "speed",
        "speed": "speed",
    }
    return aliases.get(normalized, normalized)


def dex_badges(discovered_count: int) -> list[str]:
    milestones = (
        (1, "Beginner"),
        (25, "Collector"),
        (100, "Researcher"),
        (500, "Expert"),
        (MAX_POKEMON, "Pokémon Master"),
    )
    return [name for count, name in milestones if discovered_count >= count]


def evolution_stages(chain: dict) -> list[list[str]]:
    current = [chain]
    stages = []
    while current:
        stages.append([pretty(node["species"]["name"]) for node in current])
        current = [child for node in current for child in node.get("evolves_to", [])]
    return stages


# ----------------------------
# UI Builders
# callback_data format: section|payload...
# ----------------------------
def persistent_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🌿 Catch Wild"), KeyboardButton("⚔️ Battle Arena")],
            [KeyboardButton("📦 My Box"), KeyboardButton("🎒 Bag & Shop")],
            [KeyboardButton("🏆 Gym Badges"), KeyboardButton("❓ Who's That Pokémon")],
            [KeyboardButton("🌌 Pic of the Day"), KeyboardButton("🌦️ Weather")],
            [KeyboardButton("🧰 Utilities"), KeyboardButton("📜 Menu & Help")],
        ],
        resize_keyboard=True,
    )


def welcome_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌿 Catch Wild", callback_data="menu|wild"),
            InlineKeyboardButton("⚔️ Battle Arena", callback_data="menu|battle"),
        ],
        [
            InlineKeyboardButton("📦 My Box", callback_data="menu|box"),
            InlineKeyboardButton("🎒 Bag & Shop", callback_data="menu|bag"),
        ],
        [
            InlineKeyboardButton("🏆 Gym Badges", callback_data="menu|gym"),
            InlineKeyboardButton("❓ Mystery Trivia", callback_data="menu|wtp"),
        ],
        [
            InlineKeyboardButton("🏪 Market", callback_data="menu|market"),
            InlineKeyboardButton("🤝 Trade", callback_data="menu|trade"),
        ],
        [
            InlineKeyboardButton("🌌 Pic of the Day", callback_data="utils|space"),
            InlineKeyboardButton("🌦️ Weather", callback_data="utils|weather"),
        ],
        [
            InlineKeyboardButton("🧰 All Utilities", callback_data="menu|utils"),
            InlineKeyboardButton("🎮 All Pokémon Features", callback_data="menu|pokemon"),
        ],
    ])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Pokémon", callback_data="menu|pokemon"),
            InlineKeyboardButton("🧰 Utilities", callback_data="menu|utils"),
        ],
    ])


def pokemon_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Search", callback_data="menu|search"),
            InlineKeyboardButton("🎲 Random", callback_data="menu|random"),
        ],
        [
            InlineKeyboardButton("🌿 Wild Catch", callback_data="menu|wild"),
            InlineKeyboardButton("⚔️ Battle Arena", callback_data="menu|battle"),
        ],
        [
            InlineKeyboardButton("🎒 Bag & Shop", callback_data="menu|bag"),
            InlineKeyboardButton("📦 My Box", callback_data="menu|box"),
        ],
        [
            InlineKeyboardButton("🏪 Market", callback_data="menu|market"),
            InlineKeyboardButton("🔄 Trade", callback_data="menu|trade"),
        ],
        [
            InlineKeyboardButton("🥚 Incubator", callback_data="menu|eggs"),
            InlineKeyboardButton("🏛️ Gym Badges", callback_data="menu|gym"),
        ],
        [
            InlineKeyboardButton("⚔️ PvP Duel", callback_data="menu|duel"),
            InlineKeyboardButton("❓ Mystery Trivia", callback_data="menu|wtp"),
        ],
        [
            InlineKeyboardButton("🏆 Smogon Meta", callback_data="menu|meta"),
            InlineKeyboardButton("🎮 Quiz", callback_data="menu|quiz|normal"),
        ],
        [
            InlineKeyboardButton("🌟 Daily", callback_data="menu|daily"),
            InlineKeyboardButton("📘 Pokédex", callback_data="menu|dex"),
        ],
        [
            InlineKeyboardButton("🧩 Team", callback_data="menu|team"),
            InlineKeyboardButton("❤️ Favorites", callback_data="menu|favorites"),
        ],
        [
            InlineKeyboardButton("✨ Shinies", callback_data="menu|shinies"),
            InlineKeyboardButton("👤 Profile", callback_data="menu|profile"),
        ],
        [
            InlineKeyboardButton("🏆 Ranking", callback_data="menu|ranking"),
            InlineKeyboardButton("🎯 Challenges", callback_data="menu|challenge"),
        ],
        [
            InlineKeyboardButton("🔥 Types", callback_data="menu|types"),
            InlineKeyboardButton("🍓 Berry", callback_data="menu|berry"),
        ],
        [
            InlineKeyboardButton("🧮 IV Calculator", callback_data="menu|iv"),
            InlineKeyboardButton("❓ Help", callback_data="menu|help"),
        ],
        [InlineKeyboardButton("🏠 Back to Home", callback_data="menu|home")],
    ])


def utils_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌌 Pic of the Day", callback_data="utils|space"),
            InlineKeyboardButton("🌦️ Weather", callback_data="utils|weather"),
        ],
        [
            InlineKeyboardButton("💬 Daily Quote", callback_data="utils|quote"),
            InlineKeyboardButton("💡 Random Fact", callback_data="utils|fact"),
        ],
        [
            InlineKeyboardButton("📚 Wikipedia", callback_data="utils|wiki"),
            InlineKeyboardButton("📖 Dictionary", callback_data="utils|define"),
        ],
        [
            InlineKeyboardButton("💱 FX Convert", callback_data="utils|fx"),
        ],
        [InlineKeyboardButton("🏠 Back to Main Menu", callback_data="menu|home")],
    ])


def pokemon_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖ Cancel", callback_data="menu|pokemon")],
    ])


def utility_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖ Cancel", callback_data="utils|cancel")],
    ])


async def show_callback_text(
    query: Any,
    message: Message,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


def pokemon_kb(pid: int, is_fav: bool, shiny: bool) -> InlineKeyboardMarkup:
    fav_text = "💔 Unfavorite" if is_fav else "❤️ Favorite"
    shiny_text = "✨ Shiny: ON" if shiny else "✨ Shiny: OFF"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Stats", callback_data=f"p|{pid}|stats"),
            InlineKeyboardButton("⚔️ Moves", callback_data=f"p|{pid}|moves|0"),
        ],
        [
            InlineKeyboardButton("🧬 Evolution", callback_data=f"p|{pid}|evo"),
            InlineKeyboardButton("🔥 Type Matchup", callback_data=f"p|{pid}|matchup"),
        ],
        [
            InlineKeyboardButton("📖 Species", callback_data=f"p|{pid}|species"),
            InlineKeyboardButton("📍 Locations", callback_data=f"p|{pid}|encounters"),
        ],
        [
            InlineKeyboardButton("🎮 Games", callback_data=f"p|{pid}|games"),
            InlineKeyboardButton("🔊 Cry", callback_data=f"p|{pid}|cry"),
        ],
        [
            InlineKeyboardButton("🎬 Animated Sprite", callback_data=f"p|{pid}|anim"),
            InlineKeyboardButton("🏆 Meta Build", callback_data=f"p|{pid}|meta"),
        ],
        [
            InlineKeyboardButton(fav_text, callback_data=f"p|{pid}|fav"),
            InlineKeyboardButton(shiny_text, callback_data=f"p|{pid}|shiny"),
        ],
        [
            InlineKeyboardButton("⬅️ Prev", callback_data=f"p|{max(1, pid-1)}|open"),
            InlineKeyboardButton("➡️ Next", callback_data=f"p|{min(MAX_POKEMON, pid+1)}|open"),
        ],
        [
            InlineKeyboardButton("🎲 Random", callback_data="menu|random"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu|home"),
        ],
    ])


def moves_kb(pid: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data=f"p|{pid}|moves|{max(0, page-1)}"),
            InlineKeyboardButton("➡️", callback_data=f"p|{pid}|moves|{page+1}"),
        ],
        [InlineKeyboardButton("🔙 Back to Pokémon", callback_data=f"p|{pid}|open")],
    ])


def evolution_kb(pid: int, page: int, page_count: int) -> InlineKeyboardMarkup:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"p|{pid}|evo|{page - 1}"))
    if page + 1 < page_count:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"p|{pid}|evo|{page + 1}"))
    return InlineKeyboardMarkup([buttons])


def quiz_kb(qid: str, choices: list[str], level: str) -> InlineKeyboardMarkup:
    # 2x2 grid
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(choices[0], callback_data=f"quiz|ans|{qid}|0"),
            InlineKeyboardButton(choices[1], callback_data=f"quiz|ans|{qid}|1"),
        ],
        [
            InlineKeyboardButton(choices[2], callback_data=f"quiz|ans|{qid}|2"),
            InlineKeyboardButton(choices[3], callback_data=f"quiz|ans|{qid}|3"),
        ],
        [
            InlineKeyboardButton("🎲 New Quiz", callback_data=f"menu|quiz|{level}"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu|home"),
        ]
    ])


# ----------------------------
# Content formatting
# ----------------------------
def pick_official_art(pokemon: dict, shiny: bool) -> Optional[str]:
    other = pokemon.get("sprites", {}).get("other", {})
    art = other.get("official-artwork", {})
    return art.get("front_shiny" if shiny else "front_default") or art.get("front_default")


def base_stats_text(p: dict) -> str:
    stats = {x["stat"]["name"]: x["base_stat"] for x in p["stats"]}
    total = sum(stats.values())
    return (
        f"<b>📊 {esc(pretty(p['name']))} — Base Stats</b>\n\n"
        f"❤️ HP: <code>{stats.get('hp')}</code>\n"
        f"⚔️ Attack: <code>{stats.get('attack')}</code>\n"
        f"🛡️ Defense: <code>{stats.get('defense')}</code>\n"
        f"🔥 Sp. Atk: <code>{stats.get('special-attack')}</code>\n"
        f"🛡️ Sp. Def: <code>{stats.get('special-defense')}</code>\n"
        f"⚡ Speed: <code>{stats.get('speed')}</code>\n\n"
        f"💯 Total: <code>{total}</code>"
    )


async def defensive_matchup(
    api: PokeAPI, type_names: list[str],
) -> Optional[dict[str, float]]:
    # Compute defensive multipliers for ALL types against this Pokémon (dual type supported)
    # multiplier > 1 => weakness, < 1 => resist, 0 => immune
    all_types = [
        "normal","fire","water","electric","grass","ice","fighting","poison","ground","flying",
        "psychic","bug","rock","ghost","dragon","dark","steel","fairy"
    ]
    mult = {t: 1.0 for t in all_types}

    for tn in type_names:
        tdata = await api.type(tn)
        if not tdata:
            return None
        rel = tdata["damage_relations"]

        for x in rel["double_damage_from"]:
            mult[x["name"]] *= 2.0
        for x in rel["half_damage_from"]:
            mult[x["name"]] *= 0.5
        for x in rel["no_damage_from"]:
            mult[x["name"]] *= 0.0

    return mult


def matchup_buckets(mult: dict[str, float]) -> dict[str, list[str]]:
    categories = {
        "4x weak": [],
        "2x weak": [],
        "0.5x resist": [],
        "0.25x resist": [],
        "immune": [],
    }
    for type_name, multiplier in mult.items():
        if multiplier == 4.0:
            categories["4x weak"].append(pretty(type_name))
        elif multiplier == 2.0:
            categories["2x weak"].append(pretty(type_name))
        elif multiplier == 0.5:
            categories["0.5x resist"].append(pretty(type_name))
        elif multiplier == 0.25:
            categories["0.25x resist"].append(pretty(type_name))
        elif multiplier == 0.0:
            categories["immune"].append(pretty(type_name))
    return categories


def matchup_text(type_names: list[str], mult: dict[str, float]) -> str:
    labels = {
        "4x weak": "💥 4x weak",
        "2x weak": "⚠️ 2x weak",
        "0.5x resist": "🛡️ 0.5x resist",
        "0.25x resist": "🧱 0.25x resist",
        "immune": "🚫 Immune",
    }
    buckets = matchup_buckets(mult)
    lines = [f"{labels[key]}: {esc(', '.join(values) or 'None')}" for key, values in buckets.items()]
    return (
        f"<b>🔥 Defensive Type Matchup</b>\n"
        f"Types: <code>{esc(' / '.join(pretty(t) for t in type_names))}</code>\n\n"
        + "\n".join(lines)
    )


async def pokemon_caption(api: PokeAPI, p: dict) -> str:
    pid = p["id"]
    species = await api.species(pid)

    tag = ""
    flavor = ""
    if species:
        if species.get("is_legendary"):
            tag = " 👑 <b>Legendary</b>"
        elif species.get("is_mythical"):
            tag = " 🌟 <b>Mythical</b>"

        # pick one English flavor text
        for ft in species.get("flavor_text_entries", []):
            if ft["language"]["name"] == "en":
                flavor = ft["flavor_text"].replace("\n", " ").replace("\f", " ")[:300].rstrip()
                break

    height_m = p["height"] / 10
    weight_kg = p["weight"] / 10
    types = " / ".join(pretty(x["type"]["name"]) for x in p["types"])
    abilities = ", ".join(pretty(x["ability"]["name"]) for x in p["abilities"])

    text = (
        f"<b>⚡ {esc(pretty(p['name']))} #{pid}</b>{tag}\n\n"
        f"<b>🔥 Type</b>\n<code>{esc(types)}</code>\n\n"
        f"<b>📏 Height</b> <code>{height_m:.1f} m</code>\n"
        f"<b>⚖️ Weight</b> <code>{weight_kg:.1f} kg</code>\n\n"
        f"<b>✨ Abilities</b>\n<code>{esc(abilities)}</code>"
    )

    if flavor:
        text += f"\n\n<b>📌 Pokédex Entry</b>\n<em>{esc(flavor)}</em>"

    return text


async def is_favorite(db: aiosqlite.Connection, user_id: int, pid: int) -> bool:
    row = await (await db.execute(
        "SELECT 1 FROM favorites WHERE user_id=? AND pokemon_id=?",
        (user_id, pid)
    )).fetchone()
    return bool(row)


# ----------------------------
# Core: Send / Edit Pokémon Card
# ----------------------------
async def open_pokemon(
    update_or_query_message,
    context: ContextTypes.DEFAULT_TYPE,
    name_or_id: str | int,
    *,
    edit: bool = False,
    shiny: bool = False,
) -> None:
    api: PokeAPI = context.application.bot_data["api"]
    db: aiosqlite.Connection = context.application.bot_data["db"]

    lookup_key: str | int = name_or_id
    if isinstance(name_or_id, str) and not name_or_id.strip().isdigit():
        lookup_key = "-".join(name_or_id.strip().casefold().split())
    p = await api.pokemon(lookup_key)
    if not p:
        query = str(name_or_id)
        if query.isdigit():
            await update_or_query_message.reply_text(
                "❌ Pokémon could not be loaded. Check the Pokédex number and try again."
            )
            return
        suggestions = await suggest_pokemon_names(api, query)
        normalized_query = "-".join(query.strip().casefold().split())
        if any(
            "-".join(name.casefold().split()) == normalized_query
            for name in suggestions
        ):
            await update_or_query_message.reply_text(
                "❌ That Pokémon could not be loaded right now. Please try again shortly."
            )
            return
        if suggestions:
            await update_or_query_message.reply_text(
                "❌ Pokémon not found. Did you mean: "
                + ", ".join(f"<code>{esc(name)}</code>" for name in suggestions)
                + "?\nUse /pokemon <name> to search again.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update_or_query_message.reply_text(
                "❌ Could not find or load that Pokémon. Check the spelling and try again."
            )
        return

    pid = p["id"]
    data = context_user_data(context)
    u = data.get("_uid", 0)

    # History write (best-effort)
    if u:
        await db.execute("INSERT INTO history(user_id,pokemon_id) VALUES(?,?)", (u, pid))
        await db.commit()
        await record_discovery(db, u, pid)

    fav = await is_favorite(db, u, pid) if u else False
    data["last_pid"] = pid
    data["shiny_on"] = bool(shiny)

    image = pick_official_art(p, shiny=shiny)
    if image is None:
        await update_or_query_message.reply_text("❌ Artwork is unavailable for this Pokémon.")
        return

    cap = await pokemon_caption(api, p)

    kb = pokemon_kb(pid, is_fav=fav, shiny=shiny)

    # If we can edit media, do it (better UX)
    if edit:
        try:
            await update_or_query_message.edit_message_media(
                media=InputMediaPhoto(media=image, caption=cap, parse_mode=ParseMode.HTML)
            )
            await update_or_query_message.edit_message_reply_markup(reply_markup=kb)
            return
        except Exception:
            pass

    await update_or_query_message.reply_photo(
        photo=image,
        caption=cap,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# ----------------------------
# Commands
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    chat = update.effective_chat
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    text = (
        "✨ <b>Welcome to Pokédex Go!</b> 🎮\n\n"
        "Embark on your Pokémon journey! You don't have to remember complex commands — "
        "just tap any button below to start playing immediately:\n\n"
        "• 🌿 <b>Catch Wild:</b> Encounter wild Pokémon and throw Pokéballs\n"
        "• ⚔️ <b>Battle Arena:</b> Turn-based arena fights with real-time health bars\n"
        "• 🏆 <b>Gym Badges:</b> Defeat all 8 Kanto Gym Leaders to earn badges\n"
        "• ❓ <b>Mystery Trivia:</b> Play Who's That Pokémon silhouette trivia\n"
        "• 📦 <b>Box & Bag:</b> View your collection, items, and PokéMart\n"
        "• 🏪 <b>Market & Trade:</b> Buy, sell, or trade Pokémon with trainers\n\n"
        "<i>Tap a button below to begin:</i>"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=welcome_menu_kb())
    if chat and chat.type == "private":
        await message.reply_text(
            "👇 <b>Quick Buttons:</b> Use the persistent buttons at the bottom of your screen anytime!",
            parse_mode=ParseMode.HTML,
            reply_markup=persistent_reply_kb(),
        )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    chat = update.effective_chat
    if message is None:
        return
    text = (
        "🎮 <b>Pokédex Go — Quick Menu</b>\n\n"
        "Tap any button below to jump into the action:"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=welcome_menu_kb())
    if chat and chat.type == "private":
        await message.reply_text(
            "👇 <i>Quick navigation buttons:</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=persistent_reply_kb(),
        )


async def help_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    await message.reply_text(
        "Commands:\n"
        "⚡ /pokemon <name|id> (with 🔊 audio cry)\n"
        "🌿 /wild or /catch (Safari catch minigame)\n"
        "⚔️ /battle (Turn-based PvE arena)\n"
        "🎒 /bag (PokéMart & item inventory)\n"
        "📦 /box [page] (Caught Pokémon collection)\n"
        "🏪 /market & /sell <box_id> <price> (Player trade market)\n"
        "🔄 /trade <box_id> (Offer Pokémon for peer trade)\n"
        "🥚 /eggs or /incubator (Breeding & hatching)\n"
        "🧬 /evolve <box_id> (Evolution chamber)\n"
        "❓ /wtp (Who's That Pokémon? mystery quiz)\n"
        "🏆 /meta or /smogon <pokemon> (Competitive builds)\n"
        "🏛️ /gym or /badges (Kanto 8-Gym Leader Campaign)\n"
        "⚔️ /duel (Real-time 2-player PvP duel)\n"
        "💳 /trainer or /card (Official Trainer Card profile)\n"
        "📊 /statcard <pokemon> (Radar stat visualizer)\n"
        "🎁 /claim (Daily login rewards & streak bonuses)\n"
        "🏷️ /nickname <box_id> <name> & /release <box_id>\n"
        "📊 /pollquiz or /poll (Interactive group quiz polls)\n"
        "🌿 /spawn (Force wild spawn in group)\n"
        "🔍 @BotName <pokemon> (Global inline search anywhere)\n"
        "/compare <a> <b>\n"
        "/type <type>\n"
        "/quiz [easy|normal|hard]\n"
        "/move /ability /where /nature /stat /berry\n"
        "/iv <pokemon> <stat> <iv> <ev> <level> [nature]\n"
        "/team add|list|rate /dex /challenge\n"
        "/dex missing [page] /dex discovered [page]\n"
        "/health (configured admin only)\n"
        "/wiki /define /quote /fact /space /fx /weather\n"
        "/profile /ranking /favorites /shinies /history /daily\n"
        "/item <name>\n"
        "/generation <n>\n",
    )


async def pokemon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    args = context.args or []
    if not args:
        await message.reply_text("Use: /pokemon pikachu   or   /pokemon 25")
        return
    await open_pokemon(message, context, " ".join(args).strip().lower(), shiny=False)


async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    pid = random.randint(1, MAX_POKEMON)
    await open_pokemon(message, context, pid, shiny=False)


async def type_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    args = context.args or []
    if not args:
        await message.reply_text("Example: /type fire")
        return
    api: PokeAPI = context.application.bot_data["api"]
    t = args[0].lower()
    data = await api.type(t)
    if not data:
        await message.reply_text("❌ Type not found.")
        return

    d = data["damage_relations"]
    strong = ", ".join(pretty(x["name"]) for x in d["double_damage_to"]) or "None"
    weak = ", ".join(pretty(x["name"]) for x in d["double_damage_from"]) or "None"
    resist = ", ".join(pretty(x["name"]) for x in d["half_damage_from"]) or "None"
    immune = ", ".join(pretty(x["name"]) for x in d["no_damage_from"]) or "None"

    await message.reply_text(
        f"🔥 {pretty(t).upper()} TYPE\n\n"
        f"💥 Strong against: {strong}\n"
        f"🛡️ Weak against: {weak}\n"
        f"🧱 Resists: {resist}\n"
        f"🚫 Immune to: {immune}\n"
    )


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    args = context.args or []
    if len(args) < 2:
        await message.reply_text("Use: /compare pikachu charizard")
        return
    api: PokeAPI = context.application.bot_data["api"]
    a = await api.pokemon(args[0].lower())
    b = await api.pokemon(args[1].lower())
    if not a or not b:
        await message.reply_text("❌ One or both Pokémon not found.")
        return

    sa = {x["stat"]["name"]: x["base_stat"] for x in a["stats"]}
    sb = {x["stat"]["name"]: x["base_stat"] for x in b["stats"]}

    text = (
        "⚔️ BATTLE COMPARISON\n\n"
        f"{pretty(a['name']):<14} vs {pretty(b['name'])}\n\n"
        f"HP:      {sa['hp']} vs {sb['hp']}\n"
        f"Attack:  {sa['attack']} vs {sb['attack']}\n"
        f"Defense: {sa['defense']} vs {sb['defense']}\n"
        f"Sp.Atk:  {sa['special-attack']} vs {sb['special-attack']}\n"
        f"Sp.Def:  {sa['special-defense']} vs {sb['special-defense']}\n"
        f"Speed:   {sa['speed']} vs {sb['speed']}\n\n"
        f"Total:   {sum(sa.values())} vs {sum(sb.values())}\n"
    )
    await message.reply_text(text)


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    row = await (await db.execute(
        "SELECT name,score,streak,quizzes,correct FROM users WHERE id=?",
        (uid(update),)
    )).fetchone()
    if not row:
        return
    name, score, streak, quizzes, correct = row
    acc = round((correct * 100 / quizzes), 1) if quizzes else 0

    await message.reply_text(
        f"👤 TRAINER CARD\n\n"
        f"Name: {name}\n"
        f"Score: {score}\n"
        f"Streak: {streak}\n"
        f"Quizzes: {quizzes}\n"
        f"Accuracy: {acc}%\n"
    )


async def ranking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    rows = await (await db.execute(
        "SELECT name,score FROM users ORDER BY score DESC LIMIT 10"
    )).fetchall()

    if not rows:
        await message.reply_text("No trainers yet.")
        return

    text = "🏆 LOCAL TRAINER RANKING\n\n"
    for i, (name, score) in enumerate(rows, 1):
        text += f"{i}. {name} — ⭐ {score}\n"
    await message.reply_text(text)


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    rows = await (await db.execute(
        "SELECT pokemon_id FROM favorites WHERE user_id=? ORDER BY pokemon_id LIMIT 80",
        (uid(update),)
    )).fetchall()

    if not rows:
        await message.reply_text("❤️ No favorites yet. Open a Pokémon and tap Favorite.")
        return

    lines = []
    for (pid_,) in rows:
        p = await api.pokemon(pid_)
        if p:
            lines.append(f"#{pid_} {pretty(p['name'])}")
    await message.reply_text("❤️ YOUR FAVORITES\n\n" + "\n".join(lines))


async def shinies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    rows = await (await db.execute(
        "SELECT pokemon_id FROM shinies WHERE user_id=? ORDER BY pokemon_id LIMIT 120",
        (uid(update),)
    )).fetchall()

    if not rows:
        await message.reply_text("✨ Your shiny collection is empty. Toggle Shiny on a Pokémon to collect it.")
        return

    lines = []
    for (pid_,) in rows:
        p = await api.pokemon(pid_)
        if p:
            lines.append(f"#{pid_} {pretty(p['name'])}")
    await message.reply_text("✨ SHINY COLLECTION\n\n" + "\n".join(lines))


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    rows = await (await db.execute(
        "SELECT pokemon_id FROM history WHERE user_id=? ORDER BY seen_at DESC LIMIT 30",
        (uid(update),)
    )).fetchall()

    if not rows:
        await message.reply_text("📜 Your history is empty.")
        return

    seen = set()
    lines = []
    for (pid_,) in rows:
        if pid_ in seen:
            continue
        seen.add(pid_)
        p = await api.pokemon(pid_)
        if p:
            lines.append(f"#{pid_} {pretty(p['name'])}")

    await message.reply_text("📜 RECENT\n\n" + "\n".join(lines[:20]))


async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    rr = random.Random(today_seed_int())
    pid_ = rr.randint(1, MAX_POKEMON)
    await open_pokemon(message, context, pid_, shiny=False)


async def item_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    args = context.args or []
    if not args:
        await message.reply_text("Use: /item master-ball")
        return
    api: PokeAPI = context.application.bot_data["api"]

    name = "-".join(args).lower()
    data = await api.item(name)
    if not data:
        await message.reply_text("❌ Item not found.")
        return

    effect = "No description available."
    for x in data.get("effect_entries", []):
        if x["language"]["name"] == "en":
            effect = x["effect"]
            break
    effect = effect[:700].rstrip()

    img = (data.get("sprites") or {}).get("default")
    text = (
        f"<b>🎒 {esc(pretty(data['name']))}</b>\n\n"
        f"💰 Cost: <code>{data.get('cost')}</code>\n\n"
        f"<b>📖 Effect</b>\n{esc(effect)}"
    )

    if img:
        await message.reply_photo(photo=img, caption=text, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML)


async def move_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /move flamethrower")
        return

    api: PokeAPI = context.application.bot_data["api"]
    name = "-".join(args).lower()
    move = await api.move(name)
    if not move:
        await message.reply_text("❌ Move not found.")
        return

    effect = next(
        (entry.get("effect") or entry.get("short_effect", "")
         for entry in move.get("effect_entries", [])
         if entry.get("language", {}).get("name") == "en"),
        "No English effect text available.",
    )
    learned = move.get("learned_by_pokemon", [])[:3]
    pokemon_records = await asyncio.gather(
        *(api.pokemon(entry["name"]) for entry in learned)
    )
    method_lines = []
    for entry, pokemon in zip(learned, pokemon_records):
        if not pokemon:
            continue
        move_record = next(
            (record for record in pokemon.get("moves", [])
             if record["move"]["name"] == move["name"]),
            None,
        )
        if not move_record:
            continue
        methods = sorted({
            f"{pretty(detail['move_learn_method']['name'])}"
            + (f" (Lv. {detail['level_learned_at']})" if detail.get("level_learned_at") else "")
            for detail in move_record.get("version_group_details", [])
        })
        if methods:
            method_lines.append(f"{pretty(entry['name'])}: {', '.join(methods[:3])}")

    await message.reply_text(
        f"⚔️ {pretty(move['name'])}\n\n"
        f"Type: {pretty(move['type']['name'])}\n"
        f"Class: {pretty(move['damage_class']['name'])}\n"
        f"Power: {move.get('power') or '—'}  Accuracy: {move.get('accuracy') or '—'}  PP: {move.get('pp') or '—'}\n\n"
        f"Effect: {effect}\n\n"
        "Sample learn methods:\n" + ("\n".join(method_lines) or "No method data available.")
    )


async def ability_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /ability levitate")
        return

    api: PokeAPI = context.application.bot_data["api"]
    ability = await api.ability("-".join(args).lower())
    if not ability:
        await message.reply_text("❌ Ability not found.")
        return

    effect = next(
        (entry.get("effect") or entry.get("short_effect", "")
         for entry in ability.get("effect_entries", [])
         if entry.get("language", {}).get("name") == "en"),
        "No English effect text available.",
    )
    pokemon_names = [
        pretty(entry["pokemon"]["name"])
        for entry in ability.get("pokemon", [])[:20]
    ]
    await message.reply_text(
        f"✨ {pretty(ability['name'])}\n\n{effect}\n\n"
        "Pokémon with this ability:\n"
        + (", ".join(pokemon_names) or "No Pokémon listed.")
    )


async def where_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /where pikachu")
        return

    api: PokeAPI = context.application.bot_data["api"]
    pokemon = await api.pokemon("-".join(args).lower())
    if not pokemon:
        await message.reply_text("❌ Pokémon not found.")
        return
    encounters = await api.encounters(pokemon["id"])
    areas = [pretty(entry["location_area"]["name"]) for entry in (encounters or [])]
    await message.reply_text(
        f"📍 Where to find {pretty(pokemon['name'])}\n\n"
        + ("\n".join(f"• {area}" for area in areas[:30]) or "No encounter locations are listed.")
    )


async def nature_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /nature jolly or /nature speed")
        return

    api: PokeAPI = context.application.bot_data["api"]
    name = "-".join(args).lower()
    nature = await api.nature(name)
    if nature:
        increased = pretty(nature["increased_stat"]["name"]) if nature.get("increased_stat") else "None"
        decreased = pretty(nature["decreased_stat"]["name"]) if nature.get("decreased_stat") else "None"
        await message.reply_text(
            f"🌿 {pretty(nature['name'])} Nature\n\n"
            f"Raises: {increased}\nLowers: {decreased}\n"
            "Nature changes battle stats; EV yield is set by a Pokémon's base stats."
        )
        return

    stat = await api.stat(name)
    if not stat:
        await message.reply_text("❌ Nature or stat not found. Try /nature speed or /nature jolly")
        return
    affecting = stat.get("affecting_natures", {})
    raised_by = [pretty(item["name"]) for item in affecting.get("increase", [])]
    lowered_by = [pretty(item["name"]) for item in affecting.get("decrease", [])]
    await message.reply_text(
        f"🌿 Natures for {pretty(stat['name'])}\n\n"
        f"Raises this stat: {', '.join(raised_by) or 'None'}\n"
        f"Lowers this stat: {', '.join(lowered_by) or 'None'}\n\n"
        f"EV yield: {stat.get('effort', 0)} from the corresponding Pokémon stat."
    )


async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /stat speed")
        return

    api: PokeAPI = context.application.bot_data["api"]
    stat = await api.stat("-".join(args).lower())
    if not stat:
        await message.reply_text("❌ Stat not found. Try hp, attack, defense, special-attack, special-defense, or speed.")
        return

    affecting = stat.get("affecting_natures", {})
    raised_by = [pretty(item["name"]) for item in affecting.get("increase", [])]
    lowered_by = [pretty(item["name"]) for item in affecting.get("decrease", [])]
    await message.reply_text(
        f"📈 {pretty(stat['name'])}\n\n"
        f"EV yield: {stat.get('effort', 0)}\n"
        f"Natures that raise it: {', '.join(raised_by) or 'None'}\n"
        f"Natures that lower it: {', '.join(lowered_by) or 'None'}"
    )


async def send_berry(message: Message, api: PokeAPI, args: Sequence[str]) -> None:
    args = [arg.lower() for arg in args]
    if not args:
        await message.reply_text("Use: /berry oran, /berry random, /berry flavor spicy, or /berry firmness very-soft")
        return

    if args[0] in ("flavor", "firmness"):
        if len(args) < 2:
            await message.reply_text(f"Use: /berry {args[0]} <name>")
            return
        slug = "-".join(args[1:])
        data = await (api.berry_flavor(slug) if args[0] == "flavor" else api.berry_firmness(slug))
        if not data:
            await message.reply_text("❌ Berry flavor or firmness not found.")
            return
        berries = data.get("berries", [])[:25]
        names = [
            pretty(entry.get("berry", entry).get("name", ""))
            + (f" ({entry['potency']})" if entry.get("potency") is not None else "")
            for entry in berries
        ]
        await message.reply_text(
            f"🍓 {pretty(data['name'])} {args[0]}\n\n"
            + ("\n".join(f"• {name}" for name in names) or "No berries listed.")
        )
        return

    name = args[0]
    if name == "random":
        listing = await api.berries()
        results = listing.get("results", []) if listing else []
        if not results:
            await message.reply_text("❌ Berry list is unavailable right now.")
            return
        name = random.choice(results)["name"]

    berry = await api.berry(name)
    if not berry:
        await message.reply_text("❌ Berry not found.")
        return
    flavors = [
        f"{pretty(item['flavor']['name'])} {item['potency']}"
        for item in berry.get("flavors", [])
        if item.get("potency", 0) > 0
    ]
    await message.reply_text(
        f"🍓 {pretty(berry['name'])} Berry\n\n"
        f"Firmness: {pretty(berry['firmness']['name'])}\n"
        f"Size: {berry.get('size')} mm  Smoothness: {berry.get('smoothness')}\n"
        f"Growth time: {berry.get('growth_time')} hours\n"
        f"Flavors: {', '.join(flavors) or 'None'}"
    )


async def berry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is not None:
        await send_berry(message, context.application.bot_data["api"], context.args or [])


async def send_iv_estimate(message: Message, api: PokeAPI, args: Sequence[str]) -> None:
    if len(args) not in (5, 6):
        await message.reply_text(
            "Use: /iv <pokemon> <stat> <iv> <ev> <level> [nature]\n"
            "Example: /iv pikachu speed 31 252 50 jolly"
        )
        return

    stat_name = normalize_stat_name(args[1])
    if stat_name not in {"hp", "attack", "defense", "special-attack", "special-defense", "speed"}:
        await message.reply_text("❌ Use hp, attack, defense, special-attack, special-defense, or speed.")
        return
    try:
        individual_value = int(args[2])
        effort_value = int(args[3])
        level = int(args[4])
    except ValueError:
        await message.reply_text("❌ IV, EV, and level must be whole numbers.")
        return

    pokemon = await api.pokemon(args[0].lower())
    if not pokemon:
        await message.reply_text("❌ Pokémon not found.")
        return
    base_stat = next(
        (entry["base_stat"] for entry in pokemon["stats"] if entry["stat"]["name"] == stat_name),
        None,
    )
    if base_stat is None:
        await message.reply_text("❌ That Pokémon does not have the requested stat.")
        return

    nature_name = "Neutral"
    nature_multiplier = 1.0
    if len(args) == 6:
        nature = await api.nature(args[5].lower())
        if not nature:
            await message.reply_text("❌ Nature not found.")
            return
        nature_name = pretty(nature["name"])
        if nature.get("increased_stat", {}).get("name") == stat_name:
            nature_multiplier = 1.1
        elif nature.get("decreased_stat", {}).get("name") == stat_name:
            nature_multiplier = 0.9

    try:
        result = calculate_stat(
            base_stat,
            individual_value,
            effort_value,
            level,
            stat_name,
            nature_multiplier,
        )
    except ValueError as error:
        await message.reply_text(f"❌ {error}")
        return

    await message.reply_text(
        f"🧮 <b>{esc(pretty(pokemon['name']))} {esc(pretty(stat_name))} estimate</b>\n\n"
        f"Level: {level}  Base: {base_stat}\n"
        f"IV: {individual_value}/31  EV: {effort_value}/252\n"
        f"Nature: {esc(nature_name)} ({nature_multiplier:.1f}x)\n\n"
        f"Estimated stat: <b>{result}</b>\n"
        "This estimates a configured build; it cannot reveal a Pokémon instance's hidden IVs.",
        parse_mode=ParseMode.HTML,
    )


async def generation_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is None:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Use: /generation <1-9> or /generation <region>\n"
            "Example: /generation 1 or /generation kanto"
        )
        return

    api: PokeAPI = context.application.bot_data["api"]
    raw_query = args[0].strip().lower()

    region_to_gen = {
        "kanto": "1",
        "johto": "2",
        "hoenn": "3",
        "sinnoh": "4",
        "unova": "5",
        "kalos": "6",
        "alola": "7",
        "galar": "8",
        "paldea": "9",
    }
    gen_query = region_to_gen.get(raw_query, raw_query)
    data = await api.generation(gen_query)
    if not data:
        await message.reply_text("❌ Generation not found. Try a generation number from 1 to 9 (e.g. /generation 1).")
        return

    gen_name = pretty(data.get("name", "Generation"))
    region = pretty(data.get("main_region", {}).get("name", "Unknown"))
    species_list = data.get("pokemon_species", [])
    species_count = len(species_list)
    moves_count = len(data.get("moves", []))
    types_introduced = [pretty(t["name"]) for t in data.get("types", [])]
    version_groups = [pretty(vg["name"]) for vg in data.get("version_groups", [])]

    sample_species = [pretty(s["name"]) for s in species_list[:8]]
    sample_text = ", ".join(sample_species)
    if species_count > 8:
        sample_text += f", ... (+{species_count - 8} more)"

    text = (
        f"<b>🌍 {esc(gen_name.upper())}</b>\n\n"
        f"<b>🗺️ Main Region:</b> <code>{esc(region)}</code>\n"
        f"<b>👾 Pokémon Introduced:</b> <code>{species_count}</code>\n"
        f"<b>⚔️ Moves Introduced:</b> <code>{moves_count}</code>\n"
        f"<b>🔥 New Types:</b> <code>{esc(', '.join(types_introduced) or 'None')}</code>\n"
        f"<b>🎮 Core Games:</b> <code>{esc(', '.join(version_groups) or 'None')}</code>\n\n"
        f"<b>Sample Pokémon:</b>\n<i>{esc(sample_text or 'None')}</i>"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def iv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message is not None:
        await send_iv_estimate(message, context.application.bot_data["api"], context.args or [])


async def wiki_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /wiki <topic>\nExample: /wiki black holes")
        return
    await send_wikipedia(message, context.application.bot_data["api"], " ".join(args).strip())


async def define_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /define <word>\nExample: /define serendipity")
        return
    await send_definition(message, context.application.bot_data["api"], " ".join(args).strip())


async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message:
        await send_quote(message, context.application.bot_data["api"])


async def fact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message:
        await send_fact(message, context.application.bot_data["api"])


async def space_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message:
        await send_apod(message, context.application.bot_data["api"])


async def fx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    if message:
        await send_fx_conversion(message, context.application.bot_data["api"], context.args or [])


async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    args = context.args or []
    if message is None:
        return
    if not args:
        await message.reply_text("Use: /weather <city>\nExample: /weather Chennai")
        return
    await send_weather(message, context.application.bot_data["api"], " ".join(args).strip())


async def dex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    user_id = uid(update)
    if message is None:
        return
    if user_id <= 0:
        await message.reply_text("Open the Pokédex in a private chat to track your discoveries.")
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    args = context.args or []
    if args:
        mode = args[0].casefold()
        if mode not in ("missing", "discovered"):
            await message.reply_text(
                "Use /dex for progress, /dex missing [page], or /dex discovered [page]."
            )
            return
        try:
            page = int(args[1]) if len(args) > 1 else 1
        except ValueError:
            await message.reply_text("Page must be a positive number.")
            return
        if page < 1:
            await message.reply_text("Page must be a positive number.")
            return

        offset = (page - 1) * 20
        comparison = "NOT " if mode == "missing" else ""
        count_row = await (await db.execute(
            f"""SELECT COUNT(*) FROM dex_discoveries
            WHERE user_id=? AND pokemon_id BETWEEN 1 AND ?""",
            (user_id, MAX_POKEMON),
        )).fetchone()
        discovered_count = min(int(count_row[0]) if count_row else 0, MAX_POKEMON)
        total = MAX_POKEMON - discovered_count if mode == "missing" else discovered_count
        if offset >= total and total:
            await message.reply_text(f"Page {page} is out of range. There are {total} entries.")
            return
        rows = await (await db.execute(
            f"""WITH RECURSIVE pokemon_ids(id) AS (
                SELECT 1 UNION ALL SELECT id + 1 FROM pokemon_ids WHERE id < ?
            )
            SELECT pokemon_ids.id FROM pokemon_ids
            WHERE pokemon_ids.id {comparison}IN (
                SELECT pokemon_id FROM dex_discoveries
                WHERE user_id=? AND pokemon_id BETWEEN 1 AND ?
            )
            ORDER BY pokemon_ids.id
            LIMIT 20 OFFSET ?""",
            (MAX_POKEMON, user_id, MAX_POKEMON, offset),
        )).fetchall()
        if not rows:
            await message.reply_text(
                "🎉 Your Pokédex is complete!" if mode == "missing"
                else "No discoveries yet. Open Pokémon to start filling your Pokédex."
            )
            return

        pokemon_records = await asyncio.gather(*(api.pokemon(row[0]) for row in rows))
        if any(pokemon is None for pokemon in pokemon_records):
            await message.reply_text("❌ Pokémon names are unavailable right now. Please try again.")
            return
        title = "Missing Pokémon" if mode == "missing" else "Discovered Pokémon"
        lines = [
            f"#{pokemon['id']} {pretty(pokemon['name'])}"
            for pokemon in pokemon_records if pokemon is not None
        ]
        pages = max(1, (total + 19) // 20)
        await message.reply_text(
            f"📘 {title} — page {page}/{pages} ({total} total)\n\n"
            + "\n".join(lines)
            + f"\n\nNext: /dex {mode} {page + 1}" if page < pages
            else f"📘 {title} — page {page}/{pages} ({total} total)\n\n"
            + "\n".join(lines)
        )
        return

    row = await (await db.execute(
        "SELECT COUNT(*) FROM dex_discoveries WHERE user_id=?",
        (user_id,),
    )).fetchone()
    discovered = row[0] if row else 0
    percentage = discovered * 100 / MAX_POKEMON
    badges = dex_badges(discovered)
    today_row = await (await db.execute(
        "SELECT COUNT(*) FROM daily_discoveries WHERE user_id=? AND day=?",
        (user_id, date.today().isoformat()),
    )).fetchone()
    today_count = today_row[0] if today_row else 0
    await message.reply_text(
        f"📘 Pokédex progress\n\n"
        f"Discovered: {discovered}/{MAX_POKEMON} ({percentage:.1f}%)\n"
        f"Today: {today_count}/3 new discoveries\n\n"
        f"Badges: {', '.join(badges) or 'None yet'}\n\n"
        "Browse: /dex missing or /dex discovered"
    )


async def team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    user_id = uid(update)
    args = context.args or []
    if message is None:
        return
    if user_id <= 0:
        await message.reply_text("Teams are available in private chats.")
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = user_id
    action = args[0].lower() if args else "list"

    if action == "add":
        if len(args) < 2:
            await message.reply_text("Use: /team add pikachu")
            return
        pokemon = await api.pokemon("-".join(args[1:]).lower())
        if not pokemon:
            await message.reply_text("❌ Pokémon not found.")
            return
        rows = list(await (await db.execute(
            "SELECT pokemon_id FROM teams WHERE user_id=? ORDER BY added_at",
            (user_id,),
        )).fetchall())
        if any(row[0] == pokemon["id"] for row in rows):
            await message.reply_text(f"{pretty(pokemon['name'])} is already on your team.")
            return
        if len(rows) >= 6:
            await message.reply_text("Your team is full. Remove a Pokémon before adding another.")
            return
        await db.execute(
            "INSERT INTO teams(user_id,pokemon_id) VALUES(?,?)",
            (user_id, pokemon["id"]),
        )
        await db.commit()
        await message.reply_text(f"✅ Added {pretty(pokemon['name'])} to your team ({len(rows) + 1}/6).")
        return

    if action == "remove":
        if len(args) < 2:
            await message.reply_text("Use: /team remove pikachu")
            return
        pokemon = await api.pokemon("-".join(args[1:]).lower())
        if not pokemon:
            await message.reply_text("❌ Pokémon not found.")
            return
        cursor = await db.execute(
            "DELETE FROM teams WHERE user_id=? AND pokemon_id=?",
            (user_id, pokemon["id"]),
        )
        await db.commit()
        await message.reply_text(
            f"Removed {pretty(pokemon['name'])} from your team."
            if cursor.rowcount
            else f"{pretty(pokemon['name'])} was not on your team."
        )
        return

    rows = await (await db.execute(
        "SELECT pokemon_id FROM teams WHERE user_id=? ORDER BY added_at LIMIT 6",
        (user_id,),
    )).fetchall()
    if not rows:
        await message.reply_text("Your team is empty. Add one with /team add pikachu.")
        return

    pokemon_records = await asyncio.gather(*(api.pokemon(row[0]) for row in rows))
    if any(pokemon is None for pokemon in pokemon_records):
        await message.reply_text("❌ Could not load your complete team from PokéAPI right now.")
        return
    team = [pokemon for pokemon in pokemon_records if pokemon is not None]
    if action == "list":
        lines = [
            f"{i}. {pretty(pokemon['name'])}"
            for i, pokemon in enumerate(team, 1)
        ]
        await message.reply_text("🧩 Your team\n\n" + "\n".join(lines))
        return
    if action != "rate":
        await message.reply_text("Use: /team add, /team list, /team rate, or /team remove")
        return

    if not team:
        await message.reply_text("Could not load your team from PokéAPI right now.")
        return
    profiles = await asyncio.gather(*(
        defensive_matchup(api, [entry["type"]["name"] for entry in pokemon["types"]])
        for pokemon in team
    ))
    if any(profile is None for profile in profiles):
        await message.reply_text("❌ Type matchup data is unavailable right now. Please try again.")
        return
    weak_counts: dict[str, int] = {}
    quad_counts: dict[str, int] = {}
    resist_counts: dict[str, int] = {}
    immunity_counts: dict[str, int] = {}
    all_types = set()
    total_stats = 0
    for pokemon, profile in zip(team, profiles):
        if profile is None:
            continue
        all_types.update(entry["type"]["name"] for entry in pokemon["types"])
        total_stats += sum(stat["base_stat"] for stat in pokemon["stats"])
        for type_name, multiplier in profile.items():
            if multiplier >= 2:
                weak_counts[type_name] = weak_counts.get(type_name, 0) + 1
            if multiplier >= 4:
                quad_counts[type_name] = quad_counts.get(type_name, 0) + 1
            if 0 < multiplier < 1:
                resist_counts[type_name] = resist_counts.get(type_name, 0) + 1
            if multiplier == 0:
                immunity_counts[type_name] = immunity_counts.get(type_name, 0) + 1

    average_stats = total_stats / len(team)
    score = round(average_stats / 7 + len(all_types) * 1.5
                  - sum(weak_counts.values()) * 1.5
                  - sum(quad_counts.values()) * 2)
    score = max(0, min(100, score))
    rating = "Excellent" if score >= 80 else "Balanced" if score >= 65 else "Needs coverage"
    shared_weaknesses = sorted(
        weak_counts.items(),
        key=lambda pair: (-pair[1], pair[0]),
    )
    weakness_text = ", ".join(f"{pretty(name)} ({count})" for name, count in shared_weaknesses[:6]) or "None"
    resistances = sorted(resist_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    immunities = sorted(immunity_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    resistance_text = ", ".join(
        f"{pretty(name)} ({count})" for name, count in resistances[:6]
    ) or "None"
    immunity_text = ", ".join(
        f"{pretty(name)} ({count})" for name, count in immunities[:6]
    ) or "None"
    await message.reply_text(
        f"🧩 Team review: {rating} ({score}/100)\n\n"
        f"Average base-stat total: {average_stats:.0f}\n"
        f"Types represented: {', '.join(pretty(name) for name in sorted(all_types))}\n"
        f"Weaknesses (Pokémon affected): {weakness_text}\n"
        f"Resistances: {resistance_text}\n"
        f"Immunities: {immunity_text}\n\n"
        "Defensive counts show how many team members are affected; move coverage is not evaluated."
    )


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    user = update.effective_user
    if message is None or user is None:
        return
    admin_id: int | None = context.application.bot_data.get("admin_user_id")
    if admin_id is None or user.id != admin_id or message.chat.type != "private":
        await message.reply_text("This command is available only to the configured bot admin.")
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    statuses = []
    try:
        check = await (await db.execute("PRAGMA quick_check")).fetchone()
        if check is None or check[0] != "ok":
            logging.getLogger(__name__).error("SQLite quick_check returned %r", check)
            statuses.append("SQLite: ERROR")
        else:
            statuses.append("SQLite: OK")
    except aiosqlite.Error:
        logging.getLogger(__name__).exception("SQLite health check failed")
        statuses.append("SQLite: ERROR")

    try:
        result = await api.get(f"{API_BASE}/pokemon/1", cache_response=False)
        statuses.append("PokéAPI: OK" if result else "PokéAPI: UNAVAILABLE")
    except (httpx.HTTPError, aiosqlite.Error, ValueError):
        logging.getLogger(__name__).exception("PokéAPI health check failed")
        statuses.append("PokéAPI: ERROR")

    error_count = context.application.bot_data.get("unhandled_error_count", 0)
    statuses.append(f"Unhandled errors since startup: {error_count}")
    await message.reply_text("🩺 Bot health\n\n" + "\n".join(statuses))


def configured_admin_user_id() -> int | None:
    value = os.getenv("BOT_ADMIN_ID", "").strip()
    if not value:
        return None
    try:
        admin_id = int(value)
    except ValueError as error:
        raise ValueError("BOT_ADMIN_ID must be a positive Telegram user ID.") from error
    if admin_id <= 0:
        raise ValueError("BOT_ADMIN_ID must be a positive Telegram user ID.")
    return admin_id


async def challenge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    user_id = uid(update)
    if message is None:
        return
    if user_id <= 0:
        await message.reply_text("Challenges are available in private chats.")
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    today = date.today().isoformat()
    today_row = await (await db.execute(
        "SELECT COUNT(*) FROM daily_discoveries WHERE user_id=? AND day=?",
        (user_id, today),
    )).fetchone()
    week_row = await (await db.execute(
        "SELECT progress FROM challenge_progress WHERE user_id=? AND challenge_key=? AND period_key=?",
        (user_id, "weekly_quiz_streak", week_period()),
    )).fetchone()
    daily_progress = today_row[0] if today_row else 0
    weekly_progress = week_row[0] if week_row else 0
    await message.reply_text(
        "🎯 Challenges\n\n"
        f"Daily: Discover 3 Pokémon ({min(daily_progress, 3)}/3)\n"
        f"Weekly: Get 5 quiz answers right in a row ({min(weekly_progress, 5)}/5)"
    )


# ----------------------------
# Quiz (per-user state; no globals)
# ----------------------------
def quiz_points(level: str, streak: int) -> int:
    base = {"easy": 5, "normal": 10, "hard": 20}.get(level, 10)
    return base + min(streak, 10)


async def new_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int, level: str, *, edit_query=None) -> None:
    api: PokeAPI = context.application.bot_data["api"]

    if level == "easy":
        lo, hi = 1, 151
    elif level == "hard":
        lo, hi = 500, MAX_POKEMON
    else:
        lo, hi = 1, MAX_POKEMON

    question = None
    for _ in range(QUIZ_GENERATION_ATTEMPTS):
        pid_ = random.randint(lo, hi)
        pokemon = await api.pokemon(pid_)
        if not pokemon:
            continue

        choices_by_id: dict[int, Optional[dict]] = {pid_: pokemon}
        while len(choices_by_id) < 4:
            choices_by_id.setdefault(random.randint(lo, hi), None)
        missing_ids = [pokemon_id for pokemon_id, value in choices_by_id.items() if value is None]
        fetched = await asyncio.gather(*(api.pokemon(pokemon_id) for pokemon_id in missing_ids))
        for pokemon_id, fetched_pokemon in zip(missing_ids, fetched):
            choices_by_id[pokemon_id] = fetched_pokemon

        choices = [pretty(entry["name"]) for entry in choices_by_id.values() if entry]
        image = pick_official_art(pokemon, shiny=False)
        if len(choices) == 4 and image:
            random.shuffle(choices)
            question = pokemon, choices, image
            break

    if question is None:
        unavailable = "Quiz data is temporarily unavailable. Please try again shortly."
        if edit_query:
            try:
                await edit_query.edit_message_text(unavailable)
                return
            except Exception:
                try:
                    await edit_query.edit_message_caption(caption=unavailable)
                    return
                except Exception:
                    pass
        await context.application.bot.send_message(chat_id=chat_id, text=unavailable)
        return

    pokemon, choices, image = question
    pid_ = pokemon["id"]
    correct = pretty(pokemon["name"])
    qid = hashlib.md5(f"{chat_id}-{pid_}-{random.random()}".encode()).hexdigest()[:10]
    context_user_data(context).setdefault("quizzes", {})[qid] = {
        "answer": correct,
        "choices": choices,
        "level": level,
        "done": False,
    }

    caption = (
        "<b>🎮 WHO'S THAT POKÉMON?</b>\n\n"
        f"Difficulty: <code>{esc(level.upper())}</code>\n"
        "Choose the correct answer:"
    )

    if edit_query:
        try:
            await edit_query.edit_message_media(
                InputMediaPhoto(media=image, caption=caption, parse_mode=ParseMode.HTML)
            )
            await edit_query.edit_message_reply_markup(reply_markup=quiz_kb(qid, choices, level))
            return
        except Exception:
            pass

    await context.application.bot.send_photo(
        chat_id=chat_id,
        photo=image,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=quiz_kb(qid, choices, level),
    )


async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    context_user_data(context)["_uid"] = uid(update)

    args = context.args or []
    level = args[0].lower() if args else "normal"
    if level not in ("easy", "normal", "hard"):
        level = "normal"
    await new_quiz(context, chat.id, level)


# ----------------------------
# Callback Router
# ----------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]

    q = update.callback_query
    if not q:
        return
    message = q.message
    if not isinstance(message, Message):
        return

    user_data = context_user_data(context)
    parts = (q.data or "").split("|")
    is_quiz_answer = len(parts) >= 4 and parts[0] == "quiz" and parts[1] == "ans"
    if is_quiz_answer and not user_data.get("quizzes", {}).get(parts[2]):
        await q.answer("This quiz expired. Start another with /quiz.", show_alert=True)
        return
    if parts and parts[0] not in ("w", "shop", "box", "b", "mkt", "egg", "gym", "pvp", "trade", "wtp"):
        await q.answer()

    # Ensure user tracked
    if update.effective_user:
        user_data["_uid"] = update.effective_user.id
        await ensure_user(db, update)

    if not parts:
        return

    # WILD / CATCH callbacks
    if parts[0] == "w":
        if len(parts) > 1 and parts[1] == "hunt_again":
            await q.answer()
            await wild_cmd(update, context)
            return
        await on_wild_callback(update, context)
        return

    # SHOP / BAG callbacks
    if parts[0] == "shop":
        if len(parts) > 1 and parts[1] == "bag":
            await q.answer()
            await bag_cmd(update, context)
            return
        await on_shop_callback(update, context)
        return

    # BOX callbacks
    if parts[0] == "box":
        if len(parts) > 1 and parts[1] == "open":
            await q.answer()
            await box_cmd(update, context)
            return
        await on_box_callback(update, context)
        return

    # BATTLE callbacks
    if parts[0] == "b":
        if len(parts) > 1 and parts[1] == "again":
            await q.answer()
            await battle_cmd(update, context)
            return
        await on_battle_callback(update, context)
        return

    # MARKET callbacks
    if parts[0] == "mkt":
        await on_market_callback(update, context)
        return

    # TRADE callbacks
    if parts[0] == "trade":
        await on_trade_callback(update, context)
        return

    # EGG INCUBATOR callbacks
    if parts[0] == "egg":
        await on_egg_callback(update, context)
        return

    # GYM callbacks
    if parts[0] == "gym":
        await on_gym_callback(update, context)
        return

    # PVP callbacks
    if parts[0] == "pvp":
        if len(parts) > 1 and parts[1] == "new_prompt":
            await duel_cmd(update, context)
            return
        await on_pvp_callback(update, context)
        return

    # WTP callbacks
    if parts[0] == "wtp":
        if len(parts) > 1 and parts[1] == "new":
            await wtp_cmd(update, context)
            return
        await on_wtp_callback(update, context)
        return

    # MENU callbacks
    if parts[0] == "menu":
        action = parts[1] if len(parts) > 1 else "home"
        user_data.pop("utility_mode", None)
        user_data.pop("utility_prompt", None)

        if action == "home":
            await show_callback_text(
                q,
                message,
                "🎮 <b>Pokédex Go — Main Menu</b>\nChoose a feature below:",
                parse_mode=ParseMode.HTML,
                reply_markup=welcome_menu_kb(),
            )
            return

        if action == "pokemon":
            await show_callback_text(
                q,
                message,
                "<b>⚡ Pokémon</b>\nChoose a feature:",
                parse_mode=ParseMode.HTML,
                reply_markup=pokemon_menu_kb(),
            )
            return

        if action == "wild":
            await wild_cmd(update, context)
            return

        if action == "battle":
            await battle_cmd(update, context)
            return

        if action == "bag":
            await bag_cmd(update, context)
            return

        if action == "box":
            await box_cmd(update, context)
            return

        if action == "market":
            await market_cmd(update, context)
            return

        if action == "trade":
            await trade_cmd(update, context)
            return

        if action == "eggs":
            await eggs_cmd(update, context)
            return

        if action == "wtp":
            await wtp_cmd(update, context)
            return

        if action == "meta":
            await message.reply_text("🏆 Use <code>/meta &lt;pokemon&gt;</code> or <code>/smogon &lt;pokemon&gt;</code> for competitive builds!", parse_mode=ParseMode.HTML)
            return

        if action == "gym":
            await gym_cmd(update, context)
            return

        if action == "duel":
            await duel_cmd(update, context)
            return



        if action == "search":
            await message.reply_text("🔍 Use: /pokemon pikachu   or type just: pikachu")
            return


        if action == "random":
            pid_ = random.randint(1, MAX_POKEMON)
            await open_pokemon(message, context, pid_, shiny=False)
            return

        if action == "types":
            await message.reply_text("🔥 Try: /type fire  /type water  /type dragon  /type fairy")
            return

        if action == "help":
            await message.reply_text("Use /help for all commands.")
            return

        if action == "utils":
            await show_callback_text(
                q,
                message,
                "<b>🧰 Utilities</b>\nChoose a tool or tap a prompt and type your query:",
                parse_mode=ParseMode.HTML,
                reply_markup=utils_menu_kb(),
            )
            return

        if action == "quiz":
            level = parts[2] if len(parts) > 2 else "normal"
            await new_quiz(context, message.chat_id, level, edit_query=q)
            return

        if action == "profile":
            # reuse command logic quickly
            fake_update = update
            await profile_cmd(fake_update, context)
            return

        if action == "ranking":
            await ranking_cmd(update, context)
            return

        if action == "favorites":
            await favorites_cmd(update, context)
            return

        if action == "shinies":
            await shinies_cmd(update, context)
            return

        if action == "daily":
            await daily_cmd(update, context)
            return

        if action == "dex":
            await dex_cmd(update, context)
            return

        if action == "team":
            await team_cmd(update, context)
            return

        if action == "challenge":
            await challenge_cmd(update, context)
            return

        if action == "berry":
            await message.reply_text("🍓 Try /berry oran, /berry random, /berry flavor spicy, or /berry firmness very-soft")
            return

        if action == "iv":
            prompt_text = "🧮 Type: pokemon stat iv ev level [nature]\nExample: pikachu speed 31 252 50 jolly."
            user_data["utility_mode"] = "iv"
            try:
                await q.edit_message_text(prompt_text, reply_markup=pokemon_cancel_kb())
                prompt_message_id = message.message_id
            except Exception:
                prompt = await message.reply_text(prompt_text, reply_markup=pokemon_cancel_kb())
                prompt_message_id = prompt.message_id
            user_data["utility_prompt"] = (message.chat_id, prompt_message_id)
            return

        return

    if parts[0] == "utils":
        action = parts[1] if len(parts) > 1 else ""
        user_data.pop("utility_mode", None)
        user_data.pop("utility_prompt", None)
        if action == "cancel":
            await show_callback_text(
                q,
                message,
                "<b>🧰 Utilities</b>\nChoose a tool or tap a prompt and type your query:",
                parse_mode=ParseMode.HTML,
                reply_markup=utils_menu_kb(),
            )
            return
        prompts = {
            "wiki": "📚 Type a Wikipedia topic, or use /wiki <topic>.",
            "define": "📖 Type a word to define, or use /define <word>.",
            "fx": "💱 Try: 100 USD to EUR, 100 US dollars to euros, or $100 to €. "
                "Use /fx currencies for supported names and codes.",
            "weather": "🌦️ Type a city, or use /weather Chennai.",
        }
        if action in prompts:
            user_data["utility_mode"] = action
            try:
                await q.edit_message_text(prompts[action], reply_markup=utility_cancel_kb())
                prompt_message_id = message.message_id
            except Exception:
                prompt = await message.reply_text(prompts[action], reply_markup=utility_cancel_kb())
                prompt_message_id = prompt.message_id
            user_data["utility_prompt"] = (message.chat_id, prompt_message_id)
        elif action == "quote":
            await send_quote(message, api)
        elif action == "fact":
            await send_fact(message, api)
        elif action == "space":
            await send_apod(message, api)
        return

    # QUIZ callbacks
    if parts[0] == "quiz" and len(parts) >= 4 and parts[1] == "ans":
        qid = parts[2]
        idx = int(parts[3])

        qstate = user_data.get("quizzes", {}).get(qid)
        if not qstate:
            await message.reply_text("This quiz expired. Start another with /quiz.")
            return
        if qstate["done"]:
            return
        qstate["done"] = True

        selected = qstate["choices"][idx]
        correct = qstate["answer"]
        level = qstate["level"]

        # scoring
        row = await (await db.execute(
            "SELECT score,streak,quizzes,correct FROM users WHERE id=?",
            (uid(update),)
        )).fetchone()
        if row is None:
            return
        score, streak, quizzes, correct_n = row

        quizzes += 1
        is_correct = selected.lower() == correct.lower()
        if is_correct:
            streak += 1
            pts = quiz_points(level, streak)
            score += pts
            correct_n += 1
            result = f"✅ Correct! <b>{esc(correct)}</b>\n⭐ +{pts} points\n🔥 Streak: {streak}\n🏆 Score: {score}"
        else:
            streak = 0
            result = f"❌ Wrong.\n✅ Correct: <b>{esc(correct)}</b>\n🔥 Streak reset.\n🏆 Score: {score}"

        weekly_progress_row = await (await db.execute(
            "SELECT progress FROM challenge_progress WHERE user_id=? AND challenge_key=? AND period_key=?",
            (uid(update), "weekly_quiz_streak", week_period()),
        )).fetchone()
        weekly_progress = weekly_progress_row[0] if weekly_progress_row else 0
        await set_challenge_progress(
            db,
            uid(update),
            "weekly_quiz_streak",
            week_period(),
            weekly_progress + 1 if is_correct else 0,
        )

        await db.execute(
            "UPDATE users SET score=?,streak=?,quizzes=?,correct=? WHERE id=?",
            (score, streak, quizzes, correct_n, uid(update))
        )
        await db.commit()

        try:
            await q.edit_message_caption(
                caption=result,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➡️ Next Quiz", callback_data=f"menu|quiz|{level}")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu|home")],
                ])
            )
        except Exception:
            await message.reply_text(result, parse_mode=ParseMode.HTML)
        return

    # POKEMON callbacks
    if parts[0] == "p" and len(parts) >= 3:
        pid_ = int(parts[1])
        action = parts[2]
        u = uid(update)

        if action == "encounters":
            pokemon = await api.pokemon(pid_)
            encounters = await api.encounters(pid_)
            if not pokemon:
                await message.reply_text("❌ Pokémon not found.")
                return
            areas = [
                pretty(entry["location_area"]["name"])
                for entry in (encounters or [])
            ]
            await message.reply_text(
                f"📍 Where to find {pretty(pokemon['name'])}\n\n"
                + ("\n".join(f"• {area}" for area in areas[:30]) or "No encounter locations are listed.")
            )
            return

        if action == "evo":
            species = await api.species(pid_)
            if not species:
                await message.reply_text("❌ Evolution data is unavailable.")
                return
            chain = await api.get(species["evolution_chain"]["url"])
            if not chain:
                await message.reply_text("❌ Evolution data is unavailable.")
                return
            stages = evolution_stages(chain["chain"])
            page_count = len(stages)
            page = int(parts[3]) if len(parts) > 3 else 0
            page = max(0, min(page, page_count - 1))
            stage_text = " → ".join(stages[page])
            text = (
                f"🧬 Evolution of {pretty(species['name'])}\n\n"
                f"Stage {page + 1}/{page_count}\n{stage_text}"
            )
            keyboard = evolution_kb(pid_, page, page_count) if page_count > 1 else None
            if len(parts) > 3:
                await q.edit_message_text(text, reply_markup=keyboard)
            else:
                await message.reply_text(text, reply_markup=keyboard)
            return

        if action == "open":
            shiny_on = bool(user_data.get("shiny_on", False))
            await open_pokemon(q, context, pid_, edit=True, shiny=shiny_on)
            return

        if action == "cry":
            p = await api.pokemon(pid_)
            if not p:
                await message.reply_text("❌ Pokémon not found.")
                return
            cry_url = await api.cry_url(pid_)
            if cry_url:
                try:
                    await context.bot.send_voice(
                        chat_id=message.chat_id,
                        voice=cry_url,
                        caption=f"🔊 <b>Cry of {esc(pretty(p['name']))}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    await message.reply_text(f"🔊 Listen to {pretty(p['name'])}'s cry: {cry_url}")
            else:
                await message.reply_text("❌ Audio cry is unavailable for this Pokémon.")
            return

        if action == "anim":
            p = await api.pokemon(pid_)
            if not p:
                return
            p_name = pretty(p["name"])
            shiny_on = bool(user_data.get("shiny_on", False))
            anim_url = (
                f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/showdown/shiny/{pid_}.gif"
                if shiny_on
                else f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/showdown/{pid_}.gif"
            )
            shiny_badge = " ✨ (Shiny)" if shiny_on else ""
            caption = f"🎬 <b>Animated Showdown Sprite: #{pid_:03d} {esc(p_name)}</b>{shiny_badge}"
            try:
                await context.bot.send_animation(
                    chat_id=message.chat_id,
                    animation=anim_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await message.reply_text(f"🎬 View animation: {anim_url}")
            return

        if action == "meta":
            context.args = [str(pid_)]
            await meta_cmd(update, context)
            return

        if action == "stats":
            p = await api.pokemon(pid_)
            if p:
                await message.reply_text(base_stats_text(p), parse_mode=ParseMode.HTML)
            return

        if action == "moves":
            page = int(parts[3]) if len(parts) > 3 else 0
            p = await api.pokemon(pid_)
            if not p:
                return
            moves = [pretty(x["move"]["name"]) for x in p.get("moves", [])]
            per_page = 20
            max_page = max(0, math.ceil(len(moves) / per_page) - 1)
            page = max(0, min(page, max_page))

            chunk = moves[page * per_page : (page + 1) * per_page]
            text = f"⚔️ {pretty(p['name']).upper()} — MOVES\nPage {page+1}/{max_page+1}\n\n"
            text += "\n".join(f"{i+1 + page*per_page}. {m}" for i, m in enumerate(chunk)) or "No moves."
            await message.reply_text(text, reply_markup=moves_kb(pid_, page))
            return

        if action == "matchup":
            p = await api.pokemon(pid_)
            if not p:
                return
            type_names = [x["type"]["name"] for x in p["types"]]
            mult = await defensive_matchup(api, type_names)
            if mult is None:
                await message.reply_text("❌ Type matchup data is unavailable right now. Please try again.")
                return
            await message.reply_text(matchup_text(type_names, mult), parse_mode=ParseMode.HTML)
            return

        if action == "species":
            s = await api.species(pid_)
            if not s:
                return
            habitat = pretty(s["habitat"]["name"]) if s.get("habitat") else "Unknown"
            text = (
                f"<b>📖 {esc(pretty(s['name']))} — Species</b>\n\n"
                f"Generation: <code>{esc(pretty(s['generation']['name']))}</code>\n"
                f"Capture Rate: <code>{s.get('capture_rate')}</code>\n"
                f"Base Happiness: <code>{s.get('base_happiness')}</code>\n"
                f"Legendary: <code>{'Yes' if s.get('is_legendary') else 'No'}</code>\n"
                f"Mythical: <code>{'Yes' if s.get('is_mythical') else 'No'}</code>\n"
                f"Habitat: <code>{esc(habitat)}</code>\n"
            )
            await message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        if action == "games":
            p = await api.pokemon(pid_)
            if not p:
                return
            games = []
            for x in p.get("game_indices", []):
                v = pretty(x["version"]["name"])
                if v not in games:
                    games.append(v)
            text = f"🎮 {pretty(p['name']).upper()} — GAMES\n\n" + ("\n".join(f"• {g}" for g in games) or "No data.")
            await message.reply_text(text)
            return

        if action == "fav":
            exists = await (await db.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND pokemon_id=?",
                (u, pid_)
            )).fetchone()
            if exists:
                await db.execute("DELETE FROM favorites WHERE user_id=? AND pokemon_id=?", (u, pid_))
            else:
                await db.execute("INSERT OR IGNORE INTO favorites(user_id,pokemon_id) VALUES(?,?)", (u, pid_))
            await db.commit()

            # refresh keyboard
            p = await api.pokemon(pid_)
            fav = await is_favorite(db, u, pid_)
            shiny_on = bool(user_data.get("shiny_on", False))
            try:
                await q.edit_message_reply_markup(reply_markup=pokemon_kb(pid_, fav, shiny_on))
            except Exception:
                pass
            return

        if action == "shiny":
            shiny_on = not bool(user_data.get("shiny_on", False))
            user_data["shiny_on"] = shiny_on

            if shiny_on:
                await db.execute(
                    "INSERT OR IGNORE INTO shinies(user_id,pokemon_id) VALUES(?,?)",
                    (u, pid_)
                )
                await db.commit()

            await open_pokemon(q, context, pid_, edit=True, shiny=shiny_on)
            return

    # fallback
    return


# ----------------------------
# Nice UX: allow typing "pikachu" in private chat
# ----------------------------
async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update_message(update)
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if chat.type != "private":
        return

    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    user_data = context_user_data(context)

    # Persistent keyboard button routing
    reply_btn_map = {
        "🌿 Catch Wild": wild_cmd,
        "⚔️ Battle Arena": battle_cmd,
        "📦 My Box": box_cmd,
        "🎒 Bag & Shop": bag_cmd,
        "🏆 Gym Badges": gym_cmd,
        "❓ Who's That Pokémon": wtp_cmd,
        "🌌 Pic of the Day": space_cmd,
        "📜 Menu & Help": menu_cmd,
    }
    if text in reply_btn_map:
        await reply_btn_map[text](update, context)
        return

    if text == "🧰 Utilities":
        await message.reply_text(
            "<b>🧰 Utilities Hub</b>\nChoose a tool below:",
            parse_mode=ParseMode.HTML,
            reply_markup=utils_menu_kb(),
        )
        return

    if text == "🌦️ Weather":
        user_data["utility_mode"] = "weather"
        await message.reply_text(
            "🌦️ <b>Weather Finder</b>\nPlease type a city name (e.g. <code>Chennai</code>, <code>Paris</code>, or <code>London, UK</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=utility_cancel_kb(),
        )
        return

    utility_mode = user_data.pop("utility_mode", None)
    utility_prompt = user_data.pop("utility_prompt", None)
    if utility_prompt:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=utility_prompt[0],
                message_id=utility_prompt[1],
                reply_markup=None,
            )
        except Exception:
            pass
    api: PokeAPI = context.application.bot_data["api"]
    if utility_mode == "wiki":
        await send_wikipedia(message, api, text)
        return
    if utility_mode == "define":
        await send_definition(message, api, text)
        return
    if utility_mode == "fx":
        await send_fx_conversion(message, api, text.split())
        return
    if utility_mode == "weather":
        await send_weather(message, api, text)
        return
    if utility_mode == "berry":
        await send_berry(message, api, text.split())
        return
    if utility_mode == "iv":
        await send_iv_estimate(message, api, text.split())
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    await ensure_user(db, update)
    user_data["_uid"] = uid(update)

    # Accept: "25" or "pikachu"
    await open_pokemon(message, context, text.lower(), shiny=False)


# ----------------------------
# App lifecycle
# ----------------------------
async def _handle_health_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.readline()
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
        body = b'{"status":"healthy","bot":"Pokedex Go"}\n'
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n"
            + body
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server(port: int) -> asyncio.Server:
    server = await asyncio.start_server(_handle_health_request, "0.0.0.0", port)
    logging.getLogger(__name__).info("Health server listening on port %d", port)
    return server


async def post_init(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]

    db = await aiosqlite.connect(DB_PATH)
    old_schema = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    if old_schema:
        current_version = await database_version(db)
        if current_version < SCHEMA_VERSION:
            directories = configured_backup_directories(BACKUP_DIR)
            await backup_to_directories(db, directories)
    await initialize_database(db)

    client = httpx.AsyncClient(timeout=cfg.http_timeout)

    app.bot_data["db"] = db
    app.bot_data["http"] = client
    app.bot_data["api"] = PokeAPI(client, db, cfg.cache_ttl_sec)

    # prune cache sometimes at startup
    await cache_prune(db, older_than_sec=60 * 60 * 24 * 30)  # 30 days

    port_val = os.getenv("PORT", "").strip()
    if port_val and port_val.isdigit():
        port = int(port_val)
        server = await start_health_server(port)
        app.bot_data["health_server"] = server


async def post_shutdown(app: Application) -> None:
    server = app.bot_data.get("health_server")
    if server:
        server.close()
        await server.wait_closed()
    db: aiosqlite.Connection = app.bot_data.get("db")
    client: httpx.AsyncClient = app.bot_data.get("http")
    if client:
        await client.aclose()
    if db:
        await db.close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    update_id = getattr(update, "update_id", "unknown")
    logger = logging.getLogger(__name__)
    logger.error(
        "Unhandled %s while processing Telegram update %s",
        type(context.error).__name__ if context.error else "error",
        update_id,
    )
    bot_data = context.application.bot_data
    error_count = bot_data.get("unhandled_error_count", 0) + 1
    bot_data["unhandled_error_count"] = error_count
    admin_id = bot_data.get("admin_user_id")
    now = time.monotonic()
    last_alert = bot_data.get("last_error_alert")
    if (
        admin_id
        and error_count >= ERROR_ALERT_THRESHOLD
        and (
            last_alert is None
            or now - last_alert >= ERROR_ALERT_COOLDOWN_SEC
        )
    ):
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠️ The bot has encountered {error_count} unhandled errors "
                    "since startup. Run /health for status."
                ),
            )
        except TelegramError:
            logger.exception("Could not send the operational error alert to the admin")
        else:
            bot_data["last_error_alert"] = now


async def rate_limit_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    limiter: UserRateLimiter = context.application.bot_data["rate_limiter"]
    if limiter.allow(user.id):
        return

    if limiter.should_notify(user.id):
        if update.callback_query:
            await update.callback_query.answer(
                "You're sending requests too quickly. Please wait a moment.",
                show_alert=False,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "You're sending requests too quickly. Please wait a moment."
            )
    raise ApplicationHandlerStop


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid LOG_LEVEL: {level_name}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_app() -> Application:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set BOT_TOKEN env var")

    cfg = Config(token=token)
    app = (
        Application.builder()
        .token(cfg.token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["cfg"] = cfg
    app.bot_data["rate_limiter"] = UserRateLimiter()
    app.bot_data["admin_user_id"] = configured_admin_user_id()
    app.add_error_handler(error_handler)

    app.add_handler(TypeHandler(Update, rate_limit_update), group=-1)

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pokemon", pokemon_cmd))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("wild", wild_cmd))
    app.add_handler(CommandHandler("catch", wild_cmd))
    app.add_handler(CommandHandler("bag", bag_cmd))
    app.add_handler(CommandHandler("box", box_cmd))
    app.add_handler(CommandHandler("battle", battle_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("sell", sell_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("eggs", eggs_cmd))
    app.add_handler(CommandHandler("incubator", eggs_cmd))
    app.add_handler(CommandHandler("evolve", evolve_cmd))
    app.add_handler(CommandHandler("wtp", wtp_cmd))
    app.add_handler(CommandHandler("meta", meta_cmd))
    app.add_handler(CommandHandler("smogon", meta_cmd))
    app.add_handler(CommandHandler("gym", gym_cmd))
    app.add_handler(CommandHandler("gyms", gym_cmd))
    app.add_handler(CommandHandler("badges", badges_cmd))
    app.add_handler(CommandHandler("duel", duel_cmd))
    app.add_handler(CommandHandler("trainer", trainer_cmd))
    app.add_handler(CommandHandler("card", trainer_cmd))
    app.add_handler(CommandHandler("statcard", statcard_cmd))
    app.add_handler(CommandHandler("claim", claim_cmd))
    app.add_handler(CommandHandler("nickname", nickname_cmd))
    app.add_handler(CommandHandler("release", release_cmd))
    app.add_handler(CommandHandler("pollquiz", pollquiz_cmd))
    app.add_handler(CommandHandler("poll", pollquiz_cmd))
    app.add_handler(CommandHandler("spawn", spawn_cmd))
    app.add_handler(CommandHandler("type", type_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("ranking", ranking_cmd))
    app.add_handler(CommandHandler("favorites", favorites_cmd))
    app.add_handler(CommandHandler("shinies", shinies_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("item", item_cmd))
    app.add_handler(CommandHandler("move", move_cmd))
    app.add_handler(CommandHandler("ability", ability_cmd))
    app.add_handler(CommandHandler("where", where_cmd))
    app.add_handler(CommandHandler("nature", nature_cmd))
    app.add_handler(CommandHandler("stat", stat_cmd))
    app.add_handler(CommandHandler("berry", berry_cmd))
    app.add_handler(CommandHandler("dex", dex_cmd))
    app.add_handler(CommandHandler("team", team_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("challenge", challenge_cmd))
    app.add_handler(CommandHandler("wiki", wiki_cmd))
    app.add_handler(CommandHandler("define", define_cmd))
    app.add_handler(CommandHandler("quote", quote_cmd))
    app.add_handler(CommandHandler("fact", fact_cmd))
    app.add_handler(CommandHandler("space", space_cmd))
    app.add_handler(CommandHandler("apod", space_cmd))
    app.add_handler(CommandHandler("pic", space_cmd))
    app.add_handler(CommandHandler("fx", fx_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("generation", generation_cmd))
    app.add_handler(CommandHandler("iv", iv_cmd))

    # Inline query handler (@BotName pokemon_name)
    app.add_handler(InlineQueryHandler(inline_pokemon_search))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))


    # Text search in private chats
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_search))

    return app


if __name__ == "__main__":
    configure_logging()
    app = build_app()
    logging.getLogger(__name__).info("Pokédex Go is running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)