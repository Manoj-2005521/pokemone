import html
import math
import re
from typing import Any, Optional, Sequence


WEATHER_CODE = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Heavy showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}

FX_CURRENCIES_URL = "https://api.frankfurter.app/currencies"
FX_ALIASES = {
    "usd": "USD", "us dollar": "USD", "us dollars": "USD",
    "american dollar": "USD", "american dollars": "USD", "dollar": "USD",
    "dollars": "USD", "$": "USD", "us$": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "€": "EUR",
    "gbp": "GBP", "british pound": "GBP", "pound sterling": "GBP",
    "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "£": "GBP",
    "jpy": "JPY", "japanese yen": "JPY", "yen": "JPY", "yens": "JPY", "¥": "JPY",
    "cny": "CNY", "chinese yuan": "CNY", "yuan": "CNY", "renminbi": "CNY", "rmb": "CNY", "cn¥": "CNY",
    "cad": "CAD", "canadian dollar": "CAD", "canadian dollars": "CAD", "ca$": "CAD",
    "aud": "AUD", "australian dollar": "AUD", "australian dollars": "AUD", "a$": "AUD",
    "inr": "INR", "indian rupee": "INR", "indian rupees": "INR", "rupee": "INR", "rupees": "INR", "₹": "INR",
    "brl": "BRL", "brazilian real": "BRL", "brazilian reais": "BRL", "real": "BRL", "reais": "BRL", "r$": "BRL",
    "chf": "CHF", "swiss franc": "CHF", "swiss francs": "CHF",
    "mxn": "MXN", "mexican peso": "MXN", "mexican pesos": "MXN", "mx$": "MXN",
    "sgd": "SGD", "singapore dollar": "SGD", "singapore dollars": "SGD", "s$": "SGD",
    "nzd": "NZD", "new zealand dollar": "NZD", "new zealand dollars": "NZD", "nz$": "NZD",
    "zar": "ZAR", "south african rand": "ZAR", "rand": "ZAR", "rands": "ZAR",
    "krw": "KRW", "south korean won": "KRW", "won": "KRW", "₩": "KRW",
    "try": "TRY", "turkish lira": "TRY", "lira": "TRY", "₺": "TRY",
    "aed": "AED", "uae dirham": "AED", "dirham": "AED",
}


def pretty(name: str) -> str:
    return name.replace("-", " ").title()


def esc(value: str) -> str:
    return html.escape(value or "")


def _fx_key(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _fx_code(value: str, currencies: Optional[dict[str, str]] = None) -> str:
    key = _fx_key(value)
    if key in FX_ALIASES:
        return FX_ALIASES[key]
    if len(key) == 3 and key.isalpha():
        return key.upper()
    for code, name in (currencies or {}).items():
        if _fx_key(name) == key:
            return code.upper()
    raise ValueError(f"Unknown currency '{value}'. Use a 3-letter code or /fx currencies.")


def weather_description(code: Any) -> str:
    return WEATHER_CODE.get(code, f"Unknown condition (code {code})")


def parse_fx_input(
    args: Sequence[str], currencies: Optional[dict[str, str]] = None,
) -> tuple[str, str, float]:
    if not args:
        raise ValueError("Include an amount and two currencies.")

    raw = " ".join(args).strip()
    amount_pattern = re.compile(r"(?<![\w.])([+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+))(?![\w.])")
    matches = list(amount_pattern.finditer(raw))
    if len(matches) != 1:
        raise ValueError("Include exactly one amount, such as 100.")

    amount_text = matches[0].group(1).replace(",", "")
    try:
        amount = float(amount_text)
    except ValueError as error:
        raise ValueError("Amount must be a positive number.") from error
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Amount must be a positive number.")

    without_amount = raw[:matches[0].start()] + raw[matches[0].end():]
    split = re.split(r"\s+to\s+", without_amount, maxsplit=1, flags=re.IGNORECASE)
    if len(split) == 2:
        source_text, target_text = split
    else:
        remaining = []
        for argument in args:
            cleaned = amount_pattern.sub("", argument, count=1).strip()
            if cleaned:
                remaining.append(cleaned)
        if len(remaining) != 2:
            raise ValueError("Use /fx 100 USD to EUR or /fx USD EUR 100.")
        source_text, target_text = remaining

    source, target = _fx_code(source_text, currencies), _fx_code(target_text, currencies)
    return source, target, amount


def quote_parts(data: Any) -> Optional[tuple[str, str]]:
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    content = data.get("quote") or data.get("content") or data.get("q")
    if not content:
        return None
    author = data.get("author") or data.get("a") or "Unknown"
    return str(content), str(author)


def wiki_summary_parts(
    data: dict[str, Any], fallback_title: str,
) -> tuple[str, str, Optional[str], Optional[str]]:
    title = data.get("title") or fallback_title
    extract = (data.get("extract") or "No summary available.").strip()
    if len(extract) > 650:
        extract = extract[:647].rstrip() + "..."
    page = (((data.get("content_urls") or {}).get("desktop") or {}).get("page"))
    thumbnail = (data.get("thumbnail") or {}).get("source")
    return title, extract, page, thumbnail


def wiktionary_english_extract(data: Any) -> Optional[tuple[str, str]]:
    if not isinstance(data, dict):
        return None
    pages = ((data.get("query") or {}).get("pages") or {})
    if not isinstance(pages, dict):
        return None

    for page in pages.values():
        if not isinstance(page, dict) or "missing" in page:
            continue
        lines = (page.get("extract") or "").splitlines()
        english_start = next((i for i, line in enumerate(lines) if line.strip() == "== English =="), None)
        if english_start is None:
            continue

        english_lines = []
        for line in lines[english_start + 1:]:
            if line.startswith("== ") and line.endswith(" =="):
                break
            if line.strip() and not line.startswith("==="):
                english_lines.append(line.strip())

        extract = " ".join(english_lines).strip()
        if extract:
            return str(page.get("title") or "Wiktionary entry"), extract
    return None
