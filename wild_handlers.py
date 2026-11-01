import asyncio
import logging
import random
from typing import Any, Optional, Sequence
import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import PokeAPI
from database import (
    add_caught_pokemon,
    get_caught_pokemon_page,
    get_trainer,
    record_discovery,
    update_trainer_inventory,
)
from utility_helpers import esc, pretty

logger = logging.getLogger(__name__)

NATURES = [
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
]

# Shop item prices in coins
SHOP_PRICES = {
    "pb": (50, "pokeballs", 5, "5x Pokéball 🔴"),
    "gb": (100, "greatballs", 3, "3x Great Ball 🔵"),
    "ub": (200, "ultraballs", 2, "2x Ultra Ball 🟡"),
    "rb": (75, "razz_berries", 3, "3x Razz Berry 🫐"),
}


def iv_rating(ivs: tuple[int, int, int, int, int, int]) -> str:
    total = sum(ivs)
    percentage = (total / (31 * 6)) * 100
    if percentage >= 90:
        return f"⭐⭐⭐ <b>{percentage:.0f}%</b> (Perfect)"
    elif percentage >= 70:
        return f"⭐⭐ <b>{percentage:.0f}%</b> (Great)"
    elif percentage >= 50:
        return f"⭐ <b>{percentage:.0f}%</b> (Good)"
    return f"<b>{percentage:.0f}%</b> (Decent)"


def wild_encounter_kb(
    pid: int,
    level: int,
    is_shiny: bool,
    has_berry: bool,
    trainer: dict[str, Any],
) -> InlineKeyboardMarkup:
    pb_count = trainer.get("pokeballs", 0)
    gb_count = trainer.get("greatballs", 0)
    ub_count = trainer.get("ultraballs", 0)
    rb_count = trainer.get("razz_berries", 0)

    shiny_flag = 1 if is_shiny else 0
    berry_flag = 1 if has_berry else 0

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🔴 Pokéball ({pb_count})",
                callback_data=f"w|pb|{pid}|{level}|{shiny_flag}|{berry_flag}",
            ),
            InlineKeyboardButton(
                f"🔵 Great Ball ({gb_count})",
                callback_data=f"w|gb|{pid}|{level}|{shiny_flag}|{berry_flag}",
            ),
        ],
        [
            InlineKeyboardButton(
                f"🟡 Ultra Ball ({ub_count})",
                callback_data=f"w|ub|{pid}|{level}|{shiny_flag}|{berry_flag}",
            ),
            InlineKeyboardButton(
                f"🫐 Berry ({rb_count})" + (" [Active]" if has_berry else ""),
                callback_data=f"w|rb|{pid}|{level}|{shiny_flag}|{berry_flag}",
            ),
        ],
        [
            InlineKeyboardButton("🏃 Run Away", callback_data="w|run"),
        ],
    ])


