# Pokédex Go

## Setup

Use Python 3.10 or newer. From the project directory in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:BOT_TOKEN = "your-telegram-bot-token"
$env:NASA_KEY = "your-nasa-api-key"
python bot.py
```

`BOT_TOKEN` is required. `NASA_KEY` is optional; the bot uses NASA's `DEMO_KEY`
when it is not set. `LOG_LEVEL` is optional and defaults to `INFO` (valid Python
logging levels such as `DEBUG`, `INFO`, `WARNING`, and `ERROR` are accepted).
`DB_PATH` optionally selects the SQLite database file; it defaults to
`pokedex.db` beside `bot.py`. Set the same `DB_PATH` when running `backup_db.py`.
`BACKUP_DIR` optionally selects the local backup directory; by default it is a
`backups/` folder beside the database. `BACKUP_REMOTE_DIR` optionally adds a
second backup destination, such as a mounted network share or a cloud-sync
folder. `BACKUP_RETENTION` sets how many managed backups to keep per destination
(default: 14). Backups are integrity-checked before being published, and only
files named `pokedex-*.sqlite3` are considered for retention cleanup.
`BOT_ADMIN_ID` optionally enables the private `/health` command and receives
rate-limited alerts after repeated unhandled errors. It must be the numeric
Telegram user ID of the administrator.
Never commit real tokens or keys to source control.

Run the automated tests from the project directory with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`requirements.txt` pins the runtime dependencies and their required
transitive dependencies to the exact versions used by this project.

## Database backups and migrations

The bot applies versioned SQLite migrations automatically during startup. Before
upgrading an existing database schema, it writes a safety backup to `backups/`.
If an upgrade fails, the bot logs the error and stops rather than starting with a
partially upgraded database.

Create a manual backup at any time:

```powershell
.\.venv\Scripts\python.exe backup_db.py
```

Backups are timestamped SQLite files in `backups/`. Keep copies outside the
machine running the bot for disaster recovery, and periodically prune old local
backups after verifying the off-machine copies. To restore, stop the bot, copy
the chosen backup over the configured database file (`pokedex.db` by default),
and start the bot again.

Set `BACKUP_REMOTE_DIR` to a directory that is actually synchronized or stored
off-machine; the bot cannot verify that an external sync client has uploaded its
files. When configured, a failed remote write is reported as an error (the
local backup remains), rather than silently claiming the mirror succeeded.
Both destinations receive the same verified snapshot. Startup migration
backups and manual backups are mirrored. For routine backups, schedule
`.\.venv\Scripts\python.exe backup_db.py` with Windows Task Scheduler.

## Logs and resilience

Run with `LOG_LEVEL=DEBUG` when diagnosing issues. API requests log the provider,
endpoint, status, and retry activity without logging query strings or API keys.
Transient network errors, HTTP 429, and HTTP 5xx responses are retried a bounded
number of times; permanent client errors are not retried.

## Project modules

- `bot.py` registers Telegram handlers, menu callbacks, and starts the application.
- `api_client.py` owns provider requests, retries, L1 in-memory fast caching, and SQLite response caching.
- `database.py` owns SQLite schema versioning, v4 migrations (trainers, box, eggs, market, badges, pvp), cache, and backups.
- `wild_handlers.py` implements the wild encounter minigame, safari catch mechanics, PokéMart shop, inventory bag, and box browser.
- `battle_handlers.py` implements the turn-based PvE battle arena with dynamic visual health bars and damage calculations.
- `gym_handlers.py` implements the 8 Kanto Gym Leader boss battles and Gym Badge trophy collection.
- `pvp_handlers.py` implements real-time 2-player trainer PvP duels.
- `economy_handlers.py` implements the player marketplace (`/market`), listing sales (`/sell`), egg incubator (`/eggs`), and evolution chamber (`/evolve`).
- `wtp_handlers.py` implements the Who's That Pokémon mystery trivia minigame (`/wtp`).
- `meta_handlers.py` implements competitive Smogon & VGC tournament build recommendations (`/meta`, `/smogon`).
- `inline_handlers.py` implements global Telegram inline query search (`@YourBot <pokemon>`) for instant card sharing in any chat.
- `utility_helpers.py` contains shared formatting, parsing, and response-shaping helpers.
- `utility_handlers.py` implements the wiki, dictionary, quote, fact, NASA APOD, currency, and weather commands.

## Gameplay & Features

- **Official Audio Cries & Animated Sprites**: Tap `🔊 Cry` on any Pokémon profile for official `.ogg` cries, or `🎬 Animated Sprite` for full Showdown battle animations.
- **Safari Catch & Tall Grass (`/wild` / `/catch`)**: Encounter wild Pokémon (with level, nature, randomized IVs, and shiny chance). Throw Pokéballs, Great Balls, Ultra Balls, or feed Razz Berries.
- **Gym Leader Campaign (`/gym` / `/badges`)**: Conquer the 8 Kanto Gym Leaders (Brock, Misty, Lt. Surge, Erika, Koga, Sabrina, Blaine, Giovanni) to earn official Gym Badges!
- **Real-Time PvP Duels (`/duel`)**: Challenge any trainer to a live turn-based battle with spoils going to the victor.
- **Player Marketplace (`/market` & `/sell`)**: List caught Pokémon for sale or buy rare Pokémon from other trainers.
- **Egg Incubator (`/eggs` / `/incubator`)**: Hatch mystery Pokémon with boosted IVs (18–31) and an 8% shiny rate by walking steps.
- **Evolution Chamber (`/evolve <box_id>`)**: Evolve your caught Pokémon into their next evolutionary forms with stat and level boosts.
- **Competitive Smogon Meta (`/meta <pokemon>`)**: Get instant competitive builds with tier ratings, optimal items, natures, EVs, and tournament movesets.
- **Who's That Pokémon? (`/wtp`)**: Mystery silhouette trivia with lore hints and coin rewards.
- **Global Inline Mode**: Type `@YourBot pikachu` anywhere in any chat to instantly share formatted Pokédex cards.


## Team review and request limits

Use `/team rate` to see your team's defensive weaknesses, resistances, and
immunities. Counts show how many team members are affected; the report does not
infer attack-move coverage. If type data cannot be loaded, the bot reports that
the review is unavailable instead of presenting incomplete matchups.

Incoming messages and button interactions share a per-user token bucket. The
default allows a burst of 10 updates and refills at one update per second.
Excess updates are stopped before command handlers run, and repeated warning
messages are throttled. Limits are in-memory and apply per running bot process;
they reset on restart and are not shared across multiple bot instances.

## Search, Pokédex browsing, and operations

When a Pokémon name cannot be loaded, the bot checks the cached PokéAPI name
list and offers up to three close spelling suggestions. `/dex missing [page]`
and `/dex discovered [page]` browse the user's collection in 20-entry pages.

When `BOT_ADMIN_ID` is configured, `/health` is private and admin-only. It
checks SQLite integrity, makes an uncached PokéAPI probe, and reports the
unhandled-error count since startup. After five unhandled errors, the bot sends
the admin a generic alert, then suppresses further alerts for 15 minutes.
Alert messages do not include exception text or user data. Health checks and
alerts are best-effort diagnostics, not a substitute for process monitoring.
