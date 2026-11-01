import os
import re
from datetime import date
from typing import Optional, Sequence
from urllib.parse import quote, urlencode

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
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(query, safe="")
    data = await api.get(url)
    if not data or data.get("type", "").endswith("/not_found"):
        await message.reply_text("❌ No Wikipedia page found.")
        return

    title, extract, page, thumbnail = wiki_summary_parts(data, query)
    title = title[:120]
    text = f"<b>📚 {esc(title)}</b>\n\n{esc(extract)}"
    if page:
        text += f'\n\n<a href="{esc(page)}">Open Wikipedia</a>'
    if thumbnail:
        await message.reply_photo(photo=thumbnail, caption=text, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def send_definition(message: Message, api: PokeAPI, word: str) -> None:
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word, safe='')}"
    data = await api.get(url)
    if not isinstance(data, list) or not data:
        fallback_url = "https://en.wiktionary.org/w/api.php?" + urlencode({
            "action": "query",
            "format": "json",
            "prop": "extracts",
            "explaintext": 1,
            "redirects": 1,
            "titles": word,
        })
        fallback = wiktionary_english_extract(await api.get(fallback_url))
        if fallback is None:
            await message.reply_text("❌ No definition found, or dictionary services are unavailable.")
            return
        title, extract = fallback
        await message.reply_text(f"📖 {title} (Wiktionary)\n\n{extract[:3800]}")
        return

    entry = data[0]
    phonetic = next((item.get("text") for item in entry.get("phonetics", []) if item.get("text")), "")
    lines = []
    for meaning in entry.get("meanings", [])[:3]:
        definitions = meaning.get("definitions", [])
        if not definitions:
            continue
        definition = definitions[0]
        line = f"• <b>{esc(meaning.get('partOfSpeech', ''))}</b>: {esc(definition.get('definition', '')[:300])}"
        example = definition.get("example")
        if example:
            line += f"\n  <i>Example:</i> {esc(example[:180])}"
        lines.append(line)
    text = f"<b>📖 {esc(word.title())}</b> {esc(phonetic)}\n\n" + ("\n\n".join(lines) or "No definition text available.")
    await message.reply_text(text[:3900], parse_mode=ParseMode.HTML)


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


async def send_fact(message: Message, api: PokeAPI) -> None:
    data = await api.get("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en", cache_response=False)
    fact = data.get("text") if data else None
    await message.reply_text(f"🧠 {fact[:3500]}" if fact else "❌ Fact service is unavailable right now.")


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


async def send_weather(message: Message, api: PokeAPI, city: str) -> None:
    geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
        "name": city,
        "count": 1,
        "language": "en",
        "format": "json",
    })
    geocoding = await api.get(geo_url)
    locations = (geocoding or {}).get("results") or []
    if not locations:
        await message.reply_text("❌ City not found. Try a city name with its country, such as /weather Paris France.")
        return

    location = locations[0]
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
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
        await message.reply_text("❌ Current weather is unavailable right now.")
        return

    place_parts = [location.get("name"), location.get("admin1"), location.get("country")]
    place = ", ".join(dict.fromkeys(part for part in place_parts if part))
    temperature = current.get("temperature_2m", current.get("temperature"))
    wind = current.get("wind_speed_10m", current.get("windspeed"))
    code = current.get("weather_code", current.get("weathercode"))
    humidity = current.get("relative_humidity_2m")
    lines = [
        f"🌦️ <b>Weather: {esc(place or city)}</b>",
        f"Condition: {esc(weather_description(code))}",
        f"Temperature: {temperature}°C" if temperature is not None else "Temperature: unavailable",
        f"Wind: {wind} km/h" if wind is not None else "Wind: unavailable",
    ]
    if humidity is not None:
        lines.append(f"Humidity: {humidity}%")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
