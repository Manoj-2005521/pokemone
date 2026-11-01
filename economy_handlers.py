import logging
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
from database import (
    add_caught_pokemon,
    add_trainer_egg,
    buy_market_listing,
    create_market_listing,
    evolve_caught_pokemon,
    get_caught_pokemon_by_id,
    get_market_listings,
    get_trainer,
    get_trainer_eggs,
    record_discovery,
    update_trainer_inventory,
    walk_trainer_eggs,
)
from utility_helpers import esc, pretty
from wild_handlers import NATURES, iv_rating

logger = logging.getLogger(__name__)


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    listings = await get_market_listings(db, limit=8)

    if not listings:
        await message.reply_text(
            "🏪 <b>Global Pokémon Market</b>\n\n"
            "The market is currently empty!\n"
            "You can sell any Pokémon from your box using: <code>/sell &lt;box_id&gt; &lt;price&gt;</code>\n"
            "(Find your box ID with <b>/box</b>)",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["🏪 <b>Global Pokémon Market — Recent Listings</b>\n"]
    buttons = []
    for item in listings:
        shiny_tag = "✨ " if item["is_shiny"] else ""
        lines.append(
            f"• <b>#{item['pokemon_id']:03d} {shiny_tag}{esc(item['nickname'])}</b> (Lv. {item['level']})\n"
            f"   Price: 💰 <b>{item['price']} coins</b> | IV: {iv_rating(item['ivs'])}"
        )
        buttons.append([
            InlineKeyboardButton(
                f"🛒 Buy {item['nickname']} ({item['price']} 💰)",
                callback_data=f"mkt|buy|{item['id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("🔄 Refresh Market", callback_data="mkt|refresh")])

    await message.reply_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.reply_text(
            "❌ Usage: <code>/sell &lt;box_id&gt; &lt;price&gt;</code>\nExample: <code>/sell 3 150</code>\n"
            "Find your Pokémon's ID in <b>/box</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    caught_id = int(args[0])
    price = int(args[1])

    if price < 10 or price > 100000:
        await message.reply_text("❌ Price must be between 10 and 100,000 coins.")
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    poke = await get_caught_pokemon_by_id(db, caught_id)

    if not poke or poke["user_id"] != user.id:
        await message.reply_text("❌ You don't own a Pokémon with that ID in your /box.")
        return

    await create_market_listing(db, user.id, caught_id, price)
    await message.reply_text(
        f"✅ <b>Listed {esc(poke['nickname'])} for {price} 💰 on the /market!</b>",
        parse_mode=ParseMode.HTML,
    )


async def on_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split("|")
    action = parts[1] if len(parts) > 1 else ""

    if action == "refresh":
        await market_cmd(update, context)
        return

    if action == "buy" and len(parts) > 2 and parts[2].isdigit():
        listing_id = int(parts[2])
        user = update.effective_user
        if not user:
            return

        db: aiosqlite.Connection = context.application.bot_data["db"]
        success, msg = await buy_market_listing(db, user.id, listing_id)
        if success:
            await query.answer("🎉 Purchase successful! Pokémon sent to your /box.", show_alert=True)
            await market_cmd(update, context)
        else:
            await query.answer(f"❌ {msg}", show_alert=True)


async def eggs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    eggs = await get_trainer_eggs(db, user.id)

    if not eggs:
        # Give a starter mystery egg if trainer has none!
        egg_species = random.choice([175, 447, 172, 173, 174, 236, 238, 239, 240, 440])  # Togepi, Riolu, Pichu, Cleffa, etc.
        await add_trainer_egg(db, user.id, egg_species, steps_needed=15)
        eggs = await get_trainer_eggs(db, user.id)

    lines = [f"🥚 <b>{esc(user.first_name)}'s Incubator</b>\n"]
    for i, egg in enumerate(eggs, 1):
        walked = egg["steps_walked"]
        needed = egg["steps_needed"]
        pct = int((walked / needed) * 10)
        progress_bar = f"[{'🟩' * pct}{'⬜' * (10 - pct)}]"
        lines.append(
            f"<b>Egg #{i}:</b> {progress_bar} <b>{walked}/{needed} steps</b>\n"
            f"<i>Walk by interacting with bot commands!</i>"
        )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚶 Walk 5 Steps (50 💰)", callback_data="egg|walk|5"),
            InlineKeyboardButton("✨ Hatch Boost (100 💰)", callback_data="egg|walk|15"),
        ],
        [
            InlineKeyboardButton("📦 View Box", callback_data="box|open|1"),
        ],
    ])

    await message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_egg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    steps = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
    cost = 50 if steps == 5 else 100

    user = update.effective_user
    if not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]
    trainer = await get_trainer(db, user.id)

    if trainer["coins"] < cost:
        await query.answer(f"❌ Not enough coins! Need {cost} 💰", show_alert=True)
        return

    await update_trainer_inventory(db, user.id, coins_delta=-cost)
    hatched = await walk_trainer_eggs(db, user.id, steps=steps)

    if hatched:
        hatched_messages = []
        for egg in hatched:
            pid = egg["species_id"] or random.randint(1, 1025)
            p = await api.pokemon(pid)
            p_name = pretty(p["name"]) if p else f"Pokémon #{pid}"
            nature = random.choice(NATURES)
            # Boosted IVs for hatched eggs!
            ivs = tuple(random.randint(18, 31) for _ in range(6))
            is_shiny = random.random() < 0.08  # Boosted 8% egg shiny chance

            await add_caught_pokemon(db, user.id, pid, p_name, level=1, nature=nature, ivs=ivs, is_shiny=is_shiny)
            await record_discovery(db, user.id, pid)

            shiny_tag = " ✨ <b>SHINY!</b>" if is_shiny else ""
            hatched_messages.append(
                f"🐣 <b>Oh? Your Egg hatched into a {esc(p_name)}!</b>{shiny_tag}\n"
                f"<b>Nature:</b> {nature} | <b>IV:</b> {iv_rating(ivs)}\n"
                f"Added to your <b>/box</b>!"
            )
        await query.edit_message_text("\n\n".join(hatched_messages), parse_mode=ParseMode.HTML)
    else:
        await query.answer(f"🚶 Walked {steps} steps!")
        await eggs_cmd(update, context)


async def evolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text(
            "🧬 <b>Pokémon Evolution Chamber</b>\n\n"
            "Usage: <code>/evolve &lt;box_id&gt;</code>\n"
            "Example: <code>/evolve 1</code>\n"
            "(Find your Pokémon's box ID using <b>/box</b>)",
            parse_mode=ParseMode.HTML,
        )
        return

    caught_id = int(args[0])
    db: aiosqlite.Connection = context.application.bot_data["db"]
    api: PokeAPI = context.application.bot_data["api"]

    poke = await get_caught_pokemon_by_id(db, caught_id)
    if not poke or poke["user_id"] != user.id:
        await message.reply_text("❌ Could not find that Pokémon in your /box.")
        return

    species = await api.species(poke["pokemon_id"])
    if not species or not species.get("evolution_chain"):
        await message.reply_text("❌ Evolution data unavailable.")
        return

    evo_chain_data = await api.get(species["evolution_chain"]["url"])
    if not evo_chain_data:
        await message.reply_text("❌ Evolution data unavailable.")
        return

    # Find current node in evolution chain
    curr_name = species["name"].casefold()
    next_forms = []

    def find_next(node):
        if node["species"]["name"].casefold() == curr_name:
            for child in node.get("evolves_to", []):
                next_forms.append(child["species"]["name"])
            return True
        for child in node.get("evolves_to", []):
            if find_next(child):
                return True
        return False

    find_next(evo_chain_data["chain"])

    if not next_forms:
        await message.reply_text(f"🌟 <b>{esc(poke['nickname'])}</b> is already at its final evolution stage!", parse_mode=ParseMode.HTML)
        return

    target_name = next_forms[0]
    next_poke_data = await api.pokemon(target_name)
    if not next_poke_data:
        await message.reply_text("❌ Could not load evolved form data.")
        return

    new_pid = next_poke_data["id"]
    new_name = pretty(next_poke_data["name"])

    # Perform evolution
    await evolve_caught_pokemon(db, caught_id, new_pid, new_name)
    await record_discovery(db, user.id, new_pid)

    await message.reply_text(
        f"✨ <b>WHAT?! Your {esc(poke['nickname'])} is evolving!</b> ✨\n\n"
        f"🎉 Congratulations! It evolved into <b>#{new_pid:03d} {esc(new_name)}</b>!\n"
        f"Level boosted +2 and registered in your Pokédex!",
        parse_mode=ParseMode.HTML,
    )
