import unittest
import os
import json
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from typing import Any, cast
from pathlib import Path

import aiosqlite
import httpx
from telegram import Message
from telegram.ext import ApplicationHandlerStop, ContextTypes, TypeHandler

from bot import (
    DB_PATH,
    PokeAPI,
    dex_badges,
    evolution_stages,
    build_app,
    calculate_stat,
    main_menu_kb,
    pokemon_menu_kb,
    MAX_POKEMON,
    matchup_buckets,
    moves_kb,
    evolution_kb,
    pokemon_caption,
    pokemon_kb,
    open_pokemon,
    dex_cmd,
    rate_limit_update,
    health_cmd,
    error_handler,
    configured_admin_user_id,
    suggest_pokemon_names,
    team_cmd,
    parse_fx_input,
    quote_parts,
    record_discovery,
    _find_apod_fallback_image,
    post_init,
    post_shutdown,
    send_apod,
    send_fact,
    send_fx_conversion,
    send_iv_estimate,
    send_quote,
    send_definition,
    send_weather,
    utils_menu_kb,
    weather_description,
    wiki_summary_parts,
    quiz_kb,
    new_quiz,
)
from api_client import PokeAPI as ApiClient
from backup_db import main as create_manual_backup
from database import (
    SCHEMA,
    SCHEMA_VERSION,
    backup_database,
    backup_to_directories,
    configured_backup_directories,
    database_version,
    initialize_database,
    prune_backups,
)
from rate_limiter import UserRateLimiter


