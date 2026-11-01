import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import aiosqlite
import httpx

from api_client import PokeAPI
from database import (
    SCHEMA_VERSION,
    add_caught_pokemon,
    get_caught_pokemon_page,
    get_trainer,
    initialize_database,
    update_trainer_inventory,
)
from wild_handlers import iv_rating, SHOP_PRICES
from battle_handlers import calculate_battle_damage, render_hp_bar


class NewFeaturesDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        await initialize_database(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_get_trainer_initializes_defaults(self):
        trainer = await get_trainer(self.db, 42)
        self.assertEqual(trainer["coins"], 150)
        self.assertEqual(trainer["pokeballs"], 10)
        self.assertEqual(trainer["greatballs"], 3)
        self.assertEqual(trainer["ultraballs"], 1)
        self.assertEqual(trainer["razz_berries"], 5)

    async def test_update_trainer_inventory_adjusts_items_and_coins(self):
        await get_trainer(self.db, 42)
        updated = await update_trainer_inventory(
            self.db,
            42,
            coins_delta=50,
            pokeballs_delta=-2,
            greatballs_delta=1,
            ultraballs_delta=0,
            razz_berries_delta=-1,
        )
        self.assertEqual(updated["coins"], 200)
        self.assertEqual(updated["pokeballs"], 8)
        self.assertEqual(updated["greatballs"], 4)
        self.assertEqual(updated["razz_berries"], 4)

    async def test_caught_pokemon_records_and_pages(self):
        ivs = (31, 31, 31, 20, 20, 31)
        row_id = await add_caught_pokemon(
            self.db,
            user_id=42,
            pokemon_id=25,
            nickname="Pikachu",
            level=25,
            nature="Jolly",
            ivs=ivs,
            is_shiny=True,
        )
        self.assertGreater(row_id, 0)

        pokemon_list, total = await get_caught_pokemon_page(self.db, 42, page=1, per_page=5)
        self.assertEqual(total, 1)
        self.assertEqual(len(pokemon_list), 1)
        first = pokemon_list[0]
        self.assertEqual(first["pokemon_id"], 25)
        self.assertEqual(first["nickname"], "Pikachu")
        self.assertEqual(first["level"], 25)
        self.assertEqual(first["nature"], "Jolly")
        self.assertEqual(first["ivs"], ivs)
        self.assertTrue(first["is_shiny"])


class FastL1CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        await initialize_database(self.db)
        self.client = httpx.AsyncClient()
        self.api = PokeAPI(self.client, self.db, ttl_sec=600)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.db.close()

    async def test_l1_in_memory_cache_prevents_duplicate_http_or_db_calls(self):
        mock_response = httpx.Response(200, json={"id": 25, "name": "pikachu"})
        with patch.object(self.client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            first = await self.api.pokemon(25)
            self.assertEqual(first["name"], "pikachu")
            self.assertEqual(mock_get.call_count, 1)

            # Second call should be served directly from in-memory L1 cache
            second = await self.api.pokemon(25)
            self.assertEqual(second["name"], "pikachu")
            self.assertEqual(mock_get.call_count, 1)

    async def test_cry_url_construction(self):
        url = await self.api.cry_url(25)
        self.assertIn("cries", url)
        self.assertIn("25.ogg", url)


class GameplayMathTests(unittest.TestCase):
    def test_iv_rating_calculation(self):
        perfect_ivs = (31, 31, 31, 31, 31, 31)
        rating_perfect = iv_rating(perfect_ivs)
        self.assertIn("Perfect", rating_perfect)
        self.assertIn("100%", rating_perfect)

        low_ivs = (5, 5, 5, 5, 5, 5)
        rating_low = iv_rating(low_ivs)
        self.assertIn("Decent", rating_low)

    def test_battle_damage_calculation(self):
        dmg = calculate_battle_damage(attacker_lvl=50, atk_stat=100, def_stat=80, power=90)
        self.assertGreater(dmg, 10)
        self.assertLess(dmg, 200)

    def test_render_hp_bar(self):
        bar_full = render_hp_bar(100, 100)
        self.assertEqual(bar_full, "[██████████] 100/100 HP")

        bar_half = render_hp_bar(50, 100)
        self.assertEqual(bar_half, "[█████░░░░░] 50/100 HP")

        bar_empty = render_hp_bar(0, 100)
        self.assertEqual(bar_empty, "[░░░░░░░░░░] 0/100 HP")

    def test_shop_prices_integrity(self):
        self.assertIn("pb", SHOP_PRICES)
        self.assertIn("gb", SHOP_PRICES)
        self.assertIn("ub", SHOP_PRICES)
        self.assertIn("rb", SHOP_PRICES)

    def test_evaluate_competitive_role(self):
        from meta_handlers import evaluate_competitive_role
        sweeper_stats = {"attack": 130, "speed": 110, "special-attack": 60, "hp": 80, "defense": 70, "special-defense": 70}
        role, nature, item, evs = evaluate_competitive_role(sweeper_stats)
        self.assertIn("Physical Sweeper", role)
        self.assertIn("Jolly", nature)


class AdvancedEconomyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        await initialize_database(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_egg_incubation_and_walking(self):
        from database import add_trainer_egg, get_trainer_eggs, walk_trainer_eggs
        egg_id = await add_trainer_egg(self.db, user_id=10, species_id=175, steps_needed=10)
        self.assertGreater(egg_id, 0)

        eggs = await get_trainer_eggs(self.db, user_id=10)
        self.assertEqual(len(eggs), 1)
        self.assertEqual(eggs[0]["species_id"], 175)

        # Walk 5 steps - shouldn't hatch yet
        hatched = await walk_trainer_eggs(self.db, user_id=10, steps=5)
        self.assertEqual(len(hatched), 0)

        # Walk remaining 5 steps - should hatch!
        hatched = await walk_trainer_eggs(self.db, user_id=10, steps=5)
        self.assertEqual(len(hatched), 1)
        self.assertEqual(hatched[0]["species_id"], 175)

        # Incubator should now be empty
        eggs_after = await get_trainer_eggs(self.db, user_id=10)
        self.assertEqual(len(eggs_after), 0)

    async def test_market_listing_and_purchase(self):
        from database import (
            add_caught_pokemon,
            create_market_listing,
            get_market_listings,
            buy_market_listing,
            get_trainer,
            update_trainer_inventory,
            get_caught_pokemon_by_id,
        )
        # Seller (user 1) catches a pokemon
        caught_id = await add_caught_pokemon(
            self.db, user_id=1, pokemon_id=4, nickname="Charmander", level=5, nature="Adamant", ivs=(30, 30, 30, 30, 30, 30)
        )
        # List on market for 100 coins
        listing_id = await create_market_listing(self.db, seller_id=1, caught_id=caught_id, price=100)
        self.assertGreater(listing_id, 0)

        listings = await get_market_listings(self.db)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["nickname"], "Charmander")

        # Buyer (user 2) has 150 coins by default
        success, msg = await buy_market_listing(self.db, buyer_id=2, listing_id=listing_id)
        self.assertTrue(success)

        # Check ownership transferred to user 2
        poke = await get_caught_pokemon_by_id(self.db, caught_id)
        self.assertEqual(poke["user_id"], 2)

        # Check coins transferred
        buyer = await get_trainer(self.db, 2)
        seller = await get_trainer(self.db, 1)
        self.assertEqual(buyer["coins"], 50)  # 150 - 100
        self.assertEqual(seller["coins"], 250)  # 150 + 100

    async def test_pokemon_evolution(self):
        from database import add_caught_pokemon, evolve_caught_pokemon, get_caught_pokemon_by_id
        caught_id = await add_caught_pokemon(
            self.db, user_id=1, pokemon_id=1, nickname="Bulbasaur", level=16, nature="Modest", ivs=(20, 20, 20, 20, 20, 20)
        )
        success = await evolve_caught_pokemon(self.db, caught_id, new_pid=2, new_nickname="Ivysaur")
        self.assertTrue(success)

        poke = await get_caught_pokemon_by_id(self.db, caught_id)
        self.assertEqual(poke["pokemon_id"], 2)
        self.assertEqual(poke["nickname"], "Ivysaur")
        self.assertEqual(poke["level"], 18)

    async def test_gym_badges_collection(self):
        from database import award_gym_badge, get_user_badges
        awarded = await award_gym_badge(self.db, user_id=5, gym_id=1)
        self.assertTrue(awarded)

        badges = await get_user_badges(self.db, user_id=5)
        self.assertEqual(badges, [1])

        # Second badge
        await award_gym_badge(self.db, user_id=5, gym_id=2)
        badges_after = await get_user_badges(self.db, user_id=5)
        self.assertEqual(badges_after, [1, 2])

    async def test_nickname_and_release_pokemon(self):
        from database import add_caught_pokemon, update_caught_nickname, delete_caught_pokemon, get_caught_pokemon_by_id
        caught_id = await add_caught_pokemon(
            self.db, user_id=7, pokemon_id=25, nickname="Pikachu", level=10, nature="Timid", ivs=(31, 31, 31, 31, 31, 31)
        )
        # Nickname
        renamed = await update_caught_nickname(self.db, user_id=7, caught_id=caught_id, new_nickname="Volt")
        self.assertTrue(renamed)
        poke = await get_caught_pokemon_by_id(self.db, caught_id)
        self.assertEqual(poke["nickname"], "Volt")

        # Release
        deleted = await delete_caught_pokemon(self.db, user_id=7, caught_id=caught_id)
        self.assertTrue(deleted)
        poke_after = await get_caught_pokemon_by_id(self.db, caught_id)
        self.assertIsNone(poke_after)

    def test_ascii_stat_bar(self):
        from trainer_card_handlers import ascii_stat_bar
        bar = ascii_stat_bar(100)
        self.assertIn("100", bar)
        self.assertIn("█", bar)




class TradeAndWtpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        await initialize_database(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_trade_lifecycle_transfer(self):
        from database import add_caught_pokemon, get_caught_pokemon_by_id
        from trade_handlers import PENDING_TRADES, on_trade_callback

        caught_id = await add_caught_pokemon(
            self.db, user_id=101, pokemon_id=6, nickname="Charizard", level=36, nature="Brave", ivs=(25, 25, 25, 25, 25, 25)
        )
        trade_id = "test_trade_101"
        PENDING_TRADES[trade_id] = {
            "sender_id": 101,
            "sender_name": "Ash",
            "caught_id": caught_id,
            "pokemon_id": 6,
            "nickname": "Charizard",
            "level": 36,
            "nature": "Brave",
            "is_shiny": False,
            "status": "open",
        }

        # Mock user 202 accepting trade
        update = MagicMock()
        update.effective_user.id = 202
        update.effective_user.first_name = "Gary"
        query = AsyncMock()
        query.data = f"trade|accept|{trade_id}"
        update.callback_query = query

        context = MagicMock()
        context.application.bot_data = {"db": self.db}

        await on_trade_callback(update, context)

        # Check ownership is now Gary (202)
        poke = await get_caught_pokemon_by_id(self.db, caught_id)
        self.assertEqual(poke["user_id"], 202)
        self.assertNotIn(trade_id, PENDING_TRADES)

    async def test_wtp_callback_correct_answer(self):
        from wtp_handlers import on_wtp_callback

        update = MagicMock()
        update.effective_user.id = 303
        update.effective_user.first_name = "Red"
        query = AsyncMock()
        query.data = "wtp|test_qid_1|Pikachu"
        update.callback_query = query

        context = MagicMock()
        context.application.bot_data = {
            "db": self.db,
            "api": MagicMock(),
            "test_qid_1": {
                "pid": 25,
                "correct": "Pikachu",
                "types": "Electric",
            }
        }

        await on_wtp_callback(update, context)
        # Check quiz answered and removed from bot_data
        self.assertNotIn("test_qid_1", context.application.bot_data)
        query.edit_message_text.assert_called_once()
        self.assertIn("IT'S PIKACHU!", query.edit_message_text.call_args[0][0])

    async def test_generation_cmd_formatting(self):
        from bot import generation_cmd

        update = MagicMock()
        message = AsyncMock()
        update.effective_message = message

        context = MagicMock()
        context.args = ["1"]
        mock_api = AsyncMock()
        mock_api.generation.return_value = {
            "name": "generation-i",
            "main_region": {"name": "kanto"},
            "pokemon_species": [{"name": "bulbasaur"}, {"name": "charmander"}, {"name": "squirtle"}],
            "moves": [{"name": "pound"}],
            "types": [],
            "version_groups": [{"name": "red-blue"}],
        }
        context.application.bot_data = {"api": mock_api}

        with patch("bot.update_message", return_value=message):
            await generation_cmd(update, context)
        message.reply_text.assert_called_once()
        call_text = message.reply_text.call_args[0][0]
        self.assertIn("GENERATION I", call_text)
        self.assertIn("Kanto", call_text)
        self.assertIn("Bulbasaur", call_text)


if __name__ == "__main__":
    unittest.main()



