import html
import logging
from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import PokeAPI
from utility_helpers import esc, pretty

logger = logging.getLogger(__name__)


async def inline_pokemon_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.inline_query:
        return
    query = update.inline_query.query.strip().casefold()
    api: PokeAPI = context.application.bot_data["api"]

    names = await api.pokemon_names()
    if not names:
        await update.inline_query.answer([], cache_time=60)
        return

    # Filter matching names or default to popular starters / legendaries if empty
    if not query:
        matched_names = ["pikachu", "charizard", "mewtwo", "lucario", "gengar", "eevee", "greninja", "rayquaza", "bulbasaur", "squirtle"]
    else:
        matched_names = [name for name in names if query in name.casefold()][:15]

    results = []
    for index, name in enumerate(matched_names):
        p = await api.pokemon(name)
        if not p or not isinstance(p, dict):
            continue

        pid = p["id"]
        p_name = pretty(p["name"])
        types = [pretty(t["type"]["name"]) for t in p.get("types", [])]
        type_str = " / ".join(types)

        stats = {x["stat"]["name"]: x["base_stat"] for x in p.get("stats", [])}
        hp = stats.get("hp", 0)
        atk = stats.get("attack", 0)
        dfn = stats.get("defense", 0)
        spa = stats.get("special-attack", 0)
        spd = stats.get("special-defense", 0)
        spe = stats.get("speed", 0)
        total_stat = sum(stats.values())

        sprites = p.get("sprites", {})
        other = sprites.get("other", {})
        art = other.get("official-artwork", {}).get("front_default") or sprites.get("front_default")

        description = f"Type: {type_str} | Total: {total_stat} | HP:{hp} Atk:{atk} Def:{dfn}"

        text = (
            f"<b>⚡ #{pid:04d} {esc(p_name)}</b>\n"
            f"<b>Type:</b> <code>{esc(type_str)}</code>\n"
            f"<b>Height:</b> {p.get('height', 0)/10:.1f} m | <b>Weight:</b> {p.get('weight', 0)/10:.1f} kg\n\n"
            f"<b>📊 Base Stats (Total: {total_stat}):</b>\n"
            f"• HP: <code>{hp}</code>\n"
            f"• Attack: <code>{atk}</code>\n"
            f"• Defense: <code>{dfn}</code>\n"
            f"• Sp. Atk: <code>{spa}</code>\n"
            f"• Sp. Def: <code>{spd}</code>\n"
            f"• Speed: <code>{spe}</code>\n"
        )
        if art:
            text = f'<a href="{esc(art)}">&#8205;</a>' + text

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔍 View in Pokédex", switch_inline_query_current_chat=name),
            ]
        ])

        results.append(
            InlineQueryResultArticle(
                id=f"poke_{pid}_{index}",
                title=f"#{pid:03d} {p_name} ({type_str})",
                description=description,
                thumbnail_url=art,
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                ),
                reply_markup=kb,
            )
        )

    await update.inline_query.answer(results[:15], cache_time=300)
