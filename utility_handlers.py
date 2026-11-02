import os
import random
import re
from datetime import date
from typing import Optional, Sequence
from urllib.parse import quote, quote_plus, urlencode

import httpx
from telegram import Message
from telegram.constants import ParseMode

from api_client import PokeAPI
from utility_helpers import (
    FX_CURRENCIES_URL,
    esc,
    parse_fx_input,
    pretty,
    quote_parts,
    weather_description,
    wiki_summary_parts,
    wiktionary_english_extract,
)


async def send_wikipedia(message: Message, api: PokeAPI, query: str) -> None:
    # Try exact query first, then fuzzy search fallback
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(query.replace(" ", "_"), safe="")
    data = await api.get(url)
    if not data or data.get("type", "").endswith("/not_found"):
        # Try Wikipedia's search API to find the closest article
        search_url = "https://en.wikipedia.org/w/api.php?" + urlencode({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 1,
            "format": "json",
        })
        search_data = await api.get(search_url)
        best = ((search_data or {}).get("query", {}).get("search") or [None])[0]
        if not best:
            await message.reply_text(
                "❌ No Wikipedia article found for <b>" + esc(query) + "</b>.\nTry rephrasing your search.",
                parse_mode=ParseMode.HTML,
            )
            return
        slug = best["title"].replace(" ", "_")
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(slug, safe="")
        data = await api.get(url)
        if not data or data.get("type", "").endswith("/not_found"):
            await message.reply_text("❌ Could not load Wikipedia article.", parse_mode=ParseMode.HTML)
            return

    title, extract, page, thumbnail = wiki_summary_parts(data, query)
    title = title[:120]

    # Build richer description — up to 900 chars, ending on a sentence boundary
    if len(extract) > 900:
        truncated = extract[:897]
        last_dot = truncated.rfind(". ")
        if last_dot > 400:
            extract = truncated[:last_dot + 1]
        else:
            extract = truncated.rstrip() + "…"

    # Get the page description (short subtitle) if available
    description = (data.get("description") or "").strip()

    lines = [f"📚 <b>{esc(title)}</b>"]
    if description:
        lines.append(f"<i>{esc(description.capitalize())}</i>")
    lines.append("")
    lines.append(esc(extract))

    # Reading time estimate
    word_count = len(extract.split())
    read_sec = max(1, round(word_count / 4))
    read_label = f"{read_sec // 60}m {read_sec % 60}s" if read_sec >= 60 else f"{read_sec}s"
    if page:
        lines.append("\n⏱ ~" + read_label + " read  •  🌍 <a href=\"" + esc(page) + "\">Open full article</a>")
    else:
        lines.append("\n⏱ ~" + read_label + " read")

    text = "\n".join(lines)

    if thumbnail:
        await message.reply_photo(photo=thumbnail, caption=text, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# Part-of-speech emoji map
_POS_EMOJI = {
    "noun": "🔵", "verb": "🟢", "adjective": "🟡", "adverb": "🟠",
    "pronoun": "🟣", "preposition": "⚪", "conjunction": "🔴",
    "interjection": "❗", "exclamation": "❗", "abbreviation": "📌",
}


async def send_definition(message: Message, api: PokeAPI, word: str) -> None:
    clean_word = word.strip().lower()
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(clean_word, safe='')}"
    data = await api.get(url)

    # ── Primary: Free Dictionary API ─────────────────────────────────────────
    if isinstance(data, list) and data:
        entry = data[0]

        # Phonetic: prefer text that starts with /
        phonetics = entry.get("phonetics") or []
        phonetic = next(
            (p.get("text") for p in phonetics if (p.get("text") or "").startswith("/")),
            next((p.get("text") for p in phonetics if p.get("text")), ""),
        )
        audio_url = next(
            (p.get("audio") for p in phonetics if p.get("audio")), None
        )

        # Origin
        origin = (entry.get("origin") or "").strip()

        # Build header
        header = f"📖 <b>{esc(clean_word.title())}</b>"
        if phonetic:
            header += f"  <code>{esc(phonetic)}</code>"

        sections = [header]
        if origin:
            sections.append(f"<i>Origin: {esc(origin[:200])}</i>")

        # Meanings — up to 4, up to 2 defs each
        for meaning in entry.get("meanings", [])[:4]:
            pos = meaning.get("partOfSpeech", "")
            emoji = _POS_EMOJI.get(pos, "•")
            defs = meaning.get("definitions", [])
            if not defs:
                continue
            block = [f"{emoji} <b>{esc(pos.capitalize())}</b>"]
            for i, defn in enumerate(defs[:2], 1):
                def_text = (defn.get("definition") or "").strip()[:350]
                block.append(f"  {i}. {esc(def_text)}")
                example = (defn.get("example") or "").strip()[:180]
                if example:
                    block.append(f'     <i>"{esc(example)}"</i>')
                syns = [esc(s) for s in (defn.get("synonyms") or [])[:4]]
                ants = [esc(a) for a in (defn.get("antonyms") or [])[:4]]
                if syns:
                    block.append(f"     ✅ Synonyms: {', '.join(syns)}")
                if ants:
                    block.append(f"     ❌ Antonyms: {', '.join(ants)}")
            sections.append("\n".join(block))

        # Top-level synonyms / antonyms
        all_syns = list(dict.fromkeys(
            s for m in entry.get("meanings", [])
            for s in (m.get("synonyms") or [])
        ))[:6]
        all_ants = list(dict.fromkeys(
            a for m in entry.get("meanings", [])
            for a in (m.get("antonyms") or [])
        ))[:6]
        if all_syns:
            sections.append(f"✅ <b>Synonyms:</b> {esc(', '.join(all_syns))}")
        if all_ants:
            sections.append(f"❌ <b>Antonyms:</b> {esc(', '.join(all_ants))}")

        # Footer
        wik_link = f'https://en.wiktionary.org/wiki/{quote(clean_word, safe="")}'
        sections.append(f'📎 <a href="{wik_link}">View on Wiktionary</a>')

        text = "\n\n".join(sections)
        await message.reply_text(text[:4000], parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        # Send audio pronunciation as a voice note if available
        if audio_url:
            try:
                await message.reply_voice(
                    voice=audio_url,
                    caption=f"🔊 Pronunciation: <b>{esc(clean_word.title())}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    # ── Fallback: Wiktionary ─────────────────────────────────────────────────
    fallback_url = "https://en.wiktionary.org/w/api.php?" + urlencode({
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "titles": clean_word,
    })
    fallback = wiktionary_english_extract(await api.get(fallback_url))
    if fallback is None:
        await message.reply_text(
            "No definition found for <b>" + esc(clean_word.title()) + "</b>.\nCheck your spelling, or try /wiki for an encyclopaedia entry.",
            parse_mode=ParseMode.HTML,
        )
        return
    title, extract = fallback
    wik_link = f'https://en.wiktionary.org/wiki/{quote(clean_word, safe="")}'
    text = (
        f"📖 <b>{esc(title.title())}</b>  <i>(via Wiktionary)</i>\n\n"
        f"{esc(extract[:1800])}\n\n"
        f'📎 <a href="{wik_link}">View on Wiktionary</a>'
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def send_quote(message: Message, api: PokeAPI) -> None:
    result = quote_parts(await api.get("https://dummyjson.com/quotes/random", cache_response=False))
    if result is None:
        result = quote_parts(await api.get("https://zenquotes.io/api/random", cache_response=False))
    if result is None:
        await message.reply_text("❌ Quote service is unavailable right now.")
        return
    content, author = result
    await message.reply_text(
        f"💬 <i>{esc(content[:1200])}</i>\n— <b>{esc(author)}</b>",
        parse_mode=ParseMode.HTML,
    )


_FALLBACK_FACTS = [
    "Honey never spoils. Archaeologists have found pots of honey in ancient Egyptian tombs that are over 3,000 years old and still perfectly edible.",
    "Octopuses have three hearts, and their blood is copper-based and blue.",
    "A day on Venus is longer than a year on Venus: it takes Venus longer to rotate once on its axis than to complete one orbit around the Sun.",
    "Bananas are naturally radioactive because they contain high amounts of potassium-40.",
    "The footprints left on the Moon by Apollo astronauts will stay there for at least 100 million years because there is no wind or water to erode them.",
    "Crows have been shown to remember human faces and can hold grudges against specific people for years.",
    "Sharks existed before trees; sharks date back over 400 million years, while the earliest trees emerged around 350 million years ago.",
    "The heart of a blue whale is so large that a human toddler could swim through its arteries.",
    "Wombat feces are cube-shaped, which stops them from rolling away when marking territory on elevated surfaces.",
    "Cleopatra lived closer in time to the Moon landing than to the construction of the Great Pyramid of Giza.",
    "Nintendo was founded in 1889 as a handmade hanafuda playing card company in Kyoto, Japan.",
    "Pikachu's name is a portmanteau of two Japanese sounds: 'pika' (the crackle of electricity) and 'chu' (the squeak of a mouse).",
    "Butterflies taste with their feet to test if a leaf is edible before laying eggs on it.",
    "There are more trees on Earth (estimated 3 trillion) than there are stars in the Milky Way galaxy (100–400 billion).",
    "Water can boil and freeze at the same time; this phenomenon is called the 'triple point'.",
    "A cloud can weigh more than a million pounds (around 500,000 kilograms).",
    "Koalas sleep up to 20 hours a day to conserve energy while digesting toxic eucalyptus leaves.",
    "Sea otters hold hands while they sleep so they don't drift away from each other.",
]


async def send_fact(message: Message, api: PokeAPI) -> None:
    data = await api.get("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en", cache_response=False)
    fact = (data.get("text") or "").strip() if isinstance(data, dict) else ""
    if not fact:
        data = await api.get("https://catfact.ninja/fact", cache_response=False)
        fact = (data.get("fact") or "").strip() if isinstance(data, dict) else ""
    if not fact:
        fact = random.choice(_FALLBACK_FACTS)
    await message.reply_text(f"🧠 {fact[:3500]}")


async def _find_apod_fallback_image(
    api: PokeAPI, explanation: str, image_date: date,
) -> Optional[bytes]:
    first_sentence = explanation.split(".", 1)[0]
    search_terms = re.findall(r"\b[A-Z][a-z]{2,}\b", first_sentence)
    search_terms.extend(re.findall(
        r"\b(?:selfie|panorama|rover|galaxy|nebula|comet|eclipse|planet|moon|stars?)\b",
        first_sentence,
        flags=re.IGNORECASE,
    ))
    sol = re.search(r"\bsol\s+\d+\b", first_sentence, flags=re.IGNORECASE)
    if sol:
        search_terms.append(sol.group())
    search_terms = list(dict.fromkeys(term.casefold() for term in search_terms))
    if not search_terms:
        return None

    search_url = "https://images-api.nasa.gov/search?" + urlencode({
        "q": " ".join(search_terms),
        "media_type": "image",
        "page_size": 20,
    })
    results = await api.get(search_url)
    collection = results.get("collection") if isinstance(results, dict) else None
    items = collection.get("items", []) if isinstance(collection, dict) else []
    image_urls = list(dict.fromkeys(
        link["href"]
        for item in items
        for link in (item.get("links", []) if isinstance(item, dict) else [])
        if isinstance(link, dict)
        and link.get("render") == "image"
        and link.get("rel") == "alternate"
        and str(link.get("href", "")).lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ))
    if not image_urls:
        return None

    start_index = image_date.toordinal() % len(image_urls)
    for offset in range(len(image_urls)):
        image_url = image_urls[(start_index + offset) % len(image_urls)]
        try:
            response = await api.client.get(
                image_url,
                follow_redirects=True,
                headers={"User-Agent": "PokedexGo/1.0 (Telegram bot; contact via Telegram)"},
            )
        except httpx.HTTPError:
            continue
        if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
            return response.content
    return None


async def send_apod(message: Message, api: PokeAPI) -> None:
    nasa_key = os.getenv("NASA_KEY", "DEMO_KEY").strip() or "DEMO_KEY"
    url = "https://api.nasa.gov/planetary/apod?" + urlencode({"api_key": nasa_key})
    data = await api.get(url, cache_response=False)
    if not data:
        image = await _find_apod_fallback_image(api, "NASA galaxy nebula", date.today())
        if image:
            await message.reply_photo(
                photo=image,
                caption=(
                    "<b>🚀 NASA Space Image</b>\n\n"
                    "NASA's APOD feed is temporarily unavailable. This image is from "
                    "NASA's image archive, not today's APOD.\n\n"
                    '<a href="https://apod.nasa.gov/apod/astropix.html">Open today&#39;s APOD</a>'
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        await message.reply_text(
            '❌ NASA\'s APOD feed is unavailable right now. '
            '<a href="https://apod.nasa.gov/apod/astropix.html">Open today&#39;s APOD</a>',
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    image_date = date.today()
    if isinstance(data, dict) and data.get("date"):
        try:
            image_date = date.fromisoformat(data["date"])
        except (TypeError, ValueError):
            pass

    title = esc((data.get("title") or "NASA APOD")[:150])
    explanation = esc((data.get("explanation") or "").strip()[:650])
    apod_date = data.get("date")
    date_line = f" <code>({esc(apod_date)})</code>" if apod_date else ""
    caption = f"<b>🚀 {title}</b>{date_line}\n\n{explanation}"
    media_url = data.get("url")
    if data.get("media_type") == "image" and media_url:
        if "nasa-logo" in str(media_url).casefold():
            image = await _find_apod_fallback_image(
                api, data.get("explanation") or "", image_date,
            )
            if image:
                await message.reply_photo(photo=image, caption=caption, parse_mode=ParseMode.HTML)
                return
            apod_link = "https://apod.nasa.gov/apod/astropix.html"
            await message.reply_text(
                caption + f'\n\n<a href="{esc(apod_link)}">Open NASA APOD</a>',
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        await message.reply_photo(photo=media_url, caption=caption, parse_mode=ParseMode.HTML)
    else:
        text = caption + (f"\n\n<a href=\"{esc(media_url)}\">Open NASA media</a>" if media_url else "")
        await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def send_fx_conversion(message: Message, api: PokeAPI, args: Sequence[str]) -> None:
    if isinstance(args, str):
        args = args.split()
    if len(args) == 1 and args[0].strip().casefold() in ("currencies", "list"):
        currencies = await api.get(FX_CURRENCIES_URL)
        if not isinstance(currencies, dict) or not currencies:
            await message.reply_text("❌ Supported currencies are unavailable right now.")
            return
        lines = [f"{code} — {name}" for code, name in sorted(currencies.items())]
        await message.reply_text(
            "💱 Supported currencies (code — name):\n"
            + "\n".join(lines)
            + "\n\nExample: /fx 100 USD to EUR"
        )
        return

    try:
        source, target, amount = parse_fx_input(args)
    except ValueError as error:
        if not str(error).startswith("Unknown currency"):
            await message.reply_text(
                f"❌ {error}\nTry /fx 100 USD to EUR, /fx 100 US dollars to euros, "
                "or /fx $100 to €. Use /fx currencies to see supported currencies."
            )
            return
        currencies = await api.get(FX_CURRENCIES_URL)
        try:
            source, target, amount = parse_fx_input(
                args, currencies if isinstance(currencies, dict) else None,
            )
        except ValueError as currency_error:
            await message.reply_text(
                f"❌ {currency_error}\nUse /fx currencies to see supported currencies."
            )
            return

    if source == target:
        await message.reply_text(f"💱 {amount:g} {source} = {amount:.4f} {target}")
        return

    query = urlencode({"amount": amount, "from": source, "to": target})
    data = await api.get(f"https://api.frankfurter.app/latest?{query}", cache_response=False)
    result = (data or {}).get("rates", {}).get(target)
    if result is None:
        await message.reply_text("❌ Exchange rate is unavailable. Check the currency codes and try again.")
        return
    await message.reply_text(f"💱 {amount:g} {source} = {result:.4f} {target}")


async def _send_wttr_weather(message: Message, api: PokeAPI, raw_query: str) -> bool:
    try:
        encoded_city = quote_plus(raw_query.strip())
        wttr_url = f"https://wttr.in/{encoded_city}?format=j1"
        data = await api.get(wttr_url, cache_response=False)
        if not data or not isinstance(data, dict):
            return False
        conditions = data.get("current_condition") or []
        if not conditions:
            return False
        current = conditions[0]
        area = (data.get("nearest_area") or [{}])[0]
        area_name = (area.get("areaName") or [{}])[0].get("value") or raw_query
        country = (area.get("country") or [{}])[0].get("value")
        region = (area.get("region") or [{}])[0].get("value")

        place_parts = [area_name, region, country]
        place = ", ".join(dict.fromkeys(p for p in place_parts if p))

        temp = current.get("temp_C")
        feels_like = current.get("FeelsLikeC")
        wind = current.get("windspeedKmph")
        humidity = current.get("humidity")
        desc_list = current.get("weatherDesc") or []
        desc = desc_list[0].get("value") if desc_list else "Clear"

        lines = [
            f"🌦️ <b>Weather: {esc(place or raw_query)}</b>",
            f"Condition: {esc(desc.strip())}",
            f"Temperature: {temp}°C" if temp is not None else "Temperature: unavailable",
        ]
        if feels_like is not None:
            lines.append(f"Feels like: {feels_like}°C")
        lines.append(f"Wind: {wind} km/h" if wind is not None else "Wind: unavailable")
        if humidity is not None:
            lines.append(f"Humidity: {humidity}%")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return True
    except Exception:
        return False


async def send_weather(message: Message, api: PokeAPI, city: str) -> None:
    raw_query = city.strip()
    if not raw_query:
        await message.reply_text("❌ Please specify a city. Example: /weather Chennai")
        return

    # Handle comma-separated input (e.g. "Paris, France" or "London, UK")
    if "," in raw_query:
        parts = [p.strip() for p in raw_query.split(",") if p.strip()]
        city_name = parts[0]
        hint = " ".join(parts[1:]).lower()
    else:
        city_name = raw_query
        hint = None

    async def fetch_geocoding(name: str):
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
            "name": name,
            "count": 10,
            "language": "en",
            "format": "json",
        })
        data = await api.get(geo_url)
        return (data or {}).get("results") or []

    locations = await fetch_geocoding(city_name)

    # If no results and no comma was provided, try splitting off the last word as a country or state hint
    # (e.g. "Paris France" -> search "Paris", filter by "France")
    if not locations and " " in raw_query and not hint:
        c_part, _, h_part = raw_query.rpartition(" ")
        if c_part.strip():
            hint = h_part.lower()
            locations = await fetch_geocoding(c_part.strip())

    if not locations:
        if await _send_wttr_weather(message, api, raw_query):
            return
        await message.reply_text("❌ City not found. Try a city name like /weather Paris or /weather London, UK.")
        return

    # Pick the best matching location (matching country/admin hint if provided)
    location = locations[0]
    if hint:
        for loc in locations:
            country = (loc.get("country") or "").lower()
            country_code = (loc.get("country_code") or "").lower()
            admin1 = (loc.get("admin1") or "").lower()
            admin2 = (loc.get("admin2") or "").lower()
            if hint in country or hint == country_code or hint in admin1 or hint in admin2:
                location = loc
                break

    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
        if await _send_wttr_weather(message, api, raw_query):
            return
        await message.reply_text("❌ Location coordinates are unavailable.")
        return
    weather_url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "timezone": "auto",
    })
    weather = await api.get(weather_url, cache_response=False)
    current = (weather or {}).get("current") or (weather or {}).get("current_weather")
    if not current:
        if await _send_wttr_weather(message, api, raw_query):
            return
        await message.reply_text("❌ Current weather is unavailable right now.")
        return

    place_parts = [location.get("name"), location.get("admin1"), location.get("country")]
    place = ", ".join(dict.fromkeys(part for part in place_parts if part))
    temperature = current.get("temperature_2m", current.get("temperature"))
    apparent = current.get("apparent_temperature")
    wind = current.get("wind_speed_10m", current.get("windspeed"))
    code = current.get("weather_code", current.get("weathercode"))
    humidity = current.get("relative_humidity_2m")
    lines = [
        f"🌦️ <b>Weather: {esc(place or city_name)}</b>",
        f"Condition: {esc(weather_description(code))}",
        f"Temperature: {temperature}°C" if temperature is not None else "Temperature: unavailable",
    ]
    if apparent is not None:
        lines.append(f"Feels like: {apparent}°C")
    lines.append(f"Wind: {wind} km/h" if wind is not None else "Wind: unavailable")
    if humidity is not None:
        lines.append(f"Humidity: {humidity}%")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
