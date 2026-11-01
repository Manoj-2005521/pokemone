import logging
from typing import Sequence
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import PokeAPI
from utility_helpers import esc, pretty

logger = logging.getLogger(__name__)


def evaluate_competitive_role(stats: dict[str, int]) -> tuple[str, str, str, str]:
    atk = stats.get("attack", 50)
    spa = stats.get("special-attack", 50)
    spe = stats.get("speed", 50)
    hp = stats.get("hp", 50)
    dfn = stats.get("defense", 50)
    spd = stats.get("special-defense", 50)

    if spe >= 100 and atk >= 110:
        return (
            "Fast Physical Sweeper",
            "Jolly (+Spe, -SpA) or Adamant (+Atk, -SpA)",
            "Choice Band / Life Orb / Focus Sash",
            "252 Atk / 4 SpD / 252 Spe",
        )
    elif spe >= 100 and spa >= 110:
        return (
            "Fast Special Sweeper",
            "Timid (+Spe, -Atk) or Modest (+SpA, -Atk)",
            "Choice Specs / Life Orb / Focus Sash",
            "252 SpA / 4 SpD / 252 Spe",
        )
    elif hp >= 95 and dfn >= 95:
        return (
            "Physical Tank / Wall",
            "Impish (+Def, -SpA) or Relaxed (+Def, -Spe)",
            "Leftovers / Rocky Helmet",
            "252 HP / 252 Def / 4 SpD",
        )
    elif hp >= 95 and spd >= 95:
        return (
            "Special Wall / Support",
            "Calm (+SpD, -Atk) or Careful (+SpD, -SpA)",
            "Assault Vest / Leftovers",
            "252 HP / 4 Def / 252 SpD",
        )
    elif atk >= spa:
        return (
            "Bulky Physical Attacker",
            "Adamant (+Atk, -SpA)",
            "Assault Vest / Choice Band",
            "252 HP / 252 Atk / 4 Spe",
        )
    else:
        return (
            "Bulky Special Attacker",
            "Modest (+SpA, -Atk)",
            "Assault Vest / Life Orb",
            "252 HP / 252 SpA / 4 Spe",
        )


async def meta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "🏆 <b>Competitive Smogon & VGC Meta Advisor</b>\n\n"
            "Usage: <code>/meta &lt;pokemon&gt;</code> or <code>/smogon &lt;pokemon&gt;</code>\n"
            "Example: <code>/meta garchomp</code> or <code>/smogon dragapult</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name_or_id = "-".join(" ".join(args).strip().casefold().split())
    api: PokeAPI = context.application.bot_data["api"]

    p = await api.pokemon(name_or_id)
    if not p:
        await message.reply_text("❌ Pokémon not found. Check spelling and try again.")
        return

    p_name = pretty(p["name"])
    types = " / ".join(pretty(t["type"]["name"]) for t in p.get("types", []))
    abilities = [pretty(a["ability"]["name"]) + (" (Hidden)" if a.get("is_hidden") else "") for a in p.get("abilities", [])]

    stats = {x["stat"]["name"]: x["base_stat"] for x in p.get("stats", [])}
    total_bst = sum(stats.values())

    role, nature, item, evs = evaluate_competitive_role(stats)

    # Pick top signature/competitive moves from movepool
    moves = [pretty(x["move"]["name"]) for x in p.get("moves", [])]
    recommended_moves = moves[:4] if moves else ["Tackle", "Protect"]

    tier_est = "Ubers" if total_bst >= 650 else "OU (OverUsed)" if total_bst >= 520 else "UU / RU"

    text = (
        f"🏆 <b>COMPETITIVE META BUILD: {esc(p_name).upper()}</b> 🏆\n\n"
        f"<b>Tier:</b> <code>{esc(tier_est)}</code> | <b>Type:</b> <code>{esc(types)}</code>\n"
        f"<b>Base Stat Total:</b> <code>{total_bst}</code>\n\n"
        f"🎯 <b>Primary Role:</b> <b>{esc(role)}</b>\n"
        f"💼 <b>Recommended Item:</b> <code>{esc(item)}</code>\n"
        f"🧬 <b>Recommended Nature:</b> <code>{esc(nature)}</code>\n"
        f"⚡ <b>EV Spread:</b> <code>{esc(evs)}</code>\n"
        f"🌟 <b>Best Ability:</b> <code>{esc(abilities[0] if abilities else 'Standard')}</code>\n\n"
        f"⚔️ <b>Recommended Tournament Moveset:</b>\n"
        + "\n".join(f"• <code>{esc(m)}</code>" for m in recommended_moves)
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)
