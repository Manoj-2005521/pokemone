import logging
import random
from typing import Optional
from telegram import Message, Poll, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import PokeAPI
from database import record_discovery
from utility_helpers import esc, pretty
from wild_handlers import wild_cmd

logger = logging.getLogger(__name__)


async def pollquiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    api: PokeAPI = context.application.bot_data["api"]
    names = await api.pokemon_names()
    if not names:
        await message.reply_text("❌ Pokémon catalog unavailable.")
        return

    # Select random Pokémon for quiz
    pid = random.randint(1, 1025)
    p = await api.pokemon(pid)
    species = await api.species(pid)

    if not p or not species:
        await message.reply_text("❌ Could not generate quiz poll. Try again!")
        return

    correct_name = pretty(p["name"])
    raw_name = p["name"]

    types = [pretty(t["type"]["name"]) for t in p.get("types", [])]
    type_str = " / ".join(types)

    # Question types
    q_mode = random.choice(["type", "name"])

    if q_mode == "type":
        question = f"❓ What is the primary typing of #{pid:03d} {correct_name}?"
        correct_answer = types[0]
        all_types = ["Normal", "Fire", "Water", "Grass", "Electric", "Ice", "Fighting", "Poison", "Ground", "Flying", "Psychic", "Bug", "Rock", "Ghost", "Dragon", "Steel", "Fairy", "Dark"]
        decoy_types = [t for t in all_types if t != correct_answer]
        options = [correct_answer] + random.sample(decoy_types, 3)
        random.shuffle(options)
        correct_idx = options.index(correct_answer)
        explanation = f"{correct_name} is a {type_str} type Pokémon!"
    else:
        question = f"❓ Which Pokémon has the National Pokédex number #{pid:03d} ({type_str} type)?"
        correct_answer = correct_name
        decoys = random.sample([pretty(n) for n in names if n != raw_name], 3)
        options = [correct_answer] + decoys
        random.shuffle(options)
        correct_idx = options.index(correct_answer)
        explanation = f"National Dex #{pid:03d} belongs to {correct_name}!"

    await context.bot.send_poll(
        chat_id=message.chat_id,
        question=question[:300],
        options=options[:4],
        type=Poll.QUIZ,
        correct_option_id=correct_idx,
        is_anonymous=False,
        explanation=explanation[:200],
        open_period=45,
    )


async def spawn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Trigger a wild spawn encounter in the current group/chat
    await wild_cmd(update, context)
