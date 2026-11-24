"""Человеческие подписи к сырым данным визита: устройство, источник, страна.

Без внешних библиотек: строка браузера разбирается по нескольким десяткам
признаков, которых хватает для отчёта («Chrome 131 · Android 14 · Samsung
SM-A536B»). Где браузер сам прислал Client Hints (Chromium), они точнее строки
и берутся первыми: в урезанной строке Android-модель всегда «K», а Windows 11
выдаёт себя за 10.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# ── Устройство ───────────────────────────────────────────────────────────────

_BOT = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|preview|headless|lighthouse|pagespeed|"
    r"python-|curl/|wget/|httpx|axios|node-fetch|go-http|java/|okhttp|"
    r"facebookexternalhit|whatsapp/|telegrambot|vkshare|yandex(?!browser|searchbrowser)|"
    r"monitor|uptime|pingdom|statuscake|phantomjs|playwright|puppeteer",
    re.IGNORECASE,
)

#: (метка в строке, название, шаблон версии). Порядок важен: Яндекс, Edge и
#: Opera пишут в строку ещё и «Chrome», Chrome пишет «Safari».
_BROWSERS: list[tuple[str, str, str]] = [
    ("Instagram", "Instagram (встроенный)", r"Instagram ([\d.]+)"),
    ("FBAN", "Facebook (встроенный)", r"FBAV/([\d.]+)"),
    ("FBAV", "Facebook (встроенный)", r"FBAV/([\d.]+)"),
    ("Telegram", "Telegram (встроенный)", r"Telegram[- ]?(?:Android|iOS)?/?([\d.]*)"),
    ("WhatsApp", "WhatsApp (встроенный)", r"WhatsApp/([\d.]+)"),
    ("TikTok", "TikTok (встроенный)", r"(?:musical_ly|TikTok)[_/]([\d.]+)"),
    ("YaSearchBrowser", "Яндекс (приложение)", r"YaSearchBrowser/([\d.]+)"),
    ("YaBrowser", "Яндекс Браузер", r"YaBrowser/([\d.]+)"),
    ("SamsungBrowser", "Samsung Internet", r"SamsungBrowser/([\d.]+)"),
    ("MiuiBrowser", "MIUI Browser", r"MiuiBrowser/([\d.]+)"),
    ("HuaweiBrowser", "Huawei Browser", r"HuaweiBrowser/([\d.]+)"),
    ("EdgA", "Edge", r"EdgA/([\d.]+)"),
    ("EdgiOS", "Edge", r"EdgiOS/([\d.]+)"),
    ("Edg/", "Edge", r"Edg/([\d.]+)"),
    ("OPR/", "Opera", r"OPR/([\d.]+)"),
    ("OPX/", "Opera", r"OPX/([\d.]+)"),
    ("Opera", "Opera", r"Version/([\d.]+)"),
    ("Vivaldi", "Vivaldi", r"Vivaldi/([\d.]+)"),
    ("Firefox/", "Firefox", r"Firefox/([\d.]+)"),
    ("FxiOS", "Firefox", r"FxiOS/([\d.]+)"),
    ("CriOS", "Chrome", r"CriOS/([\d.]+)"),
    ("Chrome/", "Chrome", r"Chrome/([\d.]+)"),
    ("Safari/", "Safari", r"Version/([\d.]+)"),
]

_VENDORS: list[tuple[str, str]] = [
    (r"^SM-|^GT-|^Galaxy", "Samsung"),
    (r"^Redmi|^M2\d{3}|^2\d{3}[A-Z0-9]{4,}|^Mi |^MI |^POCO|^Xiaomi", "Xiaomi"),
    (r"^Pixel", "Google"),
    (r"^HUAWEI|^ELE-|^VOG-|^ANA-|^NOH-|^JNY-|^MAR-|^LYA-|^NOVA", "Huawei"),
    (r"^HONOR|^HRY-|^RMO-|^ANY-|^CMA-|^LGE-", "Honor"),
    (r"^RMX", "Realme"),
    (r"^CPH|^OPPO", "OPPO"),
    (r"^V2\d{3}|^vivo", "vivo"),
    (r"^Infinix", "Infinix"),
    (r"^TECNO", "Tecno"),
    (r"^moto|^Moto|^XT\d", "Motorola"),
    (r"^Nokia|^TA-", "Nokia"),
    (r"^ONEPLUS|^OnePlus|^KB\d|^LE\d|^NE\d|^CPH2\d{3}", "OnePlus"),
    (r"^itel", "itel"),
    (r"^ZTE|^Blade", "ZTE"),
    (r"^Lenovo|^TB-", "Lenovo"),
]


@dataclass
class Device:
    browser: str = ""
    browser_version: str = ""
    os: str = ""
    os_version: str = ""
    device_type: str = ""  # «Компьютер», «Телефон», «Планшет», «Бот»
    vendor: str = ""
    model: str = ""
    is_bot: bool = False

    @property
    def label(self) -> str:
        parts = [self.vendor, self.model]
        return " ".join(p for p in parts if p)


def _major(version: str, parts: int = 1) -> str:
    pieces = [p for p in version.split(".") if p]
    return ".".join(pieces[:parts])


def parse_device(user_agent: str, hints: dict | None = None, touch_points: int = 0) -> Device:
    ua = user_agent or ""
    hints = hints or {}
    device = Device()

    if not ua or _BOT.search(ua) or hints.get("webdriver"):
        device.is_bot = True
        device.device_type = "Бот"

    for marker, name, pattern in _BROWSERS:
        if marker in ua:
            device.browser = name
            match = re.search(pattern, ua)
            device.browser_version = _major(match.group(1)) if match and match.group(1) else ""
            break

    # Система.
    if match := re.search(r"Windows NT ([\d.]+)", ua):
        device.os = "Windows"
        device.os_version = {"10.0": "10", "6.3": "8.1", "6.2": "8", "6.1": "7"}.get(match.group(1), match.group(1))
        platform_version = str(hints.get("platformVersion") or "")
        if device.os_version == "10" and platform_version and int(_major(platform_version) or 0) >= 13:
            device.os_version = "11"
    elif match := re.search(r"(?:iPhone|CPU) OS (\d+)[_.](\d+)", ua):
        device.os = "iPadOS" if "iPad" in ua else "iOS"
        device.os_version = f"{match.group(1)}.{match.group(2)}"
    elif match := re.search(r"Android ([\d.]+)", ua):
        device.os = "Android"
        device.os_version = _major(match.group(1), 2)
    elif "CrOS" in ua:
        device.os = "ChromeOS"
    elif match := re.search(r"Mac OS X (\d+)[_.](\d+)", ua):
        device.os = "macOS"
        # Safari и Chrome замерли на 10.15.7 — настоящая версия только в подсказке.
        platform_version = str(hints.get("platformVersion") or "")
        device.os_version = _major(platform_version, 2) if platform_version else ""
    elif "Linux" in ua:
        device.os = "Linux"

    # Тип и модель.
    if device.is_bot:
        pass
    elif "iPad" in ua or (device.os == "macOS" and touch_points > 1):
        # iPadOS 13+ в Safari притворяется компьютером; выдают его касания.
        device.device_type, device.vendor, device.model = "Планшет", "Apple", "iPad"
        if device.os == "macOS":
            device.os = "iPadOS"
    elif "iPhone" in ua:
        device.device_type, device.vendor, device.model = "Телефон", "Apple", "iPhone"
    elif device.os == "Android":
        mobile = hints.get("mobile")
        is_phone = bool(mobile) if mobile is not None else "Mobile" in ua
        device.device_type = "Телефон" if is_phone else "Планшет"
        model = str(hints.get("model") or "").strip()
        if not model:
            match = re.search(r"Android [\d.]+;(?: [a-z]{2}[-_][A-Za-z]{2};)? ([^;)]+?)(?: Build/[^;)]*)?[;)]", ua)
            model = match.group(1).strip() if match else ""
        if model and model != "K":
            device.model = model[:64]
            for pattern, vendor in _VENDORS:
                if re.search(pattern, model):
                    device.vendor = vendor
                    break
    elif hints.get("mobile") is True or "Mobi" in ua:
        device.device_type = "Телефон"
    else:
        device.device_type = "Компьютер"
        if device.os == "macOS":
            device.vendor = "Apple"
    return device


# ── Источник ─────────────────────────────────────────────────────────────────

CHANNEL_DIRECT = "Прямой заход"
CHANNEL_SEARCH = "Поиск"
CHANNEL_SOCIAL = "Соцсети и мессенджеры"
CHANNEL_ADS = "Реклама"
CHANNEL_EMAIL = "Рассылка"
CHANNEL_REFERRAL = "Ссылки с сайтов"
CHANNEL_UTM = "Другие метки UTM"

_SEARCH_HOSTS = {
    "google": "Google",
    "yandex": "Яндекс",
    "ya": "Яндекс",
    "bing": "Bing",
    "duckduckgo": "DuckDuckGo",
    "go.mail": "Mail.ru",
    "mail": "Mail.ru",
    "yahoo": "Yahoo",
    "baidu": "Baidu",
    "ecosia": "Ecosia",
    "search.brave": "Brave",
    "perplexity": "Perplexity",
    "chatgpt": "ChatGPT",
    "chat.openai": "ChatGPT",
    "claude": "Claude",
    "gemini.google": "Gemini",
    "copilot.microsoft": "Copilot",
}

_SOCIAL_HOSTS = {
    "instagram": "Instagram",
    "l.instagram": "Instagram",
    "facebook": "Facebook",
    "l.facebook": "Facebook",
    "lm.facebook": "Facebook",
    "m.facebook": "Facebook",
    "fb": "Facebook",
    "t": "Telegram",
    "telegram": "Telegram",
    "web.telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "wa": "WhatsApp",
    "web.whatsapp": "WhatsApp",
    "vk": "ВКонтакте",
    "m.vk": "ВКонтакте",
    "ok": "Одноклассники",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "m.youtube": "YouTube",
    "youtu": "YouTube",
    "linkedin": "LinkedIn",
    "lnkd": "LinkedIn",
    "twitter": "X (Twitter)",
    "x": "X (Twitter)",
    "threads": "Threads",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "dzen": "Дзен",
    "threads.net": "Threads",
}

_ADS_MEDIUMS = {
    "cpc", "ppc", "paid", "paidsearch", "paid_search", "paidsocial", "paid_social",
    "cpm", "cpa", "cpv", "display", "banner", "ads", "ad", "retargeting", "remarketing",
    "target", "таргет", "реклама", "context", "контекст",
}
_EMAIL_MEDIUMS = {"email", "e-mail", "mail", "newsletter", "рассылка"}
_SOCIAL_MEDIUMS = {"social", "smm", "sm", "social-network", "соцсети", "stories", "post", "bio"}

#: Метки клика рекламных систем → (источник, канал).
CLICK_IDS: dict[str, tuple[str, str]] = {
    "gclid": ("Google Ads", CHANNEL_ADS),
    "gbraid": ("Google Ads", CHANNEL_ADS),
    "wbraid": ("Google Ads", CHANNEL_ADS),
    "yclid": ("Яндекс Директ", CHANNEL_ADS),
    "fbclid": ("Facebook / Instagram", CHANNEL_SOCIAL),
    "ttclid": ("TikTok Ads", CHANNEL_ADS),
    "msclkid": ("Microsoft Ads", CHANNEL_ADS),
    "vkclid": ("VK Реклама", CHANNEL_ADS),
}

#: Встроенные браузеры приложений: реферера нет, но откуда человек — ясно.
_IN_APP_SOURCES = {
    "Instagram (встроенный)": "Instagram",
    "Facebook (встроенный)": "Facebook",
    "Telegram (встроенный)": "Telegram",
    "WhatsApp (встроенный)": "WhatsApp",
    "TikTok (встроенный)": "TikTok",
}


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _lookup(host: str, table: dict[str, str]) -> str:
    """`l.instagram.com` → Instagram, `google.kz` → Google: имя без зоны и поддоменов."""
    labels = host.split(".")
    for size in range(len(labels) - 1, 0, -1):
        key = ".".join(labels[:size])
        if key in table:
            return table[key]
        tail = ".".join(labels[-size - 1 : -1]) if size < len(labels) else ""
        if tail in table:
            return table[tail]
    return ""


@dataclass
class Source:
    channel: str
    source: str
    medium: str = ""
    campaign: str = ""
    term: str = ""
    content: str = ""
    click_id: str = ""
    referrer_host: str = ""


def classify_source(
    referrer: str,
    utm: dict[str, str] | None,
    click_ids: list[str] | None,
    own_hosts: set[str],
    browser: str = "",
) -> Source:
    utm = {k: (v or "").strip()[:255] for k, v in (utm or {}).items()}
    medium = utm.get("medium", "")
    medium_key = medium.lower()
    ref_host = host_of(referrer)
    if ref_host in own_hosts:
        ref_host, referrer = "", ""
    result = Source(
        channel=CHANNEL_DIRECT,
        source="",
        medium=medium,
        campaign=utm.get("campaign", ""),
        term=utm.get("term", ""),
        content=utm.get("content", ""),
        referrer_host=ref_host,
    )

    click = next((c for c in (click_ids or []) if c in CLICK_IDS), "")
    result.click_id = click

    search = _lookup(ref_host, _SEARCH_HOSTS) if ref_host else ""
    social = _lookup(ref_host, _SOCIAL_HOSTS) if ref_host else ""

    if utm.get("source"):
        result.source = utm["source"]
        if medium_key in _ADS_MEDIUMS or (click and CLICK_IDS[click][1] == CHANNEL_ADS):
            result.channel = CHANNEL_ADS
        elif medium_key in _EMAIL_MEDIUMS:
            result.channel = CHANNEL_EMAIL
        elif medium_key in _SOCIAL_MEDIUMS or social or _lookup(utm["source"].lower(), _SOCIAL_HOSTS):
            result.channel = CHANNEL_SOCIAL
        elif medium_key in {"organic", "seo"} or search:
            result.channel = CHANNEL_SEARCH
        else:
            result.channel = CHANNEL_UTM
    elif click:
        result.source, result.channel = CLICK_IDS[click]
    elif search:
        result.source, result.channel = search, CHANNEL_SEARCH
    elif social:
        result.source, result.channel = social, CHANNEL_SOCIAL
    elif ref_host:
        result.source, result.channel = ref_host, CHANNEL_REFERRAL
    elif browser in _IN_APP_SOURCES:
        result.source, result.channel = _IN_APP_SOURCES[browser], CHANNEL_SOCIAL
    else:
        result.source = "—"
    return result


# ── Место ────────────────────────────────────────────────────────────────────

COUNTRIES = {
    "KZ": "Казахстан", "RU": "Россия", "UZ": "Узбекистан", "KG": "Кыргызстан",
    "TJ": "Таджикистан", "TM": "Туркменистан", "AZ": "Азербайджан", "AM": "Армения",
    "GE": "Грузия", "BY": "Беларусь", "UA": "Украина", "MD": "Молдова",
    "MN": "Монголия", "CN": "Китай", "TR": "Турция", "AE": "ОАЭ", "US": "США",
    "CA": "Канада", "GB": "Великобритания", "DE": "Германия", "FR": "Франция",
    "NL": "Нидерланды", "IT": "Италия", "ES": "Испания", "PL": "Польша",
    "CZ": "Чехия", "FI": "Финляндия", "SE": "Швеция", "NO": "Норвегия",
    "LT": "Литва", "LV": "Латвия", "EE": "Эстония", "IL": "Израиль",
    "IN": "Индия", "KR": "Южная Корея", "JP": "Япония", "SG": "Сингапур",
    "TH": "Таиланд", "VN": "Вьетнам", "ID": "Индонезия", "MY": "Малайзия",
    "SA": "Саудовская Аравия", "QA": "Катар", "EG": "Египет", "CY": "Кипр",
    "AT": "Австрия", "CH": "Швейцария", "BE": "Бельгия", "IE": "Ирландия",
    "PT": "Португалия", "GR": "Греция", "RS": "Сербия", "BR": "Бразилия",
    "AU": "Австралия", "HK": "Гонконг", "IR": "Иран", "AF": "Афганистан", "PK": "Пакистан",
    "T1": "Сеть Tor", "XX": "Не определена",
}


def country_name(code: str) -> str:
    code = (code or "").upper()
    if not code:
        return "Не определена"
    return COUNTRIES.get(code, code)


# ── Страница ─────────────────────────────────────────────────────────────────

_ID_SEGMENT = re.compile(r"^(?:\d+|[0-9a-f]{16,}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.IGNORECASE)


def page_key(path: str) -> str:
    """`/finance/contracts/8f3e…` → `/finance/contracts/:id`: страницы-карточки
    складываются в одну строку отчёта, а не в тысячу по строке на карточку."""
    path = (path or "/").split("?", 1)[0].split("#", 1)[0] or "/"
    parts = ["" if not seg else (":id" if _ID_SEGMENT.match(seg) else seg) for seg in path.split("/")]
    return "/".join(parts) or "/"


__all__ = [
    "CHANNEL_ADS",
    "CHANNEL_DIRECT",
    "CHANNEL_EMAIL",
    "CHANNEL_REFERRAL",
    "CHANNEL_SEARCH",
    "CHANNEL_SOCIAL",
    "CHANNEL_UTM",
    "CLICK_IDS",
    "Device",
    "Source",
    "classify_source",
    "country_name",
    "host_of",
    "page_key",
    "parse_device",
]
