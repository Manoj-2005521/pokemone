import logging
import random
import re
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
from database import get_trainer, record_discovery, update_trainer_inventory
from utility_helpers import esc, pretty

logger = logging.getLogger(__name__)


async def wtp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    api: PokeAPI = context.application.bot_data["api"]
    names = await api.pokemon_names()
    if not names:
        await message.reply_text("❌ Pokémon catalog unavailable.")
        return

    pid = random.randint(1, 1025)
    p = await api.pokemon(pid)
    species = await api.species(pid)

    if not p or not species:
        await message.reply_text("❌ Could not load mystery Pokémon. Try /wtp again!")
        return

    correct_name = pretty(p["name"])
    raw_name = p["name"]

    # Redact name from flavor text
    entries = species.get("flavor_text_entries", [])
    english_entry = next((e["flavor_text"] for e in entries if e.get("language", {}).get("name") == "en"), "")
    cleaned_desc = re.sub(r"[\n\f\r]+", " ", english_entry)
    redacted_desc = re.sub(re.escape(raw_name), "██████", cleaned_desc, flags=re.IGNORECASE)
    if not redacted_desc.strip():
        redacted_desc = f"A mysterious {types} type Pokémon discovered in the {gen} region."

    types = " / ".join(pretty(t["type"]["name"]) for t in p.get("types", []))
    gen = species.get("generation", {}).get("name", "").replace("generation-", "Gen ").upper()

    # Create 4 choices
    decoys = random.sample([n for n in names if n != raw_name], 3)
    choices = [correct_name] + [pretty(d) for d in decoys]
    random.shuffle(choices)

    qid = f"wtp_{pid}_{random.randint(1000, 9999)}"
    context.application.bot_data[qid] = {
        "pid": pid,
        "correct": correct_name,
        "types": types,
    }

    text = (
        f"❓ <b>WHO'S THAT POKÉMON?</b> ❓\n\n"
        f"<b>Type:</b> <code>{esc(types)}</code>\n"
        f"<b>Region:</b> <code>{esc(gen)}</code>\n\n"
        f"📖 <b>Pokédex Hint:</b>\n"
        f"<i>\"{esc(redacted_desc[:300])}\"</i>\n\n"
        f"Can you identify this Pokémon?"
    )

    buttons = [
        [
            InlineKeyboardButton(choices[0], callback_data=f"wtp|{qid}|{choices[0]}"),
            InlineKeyboardButton(choices[1], callback_data=f"wtp|{qid}|{choices[1]}"),
        ],
        [
            InlineKeyboardButton(choices[2], callback_data=f"wtp|{qid}|{choices[2]}"),
            InlineKeyboardButton(choices[3], callback_data=f"wtp|{qid}|{choices[3]}"),
        ],
    ]

    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def on_wtp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    if len(parts) > 1 and parts[1] == "new":
        await query.answer()
        await wtp_cmd(update, context)
        return

    if len(parts) < 3:
        return

    qid = parts[1]
    selected_answer = "|".join(parts[2:])

    quiz_data = context.application.bot_data.get(qid)
    if not quiz_data:
        await query.answer("This trivia question has expired! Start a new one with /wtp.", show_alert=True)
        return

    await query.answer()

    user = update.effective_user
    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]

    correct_name = quiz_data["correct"]
    pid = quiz_data["pid"]

    # Expire quiz
    context.application.bot_data.pop(qid, None)

    if selected_answer.casefold() == correct_name.casefold():
        reward_coins = 75
        await update_trainer_inventory(db, user.id, coins_delta=reward_coins, greatballs_delta=1)
        if user:
            await db.execute("UPDATE users SET score=score+15, correct=correct+1 WHERE id=?", (user.id,))
            await db.commit()
            await record_discovery(db, user.id, pid)

        win_text = (
            f"🎉 <b>IT'S {esc(correct_name).upper()}!</b> 🎉\n\n"
            f"✅ <b>Correct!</b> Solved by <b>{esc(user.first_name)}</b>!\n\n"
            f"💰 <b>Reward:</b> +{reward_coins} PokéDollars & +1 Great Ball 🔵\n"
            f"Registered in your Pokédex!"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❓ Play Again (/wtp)", callback_data="wtp|new"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu|home"),
            ]
        ])
        await query.edit_message_text(win_text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        loss_text = (
            f"❌ <b>Incorrect!</b>\n\n"
            f"The correct Pokémon was <b>{esc(correct_name)}</b>!\n"
            f"Better luck next time, Trainer!"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❓ Try Another", callback_data="wtp|new"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu|home"),
            ]
        ])
        await query.edit_message_text(loss_text, parse_mode=ParseMode.HTML, reply_markup=kb)
