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

from database import (
    get_caught_pokemon_by_id,
    get_trainer,
    record_discovery,
    transfer_caught_pokemon,
    update_trainer_inventory,
)
from utility_helpers import esc, pretty

logger = logging.getLogger(__name__)

PENDING_TRADES: dict[str, dict[str, Any]] = {}


async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /trade <box_id> — offer a Pokémon from your box for trade."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "🔄 <b>Pokémon Trade Center</b>\n\n"
            "Usage: <code>/trade &lt;box_id&gt;</code>\n"
            "• Lists your Pokémon on the trade board\n"
            "• Other trainers can accept the trade offer\n\n"
            "Example: <code>/trade 5</code> (offer box #5 for trade)\n"
            "View your box with <b>/box</b> to find your box IDs.",
            parse_mode=ParseMode.HTML,
        )
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]

    try:
        box_id = int(args[0])
    except ValueError:
        await message.reply_text("❌ Box ID must be a number. Check /box for your IDs.")
        return

    pokemon = await get_caught_pokemon_by_id(db, box_id)
    if not pokemon:
        await message.reply_text("❌ Pokémon not found in any box.")
        return
    if pokemon["user_id"] != user.id:
        await message.reply_text("❌ That Pokémon doesn't belong to you!")
        return

    trade_id = f"trade_{user.id}_{box_id}_{random.randint(1000, 9999)}"
    PENDING_TRADES[trade_id] = {
        "sender_id": user.id,
        "sender_name": user.first_name,
        "caught_id": box_id,
        "pokemon_id": pokemon["pokemon_id"],
        "nickname": pokemon["nickname"],
        "level": pokemon["level"],
        "nature": pokemon["nature"],
        "is_shiny": pokemon["is_shiny"],
        "status": "open",
    }

    # Store in DB trade_offers table
    await db.execute(
        """INSERT INTO trade_offers(sender_id, receiver_id, sender_caught_id, status)
           VALUES(?, 0, ?, 'pending')""",
        (user.id, box_id),
    )
    await db.commit()

    shiny_tag = "✨ " if pokemon["is_shiny"] else ""
    text = (
        f"🔄 <b>TRADE OFFER POSTED</b> 🔄\n\n"
        f"Trainer <b>{esc(user.first_name)}</b> is offering:\n\n"
        f"🔹 <b>#{pokemon['pokemon_id']:03d} {shiny_tag}{esc(pokemon['nickname'])}</b>\n"
        f"   Level: {pokemon['level']} | Nature: {pokemon['nature']}\n\n"
        f"Any trainer can accept this offer below!"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept Trade", callback_data=f"trade|accept|{trade_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"trade|cancel|{trade_id}"),
        ]
    ])

    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    if len(parts) < 3:
        return

    action = parts[1]
    trade_id = parts[2]

    trade = PENDING_TRADES.get(trade_id)
    if not trade:
        await query.answer("This trade offer has expired!", show_alert=True)
        return

    user = update.effective_user
    if not user:
        return

    db: aiosqlite.Connection = context.application.bot_data["db"]

    if action == "cancel":
        if user.id != trade["sender_id"]:
            await query.answer("Only the trade poster can cancel.", show_alert=True)
            return

        PENDING_TRADES.pop(trade_id, None)
        await db.execute(
            """UPDATE trade_offers SET status='cancelled'
               WHERE sender_id=? AND sender_caught_id=? AND status='pending'""",
            (trade["sender_id"], trade["caught_id"]),
        )
        await db.commit()
        await query.answer("Trade cancelled.")
        try:
            await query.edit_message_text(
                f"🔄 Trade offer from <b>{esc(trade['sender_name'])}</b> was <b>cancelled</b>.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if action == "accept":
        if user.id == trade["sender_id"]:
            await query.answer("You can't accept your own trade!", show_alert=True)
            return

        if trade["status"] != "open":
            await query.answer("This trade has already been completed!", show_alert=True)
            return

        # Verify the Pokémon still belongs to the sender
        pokemon = await get_caught_pokemon_by_id(db, trade["caught_id"])
        if not pokemon or pokemon["user_id"] != trade["sender_id"]:
            await query.answer("This Pokémon is no longer available.", show_alert=True)
            PENDING_TRADES.pop(trade_id, None)
            return

        # Transfer ownership
        success = await transfer_caught_pokemon(db, trade["caught_id"], user.id)
        if not success:
            await query.answer("Trade failed! The Pokémon may no longer be available.", show_alert=True)
            return

        trade["status"] = "completed"
        PENDING_TRADES.pop(trade_id, None)

        # Award discovery to receiver
        await record_discovery(db, user.id, trade["pokemon_id"])

        # Update the DB trade_offers
        await db.execute(
            """UPDATE trade_offers SET receiver_id=?, status='accepted'
               WHERE sender_id=? AND sender_caught_id=? AND status='pending'""",
            (user.id, trade["sender_id"], trade["caught_id"]),
        )
        await db.commit()

        shiny_tag = "✨ " if trade["is_shiny"] else ""

        text = (
            f"🔄 <b>TRADE COMPLETED!</b> 🔄\n\n"
            f"<b>#{trade['pokemon_id']:03d} {shiny_tag}{esc(trade['nickname'])}</b> "
            f"(Lv. {trade['level']})\n\n"
            f"From: <b>{esc(trade['sender_name'])}</b>\n"
            f"To: <b>{esc(user.first_name)}</b>\n\n"
            f"The Pokémon has been transferred to {esc(user.first_name)}'s box!"
        )

        await query.answer("Trade successful!")
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    # Counter-offer: user picks one of their own Pokémon to swap
    if action == "counter" and len(parts) >= 4:
        counter_box_id = int(parts[3])
        counter_pokemon = await get_caught_pokemon_by_id(db, counter_box_id)
        if not counter_pokemon or counter_pokemon["user_id"] != user.id:
            await query.answer("That Pokémon doesn't belong to you!", show_alert=True)
            return

        # Verify original Pokémon still available
        orig_pokemon = await get_caught_pokemon_by_id(db, trade["caught_id"])
        if not orig_pokemon or orig_pokemon["user_id"] != trade["sender_id"]:
            await query.answer("The original Pokémon is no longer available.", show_alert=True)
            return

        # Execute swap
        await transfer_caught_pokemon(db, trade["caught_id"], user.id)
        await transfer_caught_pokemon(db, counter_box_id, trade["sender_id"])

        await record_discovery(db, user.id, trade["pokemon_id"])
        await record_discovery(db, trade["sender_id"], counter_pokemon["pokemon_id"])

        PENDING_TRADES.pop(trade_id, None)

        shiny_a = "✨ " if trade["is_shiny"] else ""
        shiny_b = "✨ " if counter_pokemon["is_shiny"] else ""

        text = (
            f"🔄 <b>TRADE SWAP COMPLETED!</b> 🔄\n\n"
            f"<b>{esc(trade['sender_name'])}</b> traded "
            f"<b>#{trade['pokemon_id']:03d} {shiny_a}{esc(trade['nickname'])}</b>\n"
            f"<b>{esc(user.first_name)}</b> traded "
            f"<b>#{counter_pokemon['pokemon_id']:03d} {shiny_b}{esc(counter_pokemon['nickname'])}</b>\n\n"
            f"Both Pokémon have been swapped!"
        )

        await query.answer("Trade swap successful!")
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
