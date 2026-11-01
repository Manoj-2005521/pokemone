import math
import random
from typing import Any, Optional, Sequence
import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import PokeAPI
from database import get_trainer, update_trainer_inventory
from utility_helpers import esc, pretty

DEFAULT_MOVES = [
    {"name": "⚡ Quick Attack", "power": 40, "type": "normal"},
    {"name": "💥 Slam", "power": 70, "type": "normal"},
    {"name": "🔥 Elemental Blast", "power": 85, "type": "fire"},
    {"name": "🛡️ Protect & Recover", "power": -35, "type": "heal"},
]


def render_hp_bar(current: int, maximum: int) -> str:
    current = max(0, min(current, maximum))
    bar_length = 10
    fill = int(math.ceil((current / maximum) * bar_length))
    empty = bar_length - fill
    return f"[{'█' * fill}{'░' * empty}] {current}/{maximum} HP"


def calculate_battle_damage(
    attacker_lvl: int,
    atk_stat: int,
    def_stat: int,
    power: int,
) -> int:
    base = ((((2 * attacker_lvl / 5) + 2) * power * (atk_stat / max(1, def_stat))) / 50) + 2
    variance = random.uniform(0.85, 1.15)
    crit = 1.5 if random.random() < 0.12 else 1.0
    return max(1, int(base * variance * crit))


def battle_kb(
    user_pid: int,
    opp_pid: int,
    user_hp: int,
    user_max_hp: int,
    opp_hp: int,
    opp_max_hp: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(DEFAULT_MOVES[0]["name"], callback_data=f"b|0|{user_pid}|{opp_pid}|{user_hp}|{user_max_hp}|{opp_hp}|{opp_max_hp}"),
            InlineKeyboardButton(DEFAULT_MOVES[1]["name"], callback_data=f"b|1|{user_pid}|{opp_pid}|{user_hp}|{user_max_hp}|{opp_hp}|{opp_max_hp}"),
        ],
        [
            InlineKeyboardButton(DEFAULT_MOVES[2]["name"], callback_data=f"b|2|{user_pid}|{opp_pid}|{user_hp}|{user_max_hp}|{opp_hp}|{opp_max_hp}"),
            InlineKeyboardButton(DEFAULT_MOVES[3]["name"], callback_data=f"b|3|{user_pid}|{opp_pid}|{user_hp}|{user_max_hp}|{opp_hp}|{opp_max_hp}"),
        ],
        [
            InlineKeyboardButton("🏳️ Forfeit Battle", callback_data="b|run"),
        ],
    ])


