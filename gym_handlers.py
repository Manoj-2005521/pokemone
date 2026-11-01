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
from database import award_gym_badge, get_trainer, get_user_badges, update_trainer_inventory
from utility_helpers import esc, pretty
from battle_handlers import DEFAULT_MOVES, calculate_battle_damage, render_hp_bar

logger = logging.getLogger(__name__)

GYM_LEADERS = [
    {
        "id": 1,
        "name": "Brock",
        "city": "Pewter City",
        "badge": "🪨 Boulder Badge",
        "badge_name": "Boulder Badge",
        "pokemon_id": 95,  # Onix
        "level": 15,
        "reward": 300,
        "quote": "My rock-hard willpower will crush your offense!",
    },
    {
        "id": 2,
        "name": "Misty",
        "city": "Cerulean City",
        "badge": "💧 Cascade Badge",
        "badge_name": "Cascade Badge",
        "pokemon_id": 121,  # Starmie
        "level": 22,
        "reward": 450,
        "quote": "My sweet Pokémon are all-powerful in the water!",
    },
    {
        "id": 3,
        "name": "Lt. Surge",
        "city": "Vermilion City",
        "badge": "⚡ Thunder Badge",
        "badge_name": "Thunder Badge",
        "pokemon_id": 26,  # Raichu
        "level": 28,
        "reward": 600,
        "quote": "Ten-hut! I'll zap you with pure high-voltage power!",
    },
    {
        "id": 4,
        "name": "Erika",
        "city": "Celadon City",
        "badge": "🌈 Rainbow Badge",
        "badge_name": "Rainbow Badge",
        "pokemon_id": 45,  # Vileplume
        "level": 34,
        "reward": 750,
        "quote": "Nature will protect our graceful flowers!",
    },
    {
        "id": 5,
        "name": "Koga",
        "city": "Fuchsia City",
        "badge": "💜 Soul Badge",
        "badge_name": "Soul Badge",
        "pokemon_id": 110,  # Weezing
        "level": 39,
        "reward": 900,
        "quote": "Despair to the creeping toxic ninja arts!",
    },
    {
        "id": 6,
        "name": "Sabrina",
        "city": "Saffron City",
        "badge": "🔮 Marsh Badge",
        "badge_name": "Marsh Badge",
        "pokemon_id": 65,  # Alakazam
        "level": 44,
        "reward": 1100,
        "quote": "I had a psychic vision of your defeat!",
    },
    {
        "id": 7,
        "name": "Blaine",
        "city": "Cinnabar Island",
        "badge": "🌋 Volcano Badge",
        "badge_name": "Volcano Badge",
        "pokemon_id": 59,  # Arcanine
        "level": 48,
        "reward": 1300,
        "quote": "Hah! Better have Burn Heal ready for this heat!",
    },
    {
        "id": 8,
        "name": "Giovanni",
        "city": "Viridian City",
        "badge": "🌍 Earth Badge",
        "badge_name": "Earth Badge",
        "pokemon_id": 112,  # Rhydon
        "level": 52,
        "reward": 1800,
        "quote": "Welcome to my hideout. Flee or face absolute ruin!",
    },
]