async def wild_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    api: PokeAPI = context.application.bot_data["api"]
    db: aiosqlite.Connection = context.application.bot_data["db"]

    trainer = await get_trainer(db, user.id)

    # Pick random wild Pokémon (1 to 1025)
    pid = random.randint(1, 1025)
    p = await api.pokemon(pid)
    if not p:
        await message.reply_text("❌ Failed to explore the tall grass. Try again in a moment!")
        return

    level = random.randint(3, 45)
    is_shiny = random.random() < 0.02  # 2% shiny chance in wild
    p_name = pretty(p["name"])
    types = " / ".join(pretty(t["type"]["name"]) for t in p.get("types", []))

    sprites = p.get("sprites", {})
    other = sprites.get("other", {})
    art = (other.get("official-artwork", {}).get("front_shiny" if is_shiny else "front_default")
           or sprites.get("front_shiny" if is_shiny else "front_default")
           or sprites.get("front_default"))

    shiny_badge = " ✨ <b>SHINY!</b>" if is_shiny else ""
    caption = (
        f"🌿 <b>A wild {esc(p_name)} appeared!</b>{shiny_badge}\n"
        f"<b>Level:</b> <code>{level}</code> | <b>Type:</b> <code>{esc(types)}</code>\n\n"
        f"💰 <b>Your Pokéballs:</b> 🔴 {trainer['pokeballs']} | 🔵 {trainer['greatballs']} | 🟡 {trainer['ultraballs']}\n"
        f"What will you do, Trainer?"
    )

    kb = wild_encounter_kb(pid, level, is_shiny, False, trainer)

    if art:
        await message.reply_photo(photo=art, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_wild_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split("|")
    action = parts[1] if len(parts) > 1 else ""

    if action == "run":
        await query.edit_message_caption(
            caption="🏃 <i>You safely fled from the wild encounter!</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(parts) < 6:
        return

    ball_type = parts[1]  # pb, gb, ub, rb
    pid = int(parts[2])
    level = int(parts[3])
    is_shiny = bool(int(parts[4]))
    has_berry = bool(int(parts[5]))

    user = update.effective_user
    if not user:
        return

    api: PokeAPI = context.application.bot_data["api"]
    db: aiosqlite.Connection = context.application.bot_data["db"]

    trainer = await get_trainer(db, user.id)
    p = await api.pokemon(pid)
    if not p:
        await query.edit_message_caption("❌ Pokémon data could not be loaded.")
        return

    p_name = pretty(p["name"])

    # Handle Razz Berry
    if ball_type == "rb":
        if has_berry:
            await query.answer("Berry effect is already active on this Pokémon!", show_alert=True)
            return
        if trainer["razz_berries"] <= 0:
            await query.answer("You don't have any Razz Berries! Buy some in /bag", show_alert=True)
            return

        trainer = await update_trainer_inventory(db, user.id, razz_berries_delta=-1)
        kb = wild_encounter_kb(pid, level, is_shiny, True, trainer)
        await query.edit_message_caption(
            caption=(
                f"🫐 <b>You fed a Razz Berry to {esc(p_name)}!</b>\n"
                f"Catch rate has increased!\n\n"
                f"<b>Level:</b> <code>{level}</code>" + (" ✨ <b>SHINY!</b>" if is_shiny else "") + "\n"
                f"💰 <b>Your Pokéballs:</b> 🔴 {trainer['pokeballs']} | 🔵 {trainer['greatballs']} | 🟡 {trainer['ultraballs']}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # Handle Ball Throw
    ball_map = {
        "pb": ("pokeballs", 1.0, "🔴 Pokéball"),
        "gb": ("greatballs", 1.6, "🔵 Great Ball"),
        "ub": ("ultraballs", 2.3, "🟡 Ultra Ball"),
    }
    field, multiplier, ball_name = ball_map.get(ball_type, ("pokeballs", 1.0, "🔴 Pokéball"))

    if trainer.get(field, 0) <= 0:
        await query.answer(f"You're out of {ball_name}s! Check /bag", show_alert=True)
        return

    # Deduct ball
    delta = {f"{field}_delta": -1}
    trainer = await update_trainer_inventory(db, user.id, **delta)

    # Base capture calculation
    # Species capture rate (default 120/255 if not in species)
    species_data = await api.species(pid)
    base_rate = (species_data or {}).get("capture_rate", 120) if isinstance(species_data, dict) else 120
    level_factor = max(0.4, 1.0 - (level / 120.0))
    berry_bonus = 1.6 if has_berry else 1.0

    catch_chance = min(0.95, max(0.12, (base_rate / 255.0) * multiplier * berry_bonus * level_factor))

    roll = random.random()
    if roll <= catch_chance:
        # Caught!
        nature = random.choice(NATURES)
        ivs = tuple(random.randint(0, 31) for _ in range(6))
        await add_caught_pokemon(db, user.id, pid, p_name, level, nature, ivs, is_shiny)
        await record_discovery(db, user.id, pid)

        # Bonus coins
        earned_coins = random.randint(20, 60) + (100 if is_shiny else 0)
        await update_trainer_inventory(db, user.id, coins_delta=earned_coins)

        shiny_str = " ✨ <b>SHINY</b>" if is_shiny else ""
        caption = (
            f"⭐ <b>Gotcha! {esc(p_name)} was caught!</b>{shiny_str}\n\n"
            f"<b>Level:</b> <code>{level}</code>\n"
            f"<b>Nature:</b> <code>{nature}</code>\n"
            f"<b>IV Potential:</b> {iv_rating(ivs)}\n"
            f"<b>HP / Atk / Def / SpA / SpD / Spe:</b> <code>{'/'.join(map(str, ivs))}</code>\n\n"
            f"💰 <b>Reward:</b> +{earned_coins} PokéDollars!\n"
            f"Added to your <b>/box</b> and registered in Pokédex!"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🌿 Catch Another", callback_data="w|hunt_again"),
                InlineKeyboardButton("📦 Open Box", callback_data="box|open|1"),
            ]
        ])
        await query.edit_message_caption(caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        # Escaped ball
        flee_chance = 0.15
        if random.random() < flee_chance:
            await query.edit_message_caption(
                caption=f"💨 <b>Oh no! {esc(p_name)} broke free and ran away!</b>\nBetter luck next time in /wild!",
                parse_mode=ParseMode.HTML,
            )
        else:
            kb = wild_encounter_kb(pid, level, is_shiny, False, trainer)
            await query.edit_message_caption(
                caption=(
                    f"⚠️ <b>Argh! {esc(p_name)} broke free!</b>\n"
                    f"It's still watching you cautiously...\n\n"
                    f"<b>Level:</b> <code>{level}</code>" + (" ✨ <b>SHINY!</b>" if is_shiny else "") + "\n"
                    f"💰 <b>Your Pokéballs:</b> 🔴 {trainer['pokeballs']} | 🔵 {trainer['greatballs']} | 🟡 {trainer['ultraballs']}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )


async def bag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    trainer = await get_trainer(db, user.id)

    text = (
        f"🎒 <b>Trainer Bag — {esc(user.first_name)}</b>\n\n"
        f"💰 <b>PokéDollars:</b> <code>{trainer['coins']}</code>\n"
        f"🔴 <b>Pokéballs:</b> <code>{trainer['pokeballs']}</code>\n"
        f"🔵 <b>Great Balls:</b> <code>{trainer['greatballs']}</code>\n"
        f"🟡 <b>Ultra Balls:</b> <code>{trainer['ultraballs']}</code>\n"
        f"🫐 <b>Razz Berries:</b> <code>{trainer['razz_berries']}</code>\n\n"
        f"🛒 <b>Poké Mart (Tap below to buy supplies):</b>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 5x Pokéball (50 💰)", callback_data="shop|buy|pb"),
            InlineKeyboardButton("🔵 3x Great Ball (100 💰)", callback_data="shop|buy|gb"),
        ],
        [
            InlineKeyboardButton("🟡 2x Ultra Ball (200 💰)", callback_data="shop|buy|ub"),
            InlineKeyboardButton("🫐 3x Razz Berry (75 💰)", callback_data="shop|buy|rb"),
        ],
        [
            InlineKeyboardButton("🌿 Go Hunting (/wild)", callback_data="w|hunt_again"),
        ],
    ])

    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    item_code = parts[2] if len(parts) > 2 else ""

    if item_code not in SHOP_PRICES:
        return

    cost, field, quantity, name = SHOP_PRICES[item_code]
    user = update.effective_user
    if not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    trainer = await get_trainer(db, user.id)

    if trainer["coins"] < cost:
        await query.answer(f"❌ Not enough coins! You have {trainer['coins']} coins (needs {cost}).", show_alert=True)
        return

    delta = {"coins_delta": -cost, f"{field}_delta": quantity}
    trainer = await update_trainer_inventory(db, user.id, **delta)

    await query.answer(f"✅ Purchased {name}!")

    text = (
        f"🎒 <b>Trainer Bag — {esc(user.first_name)}</b>\n\n"
        f"💰 <b>PokéDollars:</b> <code>{trainer['coins']}</code>\n"
        f"🔴 <b>Pokéballs:</b> <code>{trainer['pokeballs']}</code>\n"
        f"🔵 <b>Great Balls:</b> <code>{trainer['greatballs']}</code>\n"
        f"🟡 <b>Ultra Balls:</b> <code>{trainer['ultraballs']}</code>\n"
        f"🫐 <b>Razz Berries:</b> <code>{trainer['razz_berries']}</code>\n\n"
        f"🛒 <b>Poké Mart (Tap below to buy supplies):</b>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 5x Pokéball (50 💰)", callback_data="shop|buy|pb"),
            InlineKeyboardButton("🔵 3x Great Ball (100 💰)", callback_data="shop|buy|gb"),
        ],
        [
            InlineKeyboardButton("🟡 2x Ultra Ball (200 💰)", callback_data="shop|buy|ub"),
            InlineKeyboardButton("🫐 3x Razz Berry (75 💰)", callback_data="shop|buy|rb"),
        ],
        [
            InlineKeyboardButton("🌿 Go Hunting (/wild)", callback_data="w|hunt_again"),
        ],
    ])

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def box_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    page = 1
    if context.args and context.args[0].isdigit():
        page = max(1, int(context.args[0]))

    db: aiosqlite.Connection = context.application.bot_data["db"]
    pokemon_list, total = await get_caught_pokemon_page(db, user.id, page=page, per_page=8)

    max_pages = max(1, (total + 7) // 8)
    if total == 0:
        await message.reply_text(
            "📦 <b>Your Pokémon Box is currently empty!</b>\n\nUse <b>/wild</b> to encounter and catch your first Pokémon!",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📦 <b>{esc(user.first_name)}'s Pokémon Box</b> (Total: {total} | Page {page}/{max_pages})\n"]
    for poke in pokemon_list:
        shiny_icon = "✨ " if poke["is_shiny"] else ""
        iv_score = iv_rating(poke["ivs"])
        lines.append(
            f"• #{poke['pokemon_id']:03d} <b>{shiny_icon}{esc(poke['nickname'])}</b> (Lv. {poke['level']})\n"
            f"   Nature: <i>{poke['nature']}</i> | IV: {iv_score}"
        )

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"box|page|{page-1}"))
    if page < max_pages:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"box|page|{page+1}"))

    kb_rows = [nav_buttons] if nav_buttons else []
    kb_rows.append([InlineKeyboardButton("🌿 Catch More (/wild)", callback_data="w|hunt_again")])

    await message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))


async def on_box_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split("|")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    user = update.effective_user
    if not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]
    pokemon_list, total = await get_caught_pokemon_page(db, user.id, page=page, per_page=8)
    max_pages = max(1, (total + 7) // 8)

    lines = [f"📦 <b>{esc(user.first_name)}'s Pokémon Box</b> (Total: {total} | Page {page}/{max_pages})\n"]
    for poke in pokemon_list:
        shiny_icon = "✨ " if poke["is_shiny"] else ""
        iv_score = iv_rating(poke["ivs"])
        lines.append(
            f"• #{poke['pokemon_id']:03d} <b>{shiny_icon}{esc(poke['nickname'])}</b> (Lv. {poke['level']})\n"
            f"   Nature: <i>{poke['nature']}</i> | IV: {iv_score}"
        )

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"box|page|{page-1}"))
    if page < max_pages:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"box|page|{page+1}"))

    kb_rows = [nav_buttons] if nav_buttons else []
    kb_rows.append([InlineKeyboardButton("🌿 Catch More (/wild)", callback_data="w|hunt_again")])

    await query.edit_message_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))
