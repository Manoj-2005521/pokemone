import logging
import random
from typing import Any, Optional
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
from battle_handlers import DEFAULT_MOVES, calculate_battle_damage, render_hp_bar

logger = logging.getLogger(__name__)

ACTIVE_DUELS: dict[str, dict[str, Any]] = {}


async def duel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    battle_id = f"duel_{user.id}_{random.randint(1000, 9999)}"
    ACTIVE_DUELS[battle_id] = {
        "p1_id": user.id,
        "p1_name": user.first_name,
        "p2_id": None,
        "p2_name": None,
        "status": "waiting",
    }
    db: aiosqlite.Connection = context.application.bot_data["db"]
    await db.execute(
        "INSERT OR REPLACE INTO pvp_battles (battle_id, p1_id, status) VALUES (?, ?, 'waiting')",
        (battle_id, user.id),
    )
    await db.commit()

    text = (
        f"⚔️ <b>PVP TRAINER DUEL CHALLENGE</b> ⚔️\n\n"
        f"Trainer <b>{esc(user.first_name)}</b> has issued an open battle challenge!\n\n"
        f"Who dares to step into the arena and fight?"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚔️ Accept Challenge!", callback_data=f"pvp|join|{battle_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"pvp|cancel|{battle_id}"),
        ]
    ])

    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_pvp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    if len(parts) < 3:
        return

    action = parts[1]
    battle_id = parts[2]
    user = update.effective_user
    if not user:
        return

    duel = ACTIVE_DUELS.get(battle_id)
    if not duel:
        await query.answer("This duel has expired or was already completed!", show_alert=True)
        return

    if action == "cancel":
        if user.id != duel["p1_id"]:
            await query.answer("Only the host can cancel this duel.", show_alert=True)
            return
        ACTIVE_DUELS.pop(battle_id, None)
        await query.edit_message_text("❌ <i>The PvP challenge was cancelled.</i>", parse_mode=ParseMode.HTML)
        return

    if action == "join":
        if user.id == duel["p1_id"]:
            await query.answer("You cannot battle yourself! Share this challenge in a group or with a friend.", show_alert=True)
            return

        api: PokeAPI = context.application.bot_data["api"]
        db: aiosqlite.Connection = context.application.bot_data["db"]

        # Fetch lead for both players
        p1_lead_row = await (await db.execute(
            "SELECT pokemon_id FROM teams WHERE user_id=? ORDER BY added_at ASC LIMIT 1", (duel["p1_id"],)
        )).fetchone()
        p2_lead_row = await (await db.execute(
            "SELECT pokemon_id FROM teams WHERE user_id=? ORDER BY added_at ASC LIMIT 1", (user.id,)
        )).fetchone()

        p1_pid = int(p1_lead_row[0]) if p1_lead_row else 25
        p2_pid = int(p2_lead_row[0]) if p2_lead_row else 6

        p1_pokemon = await api.pokemon(p1_pid)
        p2_pokemon = await api.pokemon(p2_pid)

        p1_pname = pretty(p1_pokemon["name"]) if p1_pokemon else "Pikachu"
        p2_pname = pretty(p2_pokemon["name"]) if p2_pokemon else "Charizard"

        duel.update({
            "p2_id": user.id,
            "p2_name": user.first_name,
            "p1_pid": p1_pid,
            "p2_pid": p2_pid,
            "p1_pname": p1_pname,
            "p2_pname": p2_pname,
            "p1_hp": 120,
            "p1_max_hp": 120,
            "p2_hp": 120,
            "p2_max_hp": 120,
            "status": "in_progress",
            "current_turn": duel["p1_id"],
        })
        await db.execute(
            """UPDATE pvp_battles SET p2_id=?, p1_pid=?, p2_pid=?, p1_hp=120, p1_max_hp=120,
               p2_hp=120, p2_max_hp=120, status='in_progress' WHERE battle_id=?""",
            (user.id, p1_pid, p2_pid, battle_id),
        )
        await db.commit()

        await query.answer()

        text = (
            f"⚔️ <b>PVP DUEL COMMENCED!</b> ⚔️\n\n"
            f"🔵 <b>{esc(duel['p1_name'])}'s {esc(p1_pname)}:</b>\n"
            f"{render_hp_bar(120, 120)}\n\n"
            f"🔴 <b>{esc(duel['p2_name'])}'s {esc(p2_pname)}:</b>\n"
            f"{render_hp_bar(120, 120)}\n\n"
            f"👉 <b>{esc(duel['p1_name'])}'s turn to attack!</b>"
        )

        buttons = [
            [
                InlineKeyboardButton(DEFAULT_MOVES[0]["name"], callback_data=f"pvp|move|{battle_id}|0"),
                InlineKeyboardButton(DEFAULT_MOVES[1]["name"], callback_data=f"pvp|move|{battle_id}|1"),
            ],
            [
                InlineKeyboardButton(DEFAULT_MOVES[2]["name"], callback_data=f"pvp|move|{battle_id}|2"),
                InlineKeyboardButton(DEFAULT_MOVES[3]["name"], callback_data=f"pvp|move|{battle_id}|3"),
            ],
        ]

        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "move" and len(parts) >= 4:
        move_idx = int(parts[3])
        if user.id != duel.get("current_turn"):
            await query.answer("It is not your turn right now!", show_alert=True)
            return

        await query.answer()
        db: aiosqlite.Connection = context.application.bot_data["db"]

        is_p1_turn = (user.id == duel["p1_id"])
        chosen_move = DEFAULT_MOVES[move_idx]
        attacker_name = duel["p1_name"] if is_p1_turn else duel["p2_name"]
        attacker_poke = duel["p1_pname"] if is_p1_turn else duel["p2_pname"]
        defender_poke = duel["p2_pname"] if is_p1_turn else duel["p1_pname"]

        log_lines = []

        if chosen_move["power"] < 0:
            heal = abs(chosen_move["power"])
            if is_p1_turn:
                duel["p1_hp"] = min(duel["p1_max_hp"], duel["p1_hp"] + heal)
            else:
                duel["p2_hp"] = min(duel["p2_max_hp"], duel["p2_hp"] + heal)
            log_lines.append(f"💚 <b>{esc(attacker_name)}'s {esc(attacker_poke)}</b> recovered {heal} HP!")
        else:
            dmg = calculate_battle_damage(35, 75, 65, chosen_move["power"])
            if is_p1_turn:
                duel["p2_hp"] = max(0, duel["p2_hp"] - dmg)
            else:
                duel["p1_hp"] = max(0, duel["p1_hp"] - dmg)
            log_lines.append(f"💥 <b>{esc(attacker_name)}'s {esc(attacker_poke)}</b> used {chosen_move['name']} for <b>{dmg} DMG</b>!")

        # Check knockout
        if duel["p1_hp"] <= 0 or duel["p2_hp"] <= 0:
            winner_id = duel["p1_id"] if duel["p2_hp"] <= 0 else duel["p2_id"]
            winner_name = duel["p1_name"] if duel["p2_hp"] <= 0 else duel["p2_name"]
            loser_name = duel["p2_name"] if duel["p2_hp"] <= 0 else duel["p1_name"]

            # Award spoils
            await update_trainer_inventory(db, winner_id, coins_delta=200)
            await db.execute("UPDATE users SET score=score+25 WHERE id=?", (winner_id,))
            await db.commit()

            ACTIVE_DUELS.pop(battle_id, None)
            await db.execute("UPDATE pvp_battles SET status='completed' WHERE battle_id=?", (battle_id,))
            await db.commit()

            win_text = (
                f"🏆 <b>PVP DUEL VICTORY!</b> 🏆\n\n"
                f"🎉 <b>{esc(winner_name)}</b> has defeated <b>{esc(loser_name)}</b> in combat!\n\n"
                f"💰 <b>Winner Reward:</b> +200 PokéDollars & +25 Leaderboard Score!"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⚔️ New Challenge (/duel)", callback_data="pvp|new_prompt"),
                    InlineKeyboardButton("🏠 Menu", callback_data="menu|home"),
                ]
            ])
            await query.edit_message_text(win_text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        # Switch turns
        next_turn_id = duel["p2_id"] if is_p1_turn else duel["p1_id"]
        next_turn_name = duel["p2_name"] if is_p1_turn else duel["p1_name"]
        duel["current_turn"] = next_turn_id

        text = (
            f"⚔️ <b>PVP DUEL IN PROGRESS</b> ⚔️\n\n"
            + "\n".join(log_lines)
            + "\n\n"
            f"🔵 <b>{esc(duel['p1_name'])}'s {esc(duel['p1_pname'])}:</b>\n"
            f"{render_hp_bar(duel['p1_hp'], duel['p1_max_hp'])}\n\n"
            f"🔴 <b>{esc(duel['p2_name'])}'s {esc(duel['p2_pname'])}:</b>\n"
            f"{render_hp_bar(duel['p2_hp'], duel['p2_max_hp'])}\n\n"
            f"👉 <b>{esc(next_turn_name)}'s turn to attack!</b>"
        )

        buttons = [
            [
                InlineKeyboardButton(DEFAULT_MOVES[0]["name"], callback_data=f"pvp|move|{battle_id}|0"),
                InlineKeyboardButton(DEFAULT_MOVES[1]["name"], callback_data=f"pvp|move|{battle_id}|1"),
            ],
            [
                InlineKeyboardButton(DEFAULT_MOVES[2]["name"], callback_data=f"pvp|move|{battle_id}|2"),
                InlineKeyboardButton(DEFAULT_MOVES[3]["name"], callback_data=f"pvp|move|{battle_id}|3"),
            ],
        ]

        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