async def battle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    api: PokeAPI = context.application.bot_data["api"]
    db: aiosqlite.Connection = context.application.bot_data["db"]

    # Check user team lead or default to Pikachu (#25)
    team_row = await (await db.execute(
        "SELECT pokemon_id FROM teams WHERE user_id=? ORDER BY added_at ASC LIMIT 1",
        (user.id,),
    )).fetchone()

    user_pid = int(team_row[0]) if team_row else 25
    user_pokemon = await api.pokemon(user_pid)
    if not user_pokemon:
        await message.reply_text("❌ Could not prepare your battle team. Try again shortly.")
        return

    # Generate wild boss opponent
    opp_pid = random.randint(1, 1025)
    opp_pokemon = await api.pokemon(opp_pid)
    if not opp_pokemon:
        await message.reply_text("❌ Could not find a battle opponent. Try again shortly.")
        return

    user_name = pretty(user_pokemon["name"])
    opp_name = pretty(opp_pokemon["name"])

    user_lvl = 30
    opp_lvl = random.randint(25, 35)

    user_stats = {x["stat"]["name"]: x["base_stat"] for x in user_pokemon.get("stats", [])}
    opp_stats = {x["stat"]["name"]: x["base_stat"] for x in opp_pokemon.get("stats", [])}

    user_max_hp = int(((2 * user_stats.get("hp", 60) + 31) * user_lvl / 100) + user_lvl + 10)
    opp_max_hp = int(((2 * opp_stats.get("hp", 60) + 31) * opp_lvl / 100) + opp_lvl + 10)

    user_hp = user_max_hp
    opp_hp = opp_max_hp

    text = (
        f"⚔️ <b>TRAINER BATTLE ARENA</b> ⚔️\n\n"
        f"<b>Your Lead:</b> 🌟 <b>{esc(user_name)}</b> (Lv. {user_lvl})\n"
        f"{render_hp_bar(user_hp, user_max_hp)}\n\n"
        f"<b>Opponent:</b> 👹 <b>Wild {esc(opp_name)}</b> (Lv. {opp_lvl})\n"
        f"{render_hp_bar(opp_hp, opp_max_hp)}\n\n"
        f"Choose your battle move:"
    )

    kb = battle_kb(user_pid, opp_pid, user_hp, user_max_hp, opp_hp, opp_max_hp)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_battle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split("|")
    if len(parts) < 2:
        return

    if parts[1] == "run":
        await query.edit_message_text(
            "🏳️ <i>You forfeited the battle and retreated safely.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(parts) < 8:
        return

    move_idx = int(parts[1])
    user_pid = int(parts[2])
    opp_pid = int(parts[3])
    user_hp = int(parts[4])
    user_max_hp = int(parts[5])
    opp_hp = int(parts[6])
    opp_max_hp = int(parts[7])

    api: PokeAPI = context.application.bot_data["api"]
    db: aiosqlite.Connection = context.application.bot_data["db"]
    user = update.effective_user
    if not user:
        return

    user_p = await api.pokemon(user_pid)
    opp_p = await api.pokemon(opp_pid)
    if not user_p or not opp_p:
        await query.edit_message_text("❌ Battle interrupted due to loading error.")
        return

    user_name = pretty(user_p["name"])
    opp_name = pretty(opp_p["name"])

    user_stats = {x["stat"]["name"]: x["base_stat"] for x in user_p.get("stats", [])}
    opp_stats = {x["stat"]["name"]: x["base_stat"] for x in opp_p.get("stats", [])}

    chosen_move = DEFAULT_MOVES[move_idx]
    log_lines = []

    # Player Turn
    if chosen_move["power"] < 0:
        # Healing
        heal_amt = abs(chosen_move["power"])
        user_hp = min(user_max_hp, user_hp + heal_amt)
        log_lines.append(f"💚 <b>{esc(user_name)}</b> used <i>{chosen_move['name']}</i> and recovered {heal_amt} HP!")
    else:
        dmg = calculate_battle_damage(30, user_stats.get("attack", 65), opp_stats.get("defense", 60), chosen_move["power"])
        opp_hp = max(0, opp_hp - dmg)
        log_lines.append(f"💥 <b>{esc(user_name)}</b> used <i>{chosen_move['name']}</i> for <b>{dmg} DMG</b>!")

    # Check opponent fainted
    if opp_hp <= 0:
        earned_coins = random.randint(100, 250)
        await update_trainer_inventory(db, user.id, coins_delta=earned_coins)
        await db.execute("UPDATE users SET score=score+10 WHERE id=?", (user.id,))
        await db.commit()

        win_text = (
            f"🏆 <b>VICTORY!</b>\n\n"
            f"💥 <b>Wild {esc(opp_name)} fainted!</b>\n"
            f"🌟 <b>{esc(user_name)}</b> won the battle!\n\n"
            f"💰 <b>Battle Spoils:</b> +{earned_coins} PokéDollars & +10 Trainer Score!"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚔️ Battle Again", callback_data="b|again"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu|home"),
            ]
        ])
        await query.edit_message_text(win_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # Opponent Turn
    opp_move = random.choice([m for m in DEFAULT_MOVES if m["power"] > 0])
    opp_dmg = calculate_battle_damage(28, opp_stats.get("attack", 60), user_stats.get("defense", 60), opp_move["power"])
    user_hp = max(0, user_hp - opp_dmg)
    log_lines.append(f"⚡ <b>Wild {esc(opp_name)}</b> countered with <i>{opp_move['name']}</i> for <b>{opp_dmg} DMG</b>!")

    # Check user fainted
    if user_hp <= 0:
        loss_text = (
            f"💀 <b>DEFEATED!</b>\n\n"
            f"❌ <b>{esc(user_name)}</b> fainted!\n"
            f"You rushed to the nearest Pokémon Center to recover.\n\n"
            f"Train up your team and try again!"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚔️ Retry Battle", callback_data="b|again"),
                InlineKeyboardButton("🎒 Check Bag", callback_data="shop|bag"),
            ]
        ])
        await query.edit_message_text(loss_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # Battle Continues
    status_text = (
        f"⚔️ <b>TRAINER BATTLE ARENA</b> ⚔️\n\n"
        + "\n".join(log_lines)
        + "\n\n"
        f"<b>Your Lead:</b> 🌟 <b>{esc(user_name)}</b>\n"
        f"{render_hp_bar(user_hp, user_max_hp)}\n\n"
        f"<b>Opponent:</b> 👹 <b>Wild {esc(opp_name)}</b>\n"
        f"{render_hp_bar(opp_hp, opp_max_hp)}\n\n"
        f"Choose your next move:"
    )

    kb = battle_kb(user_pid, opp_pid, user_hp, user_max_hp, opp_hp, opp_max_hp)
    await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=kb)