async def gym_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    badges_earned = await get_user_badges(db, user.id)

    lines = [
        f"🏛️ <b>KANTO GYM LEADER CHALLENGE</b> 🏛️\n"
        f"Trainer: <b>{esc(user.first_name)}</b> | Badges: <b>{len(badges_earned)}/8</b>\n\n"
        f"Select a Gym Leader to challenge:"
    ]

    buttons = []
    for g in GYM_LEADERS:
        is_defeated = g["id"] in badges_earned
        status_icon = "✅" if is_defeated else "⚔️"
        btn_text = f"{status_icon} #{g['id']} {g['name']} ({g['city']})"
        buttons.append([
            InlineKeyboardButton(btn_text, callback_data=f"gym|fight|{g['id']}")
        ])

    buttons.append([InlineKeyboardButton("🏆 View Badge Case", callback_data="gym|badges")])

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def badges_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    badges_earned = await get_user_badges(db, user.id)

    lines = [f"🏆 <b>{esc(user.first_name)}'s Official Badge Case</b> 🏆\n"]
    for g in GYM_LEADERS:
        has_it = g["id"] in badges_earned
        badge_display = g["badge"] if has_it else "🔒 <i>Locked</i>"
        lines.append(f"• <b>Gym #{g['id']} ({g['name']}):</b> {badge_display}")

    lines.append(f"\n<b>Total Progress:</b> {len(badges_earned)}/8 Badges")
    if len(badges_earned) == 8:
        lines.append("🌟 <b>CONGRATULATIONS! You have conquered all Kanto Gyms and qualified for the Pokémon League!</b>")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚔️ Challenge Gyms", callback_data="gym|list"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu|home"),
        ]
    ])

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_gym_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split("|")
    action = parts[1] if len(parts) > 1 else ""

    if action == "list":
        await gym_cmd(update, context)
        return

    if action == "badges":
        await badges_cmd(update, context)
        return

    if action == "fight" and len(parts) > 2 and parts[2].isdigit():
        gym_id = int(parts[2])
        leader = next((g for g in GYM_LEADERS if g["id"] == gym_id), None)
        if not leader:
            return

        api: PokeAPI = context.application.bot_data["api"]
        db: aiosqlite.Connection = context.application.bot_data["db"]
        user = update.effective_user
        if not user:
            return

        # Fetch lead pokemon
        team_row = await (await db.execute(
            "SELECT pokemon_id FROM teams WHERE user_id=? ORDER BY added_at ASC LIMIT 1",
            (user.id,),
        )).fetchone()
        user_pid = int(team_row[0]) if team_row else 25

        user_p = await api.pokemon(user_pid)
        boss_p = await api.pokemon(leader["pokemon_id"])
        if not user_p or not boss_p:
            await query.edit_message_text("❌ Gym arena failed to load. Please try again.")
            return

        user_name = pretty(user_p["name"])
        boss_name = pretty(boss_p["name"])

        user_lvl = 35
        boss_lvl = leader["level"]

        user_stats = {x["stat"]["name"]: x["base_stat"] for x in user_p.get("stats", [])}
        boss_stats = {x["stat"]["name"]: x["base_stat"] for x in boss_p.get("stats", [])}

        user_max_hp = int(((2 * user_stats.get("hp", 65) + 31) * user_lvl / 100) + user_lvl + 10)
        boss_max_hp = int(((2 * boss_stats.get("hp", 70) + 31) * boss_lvl / 100) + boss_lvl + 10)

        user_hp = user_max_hp
        boss_hp = boss_max_hp

        text = (
            f"🏛️ <b>GYM BATTLE: {leader['name'].upper()} ({leader['city']})</b>\n"
            f"<i>\"{leader['quote']}\"</i>\n\n"
            f"<b>Your Lead:</b> 🌟 <b>{esc(user_name)}</b> (Lv. {user_lvl})\n"
            f"{render_hp_bar(user_hp, user_max_hp)}\n\n"
            f"<b>Gym Leader:</b> 👑 <b>{leader['name']}'s {esc(boss_name)}</b> (Lv. {boss_lvl})\n"
            f"{render_hp_bar(boss_hp, boss_max_hp)}\n\n"
            f"Choose your move:"
        )

        buttons = [
            [
                InlineKeyboardButton(DEFAULT_MOVES[0]["name"], callback_data=f"gym|turn|{gym_id}|0|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
                InlineKeyboardButton(DEFAULT_MOVES[1]["name"], callback_data=f"gym|turn|{gym_id}|1|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
            ],
            [
                InlineKeyboardButton(DEFAULT_MOVES[2]["name"], callback_data=f"gym|turn|{gym_id}|2|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
                InlineKeyboardButton(DEFAULT_MOVES[3]["name"], callback_data=f"gym|turn|{gym_id}|3|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
            ],
            [
                InlineKeyboardButton("🏳️ Forfeit Gym", callback_data="gym|list"),
            ],
        ]

        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "turn" and len(parts) >= 9:
        gym_id = int(parts[2])
        move_idx = int(parts[3])
        user_pid = int(parts[4])
        user_hp = int(parts[5])
        user_max_hp = int(parts[6])
        boss_hp = int(parts[7])
        boss_max_hp = int(parts[8])

        leader = next((g for g in GYM_LEADERS if g["id"] == gym_id), None)
        if not leader:
            return

        api: PokeAPI = context.application.bot_data["api"]
        db: aiosqlite.Connection = context.application.bot_data["db"]
        user = update.effective_user
        if not user:
            return

        user_p = await api.pokemon(user_pid)
        boss_p = await api.pokemon(leader["pokemon_id"])
        if not user_p or not boss_p:
            return

        user_name = pretty(user_p["name"])
        boss_name = pretty(boss_p["name"])

        user_stats = {x["stat"]["name"]: x["base_stat"] for x in user_p.get("stats", [])}
        boss_stats = {x["stat"]["name"]: x["base_stat"] for x in boss_p.get("stats", [])}

        chosen_move = DEFAULT_MOVES[move_idx]
        log = []

        if chosen_move["power"] < 0:
            heal = abs(chosen_move["power"])
            user_hp = min(user_max_hp, user_hp + heal)
            log.append(f"💚 <b>{esc(user_name)}</b> healed for {heal} HP!")
        else:
            dmg = calculate_battle_damage(35, user_stats.get("attack", 65), boss_stats.get("defense", 60), chosen_move["power"])
            boss_hp = max(0, boss_hp - dmg)
            log.append(f"💥 <b>{esc(user_name)}</b> dealt <b>{dmg} DMG</b> with {chosen_move['name']}!")

        if boss_hp <= 0:
            # Won Badge!
            await award_gym_badge(db, user.id, gym_id)
            await update_trainer_inventory(db, user.id, coins_delta=leader["reward"])
            await db.execute("UPDATE users SET score=score+50 WHERE id=?", (user.id,))
            await db.commit()

            win_text = (
                f"🏆 <b>GYM VICTORY! {leader['name'].upper()} DEFEATED!</b> 🏆\n\n"
                f"🌟 You defeated <b>Leader {leader['name']}</b> and earned the <b>{leader['badge']}</b>!\n\n"
                f"💰 <b>Gym Reward:</b> +{leader['reward']} PokéDollars & +50 Score!\n"
                f"Check your trophy case with <b>/badges</b>!"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🏛️ Next Gym Challenge", callback_data="gym|list"),
                    InlineKeyboardButton("🏆 Badge Case", callback_data="gym|badges"),
                ]
            ])
            await query.edit_message_text(win_text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        # Leader counter attack
        boss_dmg = calculate_battle_damage(leader["level"], boss_stats.get("attack", 65), user_stats.get("defense", 60), 65)
        user_hp = max(0, user_hp - boss_dmg)
        log.append(f"⚡ <b>Leader {leader['name']}'s {esc(boss_name)}</b> struck back for <b>{boss_dmg} DMG</b>!")

        if user_hp <= 0:
            loss_text = (
                f"💀 <b>GYM CHALLENGE FAILED!</b>\n\n"
                f"<b>{esc(user_name)}</b> was overwhelmed by Leader {leader['name']}'s strength.\n"
                f"Train up your Pokémon and challenge the gym again!"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⚔️ Rechallenge Gym", callback_data=f"gym|fight|{gym_id}"),
                    InlineKeyboardButton("🏛️ Gym Hub", callback_data="gym|list"),
                ]
            ])
            await query.edit_message_text(loss_text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        status_text = (
            f"🏛️ <b>GYM BATTLE: {leader['name'].upper()} ({leader['city']})</b>\n\n"
            + "\n".join(log)
            + "\n\n"
            f"<b>Your Lead:</b> 🌟 <b>{esc(user_name)}</b>\n"
            f"{render_hp_bar(user_hp, user_max_hp)}\n\n"
            f"<b>Leader {leader['name']}:</b> 👑 <b>{esc(boss_name)}</b>\n"
            f"{render_hp_bar(boss_hp, boss_max_hp)}\n\n"
            f"Choose your next move:"
        )

        buttons = [
            [
                InlineKeyboardButton(DEFAULT_MOVES[0]["name"], callback_data=f"gym|turn|{gym_id}|0|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
                InlineKeyboardButton(DEFAULT_MOVES[1]["name"], callback_data=f"gym|turn|{gym_id}|1|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
            ],
            [
                InlineKeyboardButton(DEFAULT_MOVES[2]["name"], callback_data=f"gym|turn|{gym_id}|2|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
                InlineKeyboardButton(DEFAULT_MOVES[3]["name"], callback_data=f"gym|turn|{gym_id}|3|{user_pid}|{user_hp}|{user_max_hp}|{boss_hp}|{boss_max_hp}"),
            ],
            [
                InlineKeyboardButton("🏳️ Forfeit Gym", callback_data="gym|list"),
            ],
        ]

        await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
