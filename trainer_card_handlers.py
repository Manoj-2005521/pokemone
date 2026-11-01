import datetime
import logging
import math
from typing import Any, Optional
import aiosqlite
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import PokeAPI
from database import (
    delete_caught_pokemon,
    get_caught_pokemon_by_id,
    get_trainer,
    get_user_badges,
    update_caught_nickname,
    update_trainer_inventory,
)
from utility_helpers import esc, pretty
from gym_handlers import GYM_LEADERS

logger = logging.getLogger(__name__)


def ascii_stat_bar(val: int, max_val: int = 255) -> str:
    bar_len = 10
    fill = max(1, min(bar_len, int((val / 160) * bar_len)))
    return f"[{'█' * fill}{'░' * (bar_len - fill)}] {val}"


async def trainer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]

    # User basic stats
    user_row = await (await db.execute(
        "SELECT score, streak, quizzes, correct FROM users WHERE id=?",
        (user.id,),
    )).fetchone()

    score = user_row[0] if user_row else 0
    streak = user_row[1] if user_row else 0
    quizzes = user_row[2] if user_row else 0
    correct = user_row[3] if user_row else 0

    trainer = await get_trainer(db, user.id)
    badges = await get_user_badges(db, user.id)

    # Caught count
    caught_row = await (await db.execute(
        "SELECT COUNT(*) FROM caught_pokemon WHERE user_id=?", (user.id,)
    )).fetchone()
    caught_count = caught_row[0] if caught_row else 0

    # Discoveries count
    dex_row = await (await db.execute(
        "SELECT COUNT(*) FROM dex_discoveries WHERE user_id=?", (user.id,)
    )).fetchone()
    dex_count = dex_row[0] if dex_row else 0

    badge_icons = "".join(next((g["badge"].split()[0] for g in GYM_LEADERS if g["id"] == bid), "⭐") for bid in badges) or "<i>None yet</i>"

    rank_title = (
        "👑 Pokémon Champion" if len(badges) == 8 and dex_count >= 500
        else "🌟 Elite Trainer" if len(badges) >= 6
        else "⚔️ Gym Challenger" if len(badges) >= 3
        else "🌿 Rising Novice"
    )

    text = (
        f"💳 <b>OFFICIAL TRAINER CARD</b> 💳\n\n"
        f"<b>Trainer:</b> <b>{esc(user.first_name)}</b>\n"
        f"<b>Rank Title:</b> <b>{esc(rank_title)}</b>\n"
        f"<b>ID No.:</b> <code>{user.id}</code>\n\n"
        f"💰 <b>PokéDollars:</b> <code>{trainer['coins']}</code>\n"
        f"📦 <b>Caught in Box:</b> <code>{caught_count}</code> Pokémon\n"
        f"📖 <b>Pokédex Discovered:</b> <code>{dex_count}/1025</code>\n"
        f"🏆 <b>Leaderboard Score:</b> <code>{score} pts</code>\n"
        f"🎮 <b>Trivia Accuracy:</b> <code>{correct}/{quizzes}</code> (Streak: {streak})\n\n"
        f"🏛️ <b>Gym Badges ({len(badges)}/8):</b>\n{badge_icons}"
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def statcard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    args = context.args or []
    if not args:
        await message.reply_text("Usage: <code>/statcard &lt;pokemon&gt;</code>\nExample: <code>/statcard mewtwo</code>", parse_mode=ParseMode.HTML)
        return

    name_or_id = "-".join(" ".join(args).strip().casefold().split())
    api: PokeAPI = context.application.bot_data["api"]

    p = await api.pokemon(name_or_id)
    if not p:
        await message.reply_text("❌ Pokémon not found. Please check the spelling.")
        return

    p_name = pretty(p["name"])
    types = " / ".join(pretty(t["type"]["name"]) for t in p.get("types", []))
    stats = {x["stat"]["name"]: x["base_stat"] for x in p.get("stats", [])}
    total = sum(stats.values())

    text = (
        f"📊 <b>RADAR STAT CARD: #{p['id']:03d} {esc(p_name).upper()}</b>\n"
        f"<b>Type:</b> <code>{esc(types)}</code> | <b>BST:</b> <code>{total}</code>\n\n"
        f"❤️ <b>HP:</b>       {ascii_stat_bar(stats.get('hp', 0))}\n"
        f"⚔️ <b>Attack:</b>   {ascii_stat_bar(stats.get('attack', 0))}\n"
        f"🛡️ <b>Defense:</b>  {ascii_stat_bar(stats.get('defense', 0))}\n"
        f"🔥 <b>Sp. Atk:</b>  {ascii_stat_bar(stats.get('special-attack', 0))}\n"
        f"🛡️ <b>Sp. Def:</b>  {ascii_stat_bar(stats.get('special-defense', 0))}\n"
        f"⚡ <b>Speed:</b>    {ascii_stat_bar(stats.get('speed', 0))}\n"
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def claim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    today_str = datetime.date.today().isoformat()

    trainer = await get_trainer(db, user.id)
    if trainer["last_daily_reward"] == today_str:
        await message.reply_text("⏰ <b>You have already claimed your daily reward today!</b>\nCome back tomorrow for more supplies!", parse_mode=ParseMode.HTML)
        return

    reward_coins = 150
    reward_pb = 5
    reward_gb = 2

    await update_trainer_inventory(
        db,
        user.id,
        coins_delta=reward_coins,
        pokeballs_delta=reward_pb,
        greatballs_delta=reward_gb,
        last_daily_reward=today_str,
    )

    text = (
        f"🎁 <b>DAILY TRAINER REWARD CLAIMED!</b> 🎁\n\n"
        f"Thank you for logging in today, <b>{esc(user.first_name)}</b>!\n\n"
        f"💰 <b>Received:</b> +{reward_coins} PokéDollars\n"
        f"🔴 <b>Received:</b> +{reward_pb} Pokéballs\n"
        f"🔵 <b>Received:</b> +{reward_gb} Great Balls\n\n"
        f"Explore the tall grass with <b>/wild</b> or challenge gyms with <b>/gym</b>!"
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def nickname_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await message.reply_text(
            "Usage: <code>/nickname &lt;box_id&gt; &lt;new_name&gt;</code>\nExample: <code>/nickname 1 Sparky</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    caught_id = int(args[0])
    new_nickname = " ".join(args[1:]).strip()[:24]

    db: aiosqlite.Connection = context.application.bot_data["db"]
    success = await update_caught_nickname(db, user.id, caught_id, new_nickname)

    if success:
        await message.reply_text(f"🏷️ <b>Renamed Pokémon #{caught_id} to \"{esc(new_nickname)}\"!</b>", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text("❌ Pokémon not found in your /box.")


async def release_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text(
            "Usage: <code>/release &lt;box_id&gt;</code>\nExample: <code>/release 1</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    caught_id = int(args[0])
    db: aiosqlite.Connection = context.application.bot_data["db"]

    poke = await get_caught_pokemon_by_id(db, caught_id)
    if not poke or poke["user_id"] != user.id:
        await message.reply_text("❌ Pokémon not found in your /box.")
        return

    await delete_caught_pokemon(db, user.id, caught_id)
    await update_trainer_inventory(db, user.id, coins_delta=50)

    await message.reply_text(
        f"🍃 <b>Farewell, {esc(poke['nickname'])}!</b>\n"
        f"You released it back into the wild.\n"
        f"💰 The Professor rewarded you with <b>+50 PokéDollars</b> for your research!",
        parse_mode=ParseMode.HTML,
    )