class PokédexFeatureTests(unittest.TestCase):
    def test_database_path_is_relative_to_the_bot_file(self):
        self.assertEqual(Path(DB_PATH).resolve().parent, Path(__file__).resolve().parent.parent)

    def test_utilities_menu_exposes_each_module(self):
        keyboard = utils_menu_kb()
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        main_callbacks = [
            button.callback_data
            for row in main_menu_kb().inline_keyboard
            for button in row
        ]

        for action in ("wiki", "define", "quote", "fact", "space", "fx", "weather"):
            self.assertIn(f"utils|{action}", callbacks)
        self.assertNotIn("utils|berry", callbacks)
        self.assertNotIn("utils|iv", callbacks)
        self.assertEqual(main_callbacks, ["menu|pokemon", "menu|utils"])
        self.assertEqual(len(main_menu_kb().inline_keyboard), 1)

    def test_pokemon_menu_keeps_pokemon_features_separate_from_utilities(self):
        callbacks = [
            button.callback_data
            for row in pokemon_menu_kb().inline_keyboard
            for button in row
            if isinstance(button.callback_data, str)
        ]

        for action in (
            "search", "random", "quiz", "daily", "dex", "team", "favorites",
            "shinies", "profile", "ranking", "challenge", "types", "berry", "iv",
        ):
            self.assertTrue(any(
                callback == f"menu|{action}" or callback.startswith(f"menu|{action}|")
                for callback in callbacks
            ))
        self.assertNotIn("utils|fx", callbacks)
        self.assertIn("menu|home", callbacks)

    def test_iv_estimates_use_hp_and_nature_formulas(self):
        self.assertEqual(calculate_stat(35, 31, 252, 50, "hp"), 142)
        self.assertEqual(calculate_stat(90, 31, 252, 50, "speed", 1.1), 156)
        with self.assertRaises(ValueError):
            calculate_stat(90, 32, 252, 50, "speed")

    def test_all_inline_callbacks_fit_telegram_payload_limit(self):
        keyboards = (
            main_menu_kb(),
            pokemon_menu_kb(),
            utils_menu_kb(),
            pokemon_kb(MAX_POKEMON, True, False),
            moves_kb(MAX_POKEMON, 99),
            evolution_kb(MAX_POKEMON, 99, 100),
            quiz_kb("0123456789", ["One", "Two", "Three", "Four"], "normal"),
        )
        for keyboard in keyboards:
            for row in keyboard.inline_keyboard:
                self.assertLessEqual(len(row), 2)
                for button in row:
                    if isinstance(button.callback_data, str):
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    def test_utility_commands_are_registered(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "123456:test-token"}):
            app = build_app()

        commands = {
            command
            for handlers in app.handlers.values()
            for handler in handlers
            for command in (getattr(handler, "commands", None) or set())
        }
        self.assertTrue({
            "wiki", "define", "quote", "fact", "space", "fx", "weather", "iv",
            "health",
        }.issubset(commands))
        self.assertTrue(any(
            isinstance(handler, TypeHandler)
            for handler in app.handlers[-1]
        ))

    def test_matchup_buckets_distinguish_dual_type_multipliers(self):
        buckets = matchup_buckets({
            "ice": 4.0,
            "rock": 2.0,
            "water": 0.5,
            "grass": 0.25,
            "electric": 0.0,
            "normal": 1.0,
        })

        self.assertEqual(buckets["4x weak"], ["Ice"])
        self.assertEqual(buckets["2x weak"], ["Rock"])
        self.assertEqual(buckets["0.5x resist"], ["Water"])
        self.assertEqual(buckets["0.25x resist"], ["Grass"])
        self.assertEqual(buckets["immune"], ["Electric"])

    def test_evolution_stages_keep_branches_on_the_same_stage(self):
        chain = {
            "species": {"name": "eevee"},
            "evolves_to": [
                {"species": {"name": "vaporeon"}, "evolves_to": []},
                {"species": {"name": "jolteon"}, "evolves_to": []},
            ],
        }

        self.assertEqual(
            evolution_stages(chain),
            [["Eevee"], ["Vaporeon", "Jolteon"]],
        )

    def test_dex_badges_include_only_reached_milestones(self):
        self.assertEqual(dex_badges(24), ["Beginner"])
        self.assertEqual(dex_badges(100), ["Beginner", "Collector", "Researcher"])

    def test_wiki_summary_is_bounded_and_extracts_image_and_link(self):
        title, extract, page, thumbnail = wiki_summary_parts({
            "title": "Black hole",
            "extract": "x" * 1000,
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Black_hole"}},
            "thumbnail": {"source": "https://upload.wikimedia.org/black-hole.jpg"},
        }, "fallback")

        self.assertEqual(title, "Black hole")
        self.assertLessEqual(len(extract), 650)
        if page is None or thumbnail is None:
            self.fail("Expected Wikipedia links and a thumbnail")
        self.assertTrue(page.endswith("Black_hole"))
        self.assertTrue(thumbnail.endswith("black-hole.jpg"))

    def test_fx_input_rejects_invalid_currencies_and_amounts(self):
        self.assertEqual(parse_fx_input(["usd", "eur", "12.5"]), ("USD", "EUR", 12.5))
        for args in (["US", "EUR", "1"], ["USD", "EUR", "nan"], ["USD", "EUR", "-1"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_fx_input(args)

    def test_fx_input_accepts_currency_names_symbols_and_natural_order(self):
        self.assertEqual(
            parse_fx_input(["100", "US dollars", "to", "euros"]),
            ("USD", "EUR", 100.0),
        )
        self.assertEqual(parse_fx_input(["$100", "to", "€"]), ("USD", "EUR", 100.0))
        self.assertEqual(parse_fx_input(["100", "USD", "EUR"]), ("USD", "EUR", 100.0))

    def test_fx_input_resolves_full_names_from_currency_catalog(self):
        self.assertEqual(
            parse_fx_input(
                ["10", "Swiss Franc", "to", "Japanese Yen"],
                {"CHF": "Swiss Franc", "JPY": "Japanese Yen"},
            ),
            ("CHF", "JPY", 10.0),
        )

    def test_unknown_weather_codes_have_a_readable_fallback(self):
        self.assertEqual(weather_description(0), "Clear sky")
        self.assertEqual(weather_description(999), "Unknown condition (code 999)")

    def test_quote_parser_accepts_primary_and_fallback_shapes(self):
        self.assertEqual(
            quote_parts({"quote": "Primary quote", "author": "Primary author"}),
            ("Primary quote", "Primary author"),
        )
        self.assertEqual(
            quote_parts([{"q": "Fallback quote", "a": "Fallback author"}]),
            ("Fallback quote", "Fallback author"),
        )


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_is_unique_but_daily_progress_is_per_day(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)

            first = await record_discovery(db, 7, 25, "2026-10-03")
            repeated = await record_discovery(db, 7, 25, "2026-10-03")
            next_day = await record_discovery(db, 7, 25, "2026-10-04")

            count = await (await db.execute(
                "SELECT COUNT(*) FROM dex_discoveries WHERE user_id=?",
                (7,),
            )).fetchone()
            daily_counts = await (await db.execute(
                "SELECT day, COUNT(*) FROM daily_discoveries WHERE user_id=? GROUP BY day ORDER BY day",
                (7,),
            )).fetchall()

        self.assertTrue(first)
        self.assertFalse(repeated)
        self.assertFalse(next_day)
        if count is None:
            self.fail("Discovery count query returned no row")
        self.assertEqual(count[0], 1)
        self.assertEqual(daily_counts, [("2026-10-03", 1), ("2026-10-04", 1)])

    async def test_dex_missing_filter_pages_valid_species_ids(self):
        class FakeApi:
            async def pokemon(self, pokemon_id):
                return {"id": pokemon_id, "name": f"species-{pokemon_id}"}

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            await db.execute(
                "INSERT INTO dex_discoveries(user_id,pokemon_id) VALUES(7,2)"
            )
            message = FakeMessage()
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=7),
            )
            context = SimpleNamespace(
                args=["missing", "1"],
                application=SimpleNamespace(
                    bot_data={"db": db, "api": FakeApi()},
                ),
            )

            with patch("bot.update_message", return_value=message):
                await dex_cmd(cast(Any, update), cast(Any, context))

        self.assertIn("Missing Pokémon — page 1/52 (1024 total)", message.text)
        self.assertIn("#1 Species 1", message.text)
        self.assertNotIn("#2 Species 2", message.text)
        self.assertIn("/dex missing 2", message.text)


class UserRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_bucket_limits_each_user_independently_and_refills(self):
        limiter = UserRateLimiter(capacity=2, refill_per_second=1)

        self.assertTrue(limiter.allow(1, now=0))
        self.assertTrue(limiter.allow(1, now=0))
        self.assertFalse(limiter.allow(1, now=0))
        self.assertTrue(limiter.allow(2, now=0))
        self.assertTrue(limiter.allow(1, now=1))
        self.assertTrue(limiter.allow(0, now=1))

    async def test_update_middleware_notifies_and_stops_excess_requests(self):
        limiter = UserRateLimiter(capacity=1, refill_per_second=0.01)

        class FakeMessage:
            reply_text = AsyncMock()

        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=3),
            effective_message=message,
            callback_query=None,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"rate_limiter": limiter}),
        )

        await rate_limit_update(cast(Any, update), cast(Any, context))
        with self.assertRaises(ApplicationHandlerStop):
            await rate_limit_update(cast(Any, update), cast(Any, context))
        with self.assertRaises(ApplicationHandlerStop):
            await rate_limit_update(cast(Any, update), cast(Any, context))

        message.reply_text.assert_awaited_once()


class OperationalHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_command_is_private_and_admin_only(self):
        class FakeMessage:
            chat = SimpleNamespace(type="private")
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        async with aiosqlite.connect(":memory:") as db:
            await initialize_database(db)
            message = FakeMessage()
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=10),
                effective_message=message,
            )
            context = SimpleNamespace(
                application=SimpleNamespace(
                    bot_data={
                        "admin_user_id": 10,
                        "db": db,
                        "api": SimpleNamespace(
                            get=AsyncMock(return_value={"id": 1}),
                        ),
                        "unhandled_error_count": 2,
                    },
                ),
            )

            with patch("bot.update_message", return_value=message):
                await health_cmd(cast(Any, update), cast(Any, context))

        self.assertIn("SQLite: OK", message.text)
        self.assertIn("PokéAPI: OK", message.text)
        self.assertIn("Unhandled errors since startup: 2", message.text)

    async def test_health_command_denies_non_admin_user(self):
        class FakeMessage:
            chat = SimpleNamespace(type="private")
            reply_text = AsyncMock()

        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=11),
            effective_message=message,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "admin_user_id": 10,
                    "db": None,
                    "api": None,
                },
            ),
        )

        with patch("bot.update_message", return_value=message):
            await health_cmd(cast(Any, update), cast(Any, context))

        message.reply_text.assert_awaited_once()
        self.assertIn("only to the configured bot admin", message.reply_text.await_args.args[0])

    async def test_error_handler_sends_throttled_admin_alert(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"admin_user_id": 10}),
            bot=bot,
            error=ValueError("internal detail"),
        )

        with patch("bot.time.monotonic", return_value=1000), self.assertLogs(
            "bot", level="ERROR",
        ):
            for _ in range(8):
                await error_handler(SimpleNamespace(update_id=3), cast(Any, context))

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 10)
        self.assertNotIn("internal detail", bot.send_message.await_args.kwargs["text"])

    def test_admin_user_id_configuration_must_be_positive_integer(self):
        with patch.dict(os.environ, {"BOT_ADMIN_ID": "123"}):
            self.assertEqual(configured_admin_user_id(), 123)
        with patch.dict(os.environ, {"BOT_ADMIN_ID": "not-a-number"}):
            with self.assertRaises(ValueError):
                configured_admin_user_id()


class ApiCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_retries_rate_limits_and_server_errors_then_recovers(self):
        class Response:
            def __init__(self, status_code, data=None, headers=None):
                self.status_code = status_code
                self.data = data
                self.headers = headers or {}

            def json(self):
                return self.data

        class Client:
            calls = 0

            async def get(self, _url, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return Response(429, headers={"Retry-After": "0"})
                if self.calls == 2:
                    return Response(503)
                return Response(200, {"recovered": True})

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            client = Client()
            api = ApiClient(cast(httpx.AsyncClient, client), db, 3600)
            with patch("api_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                result = await api.get("https://service.example/test", cache_response=False)

        self.assertEqual(result, {"recovered": True})
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_api_retries_network_and_invalid_json_errors(self):
        class Response:
            status_code = 200

            def __init__(self, invalid=False):
                self.invalid = invalid

            def json(self):
                if self.invalid:
                    raise json.JSONDecodeError("invalid", "", 0)
                return {"recovered": True}

        class Client:
            calls = 0

            async def get(self, _url, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ReadTimeout("temporary timeout")
                if self.calls == 2:
                    return Response(invalid=True)
                return Response()

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            client = Client()
            api = ApiClient(cast(httpx.AsyncClient, client), db, 3600)
            with patch("api_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                result = await api.get("https://service.example/test", cache_response=False)

        self.assertEqual(result, {"recovered": True})
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_api_does_not_retry_permanent_client_errors(self):
        class Response:
            status_code = 404
            headers = {}

        class Client:
            calls = 0

            async def get(self, _url, **_kwargs):
                self.calls += 1
                return Response()

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            client = Client()
            api = ApiClient(cast(httpx.AsyncClient, client), db, 3600)
            with self.assertLogs("api_client", level="WARNING") as captured:
                result = await api.get(
                    "https://service.example/missing?api_key=do-not-log",
                    cache_response=False,
                )

        self.assertIsNone(result)
        self.assertEqual(client.calls, 1)
        self.assertNotIn("do-not-log", "\n".join(captured.output))

    async def test_api_logs_do_not_expose_url_credentials_or_query_values(self):
        class Response:
            status_code = 404
            headers = {}

        class Client:
            async def get(self, _url, **_kwargs):
                return Response()

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            api = ApiClient(cast(httpx.AsyncClient, Client()), db, 3600)
            with self.assertLogs("api_client", level="WARNING") as captured:
                await api.get(
                    "https://user:password@service.example/missing?api_key=secret",
                    cache_response=False,
                )

        logs = "\n".join(captured.output)
        self.assertIn("service.example/missing", logs)
        for secret in ("user", "password", "api_key", "secret"):
            self.assertNotIn(secret, logs)

    async def test_json_arrays_are_cached(self):
        class Response:
            status_code = 200

            def json(self):
                return [{"word": "serendipity"}]

        class Client:
            calls = 0
            options = {}

            async def get(self, _url, **kwargs):
                self.calls += 1
                self.options = kwargs
                return Response()

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            client = Client()
            api = PokeAPI(cast(httpx.AsyncClient, client), db, 3600)
            url = "https://api.dictionaryapi.dev/api/v2/entries/en/serendipity"

            first = await api.get(url)
            second = await api.get(url)

        self.assertEqual(first, [{"word": "serendipity"}])
        self.assertEqual(second, first)
        self.assertEqual(client.calls, 1)
        self.assertTrue(client.options["follow_redirects"])
        self.assertIn("PokedexGo/1.0", client.options["headers"]["User-Agent"])

    async def test_pokemon_names_are_loaded_for_spelling_suggestions(self):
        class Response:
            status_code = 200

            def json(self):
                return {"results": [{"name": "pikachu"}, {"name": "pichu"}]}

        class Client:
            async def get(self, _url, **_kwargs):
                return Response()

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            api = ApiClient(cast(httpx.AsyncClient, Client()), db, 3600)
            names = await api.pokemon_names()

        self.assertEqual(names, ["pikachu", "pichu"])


class DatabaseMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_backups_legacy_database_before_migrating(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "pokedex.sqlite3"
            async with aiosqlite.connect(database_path) as legacy_db:
                await legacy_db.executescript(SCHEMA)
                await legacy_db.execute("INSERT INTO users(id,name) VALUES(7,'Trainer')")
                await legacy_db.commit()

            app = SimpleNamespace(
                bot_data={
                    "cfg": SimpleNamespace(
                        cache_ttl_sec=3600,
                        http_timeout=1.0,
                    ),
                },
            )
            with (
                patch("bot.DB_PATH", database_path),
                patch("bot.BACKUP_DIR", database_path.parent / "backups"),
            ):
                await post_init(cast(Any, app))
                try:
                    self.assertEqual(await database_version(app.bot_data["db"]), SCHEMA_VERSION)
                    backup_files = list((database_path.parent / "backups").glob("*.sqlite3"))
                    self.assertEqual(len(backup_files), 1)
                    async with aiosqlite.connect(backup_files[0]) as backup:
                        row = await (await backup.execute(
                            "SELECT name FROM users WHERE id=7"
                        )).fetchone()
                    self.assertEqual(row, ("Trainer",))
                finally:
                    await post_shutdown(cast(Any, app))

    async def test_legacy_database_migrates_without_losing_user_data(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            await db.execute("INSERT INTO users(id,name) VALUES(7,'Trainer')")
            await db.commit()

            self.assertEqual(await database_version(db), 0)
            await initialize_database(db)
            row = await (await db.execute(
                "SELECT name FROM users WHERE id=7"
            )).fetchone()

        self.assertEqual(row, ("Trainer",))

    async def test_initialize_database_applies_and_tracks_schema_version(self):
        async with aiosqlite.connect(":memory:") as db:
            await initialize_database(db)
            self.assertEqual(await database_version(db), SCHEMA_VERSION)
            await initialize_database(db)
            self.assertEqual(await database_version(db), SCHEMA_VERSION)
            row = await (await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )).fetchone()
        self.assertIsNotNone(row)

    async def test_database_backup_contains_current_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = Path(temp_dir) / "backups" / "test.sqlite3"
            async with aiosqlite.connect(":memory:") as db:
                await initialize_database(db)
                await db.execute("INSERT INTO users(id,name) VALUES(1,'Trainer')")
                await db.commit()
                await backup_database(db, backup_path)

            async with aiosqlite.connect(backup_path) as backup:
                row = await (await backup.execute(
                    "SELECT name FROM users WHERE id=1"
                )).fetchone()
            self.assertEqual(row, ("Trainer",))

    async def test_backup_mirrors_to_configured_remote_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local_directory = root / "local"
            remote_directory = root / "remote"
            with patch.dict(os.environ, {"BACKUP_REMOTE_DIR": str(remote_directory)}):
                directories = configured_backup_directories(local_directory)
                async with aiosqlite.connect(":memory:") as db:
                    await initialize_database(db)
                    await db.execute("INSERT INTO users(id,name) VALUES(1,'Trainer')")
                    await db.commit()
                    destinations = await backup_to_directories(db, directories)

            self.assertEqual(len(destinations), 2)
            self.assertTrue(all(destination.is_file() for destination in destinations))
            self.assertEqual(destinations[0].name, destinations[1].name)
            async with (
                aiosqlite.connect(destinations[0]) as local,
                aiosqlite.connect(destinations[1]) as remote,
            ):
                local_row = await (await local.execute(
                    "SELECT name FROM users WHERE id=1"
                )).fetchone()
                remote_row = await (await remote.execute(
                    "SELECT name FROM users WHERE id=1"
                )).fetchone()
                self.assertEqual(local_row, remote_row)

    async def test_remote_backup_failure_preserves_the_verified_local_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local_directory = root / "local"
            blocked_parent = root / "not-a-directory"
            blocked_parent.write_text("block remote backup directory creation")

            async with aiosqlite.connect(":memory:") as db:
                await initialize_database(db)
                with self.assertRaises(OSError):
                    await backup_to_directories(
                        db,
                        [local_directory, blocked_parent / "remote"],
                    )

            self.assertEqual(len(list(local_directory.glob("pokedex-*.sqlite3"))), 1)

    async def test_backup_retention_only_removes_old_managed_backups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            managed = [
                directory / f"pokedex-{index:03}.sqlite3"
                for index in range(3)
            ]
            for index, backup in enumerate(managed):
                backup.touch()
                os.utime(backup, (index + 1, index + 1))
            unrelated = directory / "keep.sqlite3"
            unrelated.touch()

            removed = prune_backups(directory, keep_last=2)

            self.assertEqual(removed, [managed[0]])
            self.assertFalse(managed[0].exists())
            self.assertTrue(managed[1].exists())
            self.assertTrue(managed[2].exists())
            self.assertTrue(unrelated.exists())

    async def test_manual_backup_command_uses_configured_database_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "custom.sqlite3"
            backup_dir = Path(temp_dir) / "backups"
            async with aiosqlite.connect(database_path) as db:
                await initialize_database(db)
                await db.execute("INSERT INTO users(id,name) VALUES(1,'Trainer')")
                await db.commit()

            with (
                patch("backup_db.DB_PATH", database_path),
                patch("backup_db.BACKUP_DIR", backup_dir),
            ):
                await create_manual_backup()

            backup_files = list(backup_dir.glob("*.sqlite3"))
            self.assertEqual(len(backup_files), 1)
            async with aiosqlite.connect(backup_files[0]) as db:
                row = await (await db.execute("SELECT name FROM users WHERE id=1")).fetchone()
            self.assertEqual(row, ("Trainer",))


class ApiCacheBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_bearing_requests_can_skip_persistent_cache(self):
        class Response:
            status_code = 200

            def json(self):
                return {"title": "Astronomy Picture of the Day"}

        class Client:
            async def get(self, _url, **_kwargs):
                return Response()

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            api = PokeAPI(cast(httpx.AsyncClient, Client()), db, 3600)
            result = await api.get("https://api.nasa.gov/apod?api_key=secret", cache_response=False)
            cached = await (await db.execute("SELECT COUNT(*) FROM cache")).fetchone()

        self.assertEqual(result["title"], "Astronomy Picture of the Day")
        if cached is None:
            self.fail("Cache count query returned no row")
        self.assertEqual(cached[0], 0)

    async def test_volatile_utility_results_bypass_the_long_lived_cache(self):
        class RecordingApi:
            calls = []

            async def get(self, url, *, cache_response=True):
                self.calls.append((url, cache_response))
                if "dummyjson.com/quotes" in url:
                    return {"quote": "A quote", "author": "A writer"}
                if "uselessfacts" in url:
                    return {"text": "A fact"}
                if "frankfurter" in url:
                    return {"rates": {"EUR": 0.9}}
                if "geocoding-api" in url:
                    return {"results": [{"name": "London", "country": "United Kingdom", "latitude": 51.5, "longitude": -0.12}]}
                if "api.open-meteo.com" in url:
                    return {"current": {"temperature_2m": 18, "weather_code": 0, "wind_speed_10m": 4}}
                return None

        class ReplyMessage:
            async def reply_text(self, *_args, **_kwargs):
                return None

        api = RecordingApi()
        message = ReplyMessage()
        typed_message = cast(Message, message)
        typed_api = cast(PokeAPI, api)
        await send_quote(typed_message, typed_api)
        await send_fact(typed_message, typed_api)
        await send_fx_conversion(typed_message, typed_api, ["USD", "EUR", "1"])
        await send_weather(typed_message, typed_api, "London")

        for url, cache_response in api.calls:
            if any(provider in url for provider in (
                "dummyjson.com/quotes",
                "uselessfacts",
                "frankfurter",
                "api.open-meteo.com/v1/forecast",
            )):
                self.assertFalse(cache_response, url)


class QuizReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiz_generation_stops_after_bounded_api_failures(self):
        class OfflineApi:
            calls = 0

            async def pokemon(self, _pokemon_id):
                self.calls += 1
                return None

        class FakeBot:
            messages = []

            async def send_message(self, **kwargs):
                self.messages.append(kwargs)

        api = OfflineApi()
        fake_bot = FakeBot()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"api": api},
                bot=fake_bot,
            ),
            user_data={},
        )

        await new_quiz(cast(ContextTypes.DEFAULT_TYPE, context), 5, "normal")

        self.assertEqual(api.calls, 5)
        self.assertEqual(len(fake_bot.messages), 1)
        self.assertIn("unavailable", fake_bot.messages[0]["text"].lower())


class SearchSuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiword_lookup_normalizes_and_suggests_matching_name(self):
        class FakeApi:
            requested_name = None

            async def pokemon(self, name):
                self.requested_name = name
                return None

            async def pokemon_names(self):
                return ["mr-mime", "mime-jr"]

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        async with aiosqlite.connect(":memory:") as db:
            await initialize_database(db)
            api = FakeApi()
            message = FakeMessage()
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"api": api, "db": db}),
                user_data={},
            )

            await open_pokemon(
                message,
                cast(Any, context),
                "Mr Mmee",
            )

        self.assertEqual(api.requested_name, "mr-mmee")
        self.assertIn("Mr Mime", message.text)


class TeamAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_review_reports_defensive_weaknesses_resists_and_immunities(self):
        class FakeApi:
            async def pokemon(self, pokemon_id):
                records = {
                    6: {
                        "id": 6,
                        "name": "charizard",
                        "types": [
                            {"type": {"name": "fire"}},
                            {"type": {"name": "flying"}},
                        ],
                        "stats": [{"base_stat": 80}] * 6,
                    },
                    3: {
                        "id": 3,
                        "name": "venusaur",
                        "types": [
                            {"type": {"name": "grass"}},
                            {"type": {"name": "poison"}},
                        ],
                        "stats": [{"base_stat": 80}] * 6,
                    },
                }
                return records.get(pokemon_id)

            async def type(self, type_name):
                relations = {
                    "double_damage_from": [],
                    "half_damage_from": [],
                    "no_damage_from": [],
                }
                if type_name == "fire":
                    relations["double_damage_from"] = [{"name": "water"}]
                    relations["half_damage_from"] = [{"name": "fire"}]
                elif type_name == "flying":
                    relations["double_damage_from"] = [{"name": "electric"}]
                    relations["no_damage_from"] = [{"name": "ground"}]
                elif type_name == "grass":
                    relations["half_damage_from"] = [{"name": "water"}]
                return {"damage_relations": relations}

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            await db.executemany(
                "INSERT INTO teams(user_id,pokemon_id) VALUES(?,?)",
                [(9, 6), (9, 3)],
            )
            message = FakeMessage()
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=9, first_name="Trainer"),
            )
            context = SimpleNamespace(
                args=["rate"],
                user_data={},
                application=SimpleNamespace(bot_data={"db": db, "api": FakeApi()}),
            )

            with patch("bot.update_message", return_value=message):
                await team_cmd(cast(Any, update), cast(Any, context))

        self.assertIsNotNone(message.text)
        self.assertIn("Weaknesses (Pokémon affected):", message.text)
        self.assertIn("Water (1)", message.text)
        self.assertIn("Resistances:", message.text)
        self.assertIn("Immunities:", message.text)
        self.assertIn("Ground (1)", message.text)
        self.assertIn("move coverage is not evaluated", message.text)

    async def test_team_review_does_not_report_neutral_matchups_when_api_fails(self):
        class FakeApi:
            async def pokemon(self, _pokemon_id):
                return {
                    "id": 25,
                    "name": "pikachu",
                    "types": [{"type": {"name": "electric"}}],
                    "stats": [{"base_stat": 50}] * 6,
                }

            async def type(self, _type_name):
                return None

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        async with aiosqlite.connect(":memory:") as db:
            await db.executescript(SCHEMA)
            await db.execute("INSERT INTO teams(user_id,pokemon_id) VALUES(9,25)")
            message = FakeMessage()
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=9, first_name="Trainer"),
            )
            context = SimpleNamespace(
                args=["rate"],
                user_data={},
                application=SimpleNamespace(bot_data={"db": db, "api": FakeApi()}),
            )

            with patch("bot.update_message", return_value=message):
                await team_cmd(cast(Any, update), cast(Any, context))

        self.assertEqual(
            message.text,
            "❌ Type matchup data is unavailable right now. Please try again.",
        )


class UtilityHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_apod_fallback_skips_unavailable_archive_images(self):
        class ImageResponse:
            headers = {"content-type": "image/jpeg"}

            def __init__(self, status_code, content):
                self.status_code = status_code
                self.content = content

        class FakeClient:
            calls = 0

            async def get(self, url, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ImageResponse(503, b"")
                return ImageResponse(200, b"available-image")

        class FakeApi:
            client = FakeClient()

            async def get(self, _url, **_kwargs):
                return {
                    "collection": {
                        "items": [
                            {"links": [{
                                "href": f"https://images-assets.nasa.gov/image/{image_id}/{image_id}~medium.jpg",
                                "rel": "alternate",
                                "render": "image",
                            }]}
                            for image_id in ("PIA1", "PIA2", "PIA3")
                        ],
                    },
                }

        image = await _find_apod_fallback_image(
            cast(PokeAPI, FakeApi()),
            "NASA galaxy nebula",
            date(2026, 10, 4),
        )

        self.assertEqual(image, b"available-image")

    async def test_apod_fallback_is_stable_per_day_and_rotates_next_day(self):
        class ImageResponse:
            status_code = 200
            headers = {"content-type": "image/jpeg"}

            def __init__(self, content):
                self.content = content

        class FakeClient:
            async def get(self, url, **_kwargs):
                return ImageResponse(url.encode())

        class FakeApi:
            client = FakeClient()

            async def get(self, _url, **_kwargs):
                return {
                    "collection": {
                        "items": [
                            {"links": [{
                                "href": f"https://images-assets.nasa.gov/image/{image_id}/{image_id}~medium.jpg",
                                "rel": "alternate",
                                "render": "image",
                            }]}
                            for image_id in ("PIA1", "PIA2", "PIA3")
                        ],
                    },
                }

        api = FakeApi()
        today = date(2026, 10, 4)

        typed_api = cast(PokeAPI, api)
        first_today = await _find_apod_fallback_image(typed_api, "NASA galaxy nebula", today)
        repeated_today = await _find_apod_fallback_image(typed_api, "NASA galaxy nebula", today)
        tomorrow = await _find_apod_fallback_image(
            typed_api, "NASA galaxy nebula", today + timedelta(days=1),
        )

        self.assertEqual(first_today, repeated_today)
        self.assertNotEqual(first_today, tomorrow)

    async def test_apod_api_failure_uses_nasa_image_archive_fallback(self):
        class ImageResponse:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"nasa-archive-photo"

        class FakeClient:
            async def get(self, _url, **_kwargs):
                return ImageResponse()

        class FakeApi:
            client = FakeClient()
            calls = []

            async def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "https://api.nasa.gov/" in url:
                    return None
                return {
                    "collection": {
                        "items": [{
                            "links": [{
                                "href": "https://images-assets.nasa.gov/image/PIA14728/PIA14728~medium.jpg",
                                "rel": "alternate",
                                "render": "image",
                            }],
                        }],
                    },
                }

        class FakeMessage:
            photo = None
            caption = None

            async def reply_photo(self, photo, **kwargs):
                self.photo = photo
                self.caption = kwargs["caption"]

            async def reply_text(self, *_args, **_kwargs):
                self.fail("Archive fallback should send a photo")

            def fail(self, message):
                raise AssertionError(message)

        message = FakeMessage()
        api = FakeApi()

        await send_apod(cast(Message, message), cast(PokeAPI, api))

        self.assertEqual(message.photo, b"nasa-archive-photo")
        if message.caption is None:
            self.fail("Archive image caption was not sent")
        self.assertIn("not today's APOD", message.caption)
        self.assertIn("Open today", message.caption)
        search_call = next(call for call in api.calls if "images-api.nasa.gov" in call[0])
        self.assertTrue(search_call[1].get("cache_response", True))

    async def test_apod_replaces_nasa_logo_with_matching_nasa_image(self):
        class ImageResponse:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"real-space-photo"

        class FakeClient:
            async def get(self, url, **_kwargs):
                self.url = url
                return ImageResponse()

        class FakeApi:
            client = FakeClient()

            async def get(self, url, **_kwargs):
                if "https://api.nasa.gov/" in url:
                    return {
                        "title": "NASA Science",
                        "date": "2026-10-04",
                        "explanation": "Curiosity Rover explores Mars.",
                        "media_type": "image",
                        "url": "https://science.nasa.gov/assets/nasa-logo@2x.png",
                    }
                return {
                    "collection": {
                        "items": [{
                            "links": [{
                                "href": "https://images-assets.nasa.gov/image/PIA22207/PIA22207~medium.jpg",
                                "rel": "alternate",
                                "render": "image",
                            }],
                        }],
                    },
                }

        class FakeMessage:
            photo = None
            text = None

            async def reply_photo(self, photo, **_kwargs):
                self.photo = photo

            async def reply_text(self, text, **_kwargs):
                self.text = text

        api = FakeApi()
        message = FakeMessage()

        await send_apod(cast(Message, message), cast(PokeAPI, api))

        self.assertEqual(message.photo, b"real-space-photo")
        self.assertIsNone(message.text)
        self.assertIn("PIA22207~medium.jpg", api.client.url)

    async def test_apod_does_not_send_logo_when_fallback_is_unavailable(self):
        class FakeApi:
            client = None

            async def get(self, _url, **_kwargs):
                return {
                    "title": "NASA Science",
                    "date": "2026-10-04",
                    "explanation": "Curiosity Rover explores Mars.",
                    "media_type": "image",
                    "url": "https://science.nasa.gov/assets/nasa-logo@2x.png",
                }

        class FakeMessage:
            photo = None
            text = None

            async def reply_photo(self, photo, **_kwargs):
                self.photo = photo

            async def reply_text(self, text, **_kwargs):
                self.text = text

        message = FakeMessage()

        await send_apod(cast(Message, message), cast(PokeAPI, FakeApi()))

        self.assertIsNone(message.photo)
        if message.text is None:
            self.fail("APOD fallback response was not sent")
        self.assertIn("Open NASA APOD", message.text)

    async def test_fx_resolves_catalog_names_and_lists_supported_currencies(self):
        class FakeApi:
            calls = []

            async def get(self, url, **_kwargs):
                self.calls.append(url)
                if url.endswith("/currencies"):
                    return {"DKK": "Danish Krone", "PLN": "Polish Zloty"}
                if "from=DKK" in url and "to=PLN" in url:
                    return {"rates": {"PLN": 0.58}}
                return None

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        api = FakeApi()
        message = FakeMessage()
        typed_api = cast(PokeAPI, api)
        typed_message = cast(Message, message)

        await send_fx_conversion(typed_message, typed_api, ["10", "Danish", "Krone", "to", "Polish", "Zloty"])
        self.assertEqual(message.text, "💱 10 DKK = 0.5800 PLN")
        self.assertTrue(any(url.endswith("/currencies") for url in api.calls))

        await send_fx_conversion(typed_message, typed_api, ["currencies"])
        if message.text is None:
            self.fail("Currency list did not send a response")
        self.assertIn("DKK — Danish Krone", message.text)
        self.assertIn("PLN — Polish Zloty", message.text)

    async def test_definition_falls_back_to_wiktionary_when_primary_is_unavailable(self):
        class FakeApi:
            async def get(self, url, **_kwargs):
                if "dictionaryapi.dev" in url:
                    return None
                return {
                    "query": {
                        "pages": {
                            "208288": {
                                "title": "responsive",
                                "extract": (
                                    "== English ==\n"
                                    "=== Adjective ===\n"
                                    "Able to receive and respond to external stimuli.\n"
                                    "== French ==\n"
                                    "responsive"
                                ),
                            },
                        },
                    },
                }

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        message = FakeMessage()
        await send_definition(cast(Message, message), cast(PokeAPI, FakeApi()), "responsive")

        if message.text is None:
            self.fail("Definition fallback did not send a response")
        self.assertIn("Wiktionary", message.text)
        self.assertIn("Able to receive and respond", message.text)
        self.assertNotIn("French", message.text)

    async def test_pokemon_photo_caption_stays_within_telegram_limit(self):
        class FakeApi:
            async def species(self, _pokemon_id):
                return {
                    "flavor_text_entries": [{
                        "language": {"name": "en"},
                        "flavor_text": "Long entry " * 300,
                    }],
                }

        pokemon = {
            "id": 25,
            "name": "pikachu",
            "height": 4,
            "weight": 60,
            "types": [{"type": {"name": "electric"}}],
            "abilities": [{"ability": {"name": "static"}}],
        }
        caption = await pokemon_caption(cast(PokeAPI, FakeApi()), pokemon)

        self.assertLessEqual(len(caption), 1024)

    async def test_iv_estimate_uses_pokemon_base_stats_and_nature(self):
        class FakeApi:
            async def pokemon(self, _name):
                return {
                    "name": "pikachu",
                    "stats": [{"stat": {"name": "speed"}, "base_stat": 90}],
                }

            async def nature(self, _name):
                return {
                    "name": "jolly",
                    "increased_stat": {"name": "speed"},
                    "decreased_stat": {"name": "special-attack"},
                }

        class FakeMessage:
            text = None

            async def reply_text(self, text, **_kwargs):
                self.text = text

        message = FakeMessage()
        await send_iv_estimate(
            cast(Message, message),
            cast(PokeAPI, FakeApi()),
            ["pikachu", "speed", "31", "252", "50", "jolly"],
        )

        if message.text is None:
            self.fail("IV calculation did not send a response")
        self.assertIn("Estimated stat: <b>156</b>", message.text)
        self.assertIn("cannot reveal", message.text)


if __name__ == "__main__":
    unittest.main()