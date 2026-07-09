import asyncio
import os
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Flask keep-alive
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)
KEEP_ALIVE_PORT = 8000


@flask_app.route("/")
def home():
    return "Bot is alive!", 200


def keep_alive():
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=KEEP_ALIVE_PORT),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# API base URLs & Turkey bounding box
# ---------------------------------------------------------------------------
OPENSKY_BASE  = "https://opensky-network.org/api"
AIRLABS_BASE  = "https://airlabs.co/api/v9"
TURKEY_BOUNDS = {"lamin": 35.8, "lamax": 42.2, "lomin": 25.6, "lomax": 44.8}
TURKEY_BBOX_AL = "25.6,35.8,44.8,42.2"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALT_MAX_M_UAV   = 7_315.0   # 24 000 ft
ALT_MAX_M_OTHER = 8_229.6   # 27 000 ft

CATEGORY_LABELS: dict[int, str] = {
    0:  "Bilinmiyor / Özel",
    1:  "Hafif Uçak",
    5:  "Planör",
    7:  "Helikopter",
    13: "İHA / Drone",
}

TZ_TR = timezone(timedelta(hours=3))

MEYDAN_NAMES: dict[str, str] = {
    "LTCP": "Elazığ",
    "LTCI": "Van",
    "LTCW": "Şırnak / Cizre",
    "LTAJ": "Gaziantep",
    "LTBD": "Aydın",
    "LTBF": "Balıkesir",
}

_MAX_MSG      = 4000
_MAX_HIST_BLK = 1400
_KNOWN_PREFIXES = ("J", "T", "SGK", "ORMAN", "BATU")

_ICAO_WHITELIST: dict[str, str] = {
    "4b8392": "J",     # Jandarma
    "4b8394": "J",     # Jandarma (TC-J3 / ASLAN03)
    "4b83a4": "SGK",   # SGK (TCSG-572)
    "4b8362": "DZKK",  # Deniz Kuvvetleri Komutanlığı (TC-S48 / BATU)
}

_MILITARY_CALL_PREFIXES: tuple[str, ...] = (
    "ANKA", "AKINCI", "AKSUNGUR", "TB2", "BAYRAKTAR", "THK", "TUAF", "BATU"
)

_PREFIX_LABEL_MAP: dict[str, str] = {
    "BATU": "⚓ DZKK / Deniz Kuvvetleri",
}

# ---------------------------------------------------------------------------
# OpenSky HTTP Basic Auth
# ---------------------------------------------------------------------------
def _opensky_auth() -> tuple[str, str] | None:
    user = os.environ.get("OPENSKY_USER", "").strip()
    pwd  = os.environ.get("OPENSKY_PASS", "").strip()
    return (user, pwd) if user and pwd else None


# ---------------------------------------------------------------------------
# AirLabs Helper and Cache
# ---------------------------------------------------------------------------
_HELI_ICAO_CODES: frozenset[str] = frozenset({
    "AS32","AS35","AS50","AS55","AS65",
    "EC13","EC20","EC25","EC30","EC35","EC45","EC55","EC75",
    "H120","H125","H130","H135","H145","H155","H160","H175","H215","H225",
    "B06","B06T","B07","B09","B12","B14","B22","B23","B29","B30",
    "B47","B72","B74","B76","BHT2",
    "S55","S58","S61","S70","S76","S92","H60","UH60","SH60","CH47",
    "A109","A119","A129","A139","A169","A189","AW09","AW13","AW19","AW69","AW89",
    "MI8","MI17","MI24","MI26","MI28","MI35","KA32","KA50","KA52",
    "H500","MD50","MD60","R22","R44","R66","S300","S333","S369","T129","EXEC","LAMA","GYRO",
})

_flight_info_cache: dict[str, dict] = {}


def _airlabs_to_state(f: dict) -> list | None:
    icao24 = (f.get("hex") or "").lower().strip()
    if not icao24:
        return None
    cs     = (f.get("flight_icao") or f.get("flight_iata") or "").strip()
    lat    = f.get("lat")
    lon    = f.get("lng")
    alt_m  = f.get("alt")
    speed  = f.get("speed")
    vel_ms = (speed / 3.6) if speed is not None else None
    ts     = f.get("updated")
    on_gnd = bool(alt_m is not None and alt_m < 15)
    ac_typ = (f.get("aircraft_icao") or "").upper().strip()
    cat    = 7 if ac_typ in _HELI_ICAO_CODES else 0

    return [
        icao24, cs.ljust(8)[:8], "Turkey", ts, ts, lon, lat, alt_m,
        on_gnd, vel_ms, f.get("dir"), f.get("v_speed"), None, alt_m,
        f.get("squawk"), False, 0, cat, True
    ]


def _get_states_airlabs() -> list | None:
    api_key = os.environ.get("AIRLABS_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        r = requests.get(
            f"{AIRLABS_BASE}/flights",
            params={"api_key": api_key, "_bbox": TURKEY_BBOX_AL},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if "error" in data:
            return None
        raw = data.get("response")
        if raw is None:
            return None

        new_cache: dict[str, dict] = {}
        states: list = []
        for f in raw:
            sv = _airlabs_to_state(f)
            if sv is None:
                continue
            dep = f.get("dep_iata") or f.get("dep_icao") or "N/A"
            arr = f.get("arr_iata") or f.get("arr_icao") or "N/A"
            new_cache[sv[0]] = {
                "dep_airport": dep,
                "arr_airport": arr,
                "dep_time":    "N/A",
                "arr_time":    "N/A",
            }
            states.append(sv)

        global _flight_info_cache
        _flight_info_cache = new_cache
        return states
    except Exception:
        return None

# Nominatim semaphore
_NOM_SEM: asyncio.Semaphore | None = None


def get_nom_sem() -> asyncio.Semaphore:
    global _NOM_SEM
    if _NOM_SEM is None:
        _NOM_SEM = asyncio.Semaphore(1)
    return _NOM_SEM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ts_to_tr(ts: int | None) -> str:
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=TZ_TR).strftime("%H:%M")


def now_tr() -> str:
    return datetime.now(tz=TZ_TR).strftime("%H:%M")


def category_label(cat: int | None) -> str:
    return CATEGORY_LABELS.get(cat or 0, "Bilinmiyor / Özel")


def resolve_label(s: list) -> str:
    icao24 = (s[0] or "").lower().strip()
    cs     = _callsign(s)
    cat    = (s[17] if len(s) > 17 else None) or 0

    if _ICAO_WHITELIST.get(icao24) == "DZKK" or cs.startswith("BATU"):
        return "⚓ DZKK / Deniz Kuvvetleri"
    if _ICAO_WHITELIST.get(icao24) == "J" or cs.startswith("J"):
        return "🟢 Jandarma Genel K."
    if _ICAO_WHITELIST.get(icao24) == "SGK" or cs.startswith("SGK"):
        return "🟡 Sahil Güvenlik K."

    for prefix, label in _PREFIX_LABEL_MAP.items():
        if cs.startswith(prefix):
            return label
    if any(cs.startswith(p) for p in _MILITARY_CALL_PREFIXES):
        return "🪖 Askeri İHA / Taktik"
    if cat == 0 and not _is_airlabs(s):
        return "🪖 Askeri İHA / Taktik"
    return CATEGORY_LABELS.get(cat, "Bilinmiyor / Özel")


_MDV2_RESERVED = r"\_*[]()~`>#+-=|{}.!"


def e(text: str) -> str:
    for ch in _MDV2_RESERVED:
        text = text.replace(ch, f"\\{ch}")
    return text


def approx_region(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "Konum bilinmiyor"
    if lon < 30.5:
        return "Marmara" if lat >= 40.0 else "Ege"
    if lon < 33.0:
        if lat >= 40.5: return "Batı Karadeniz"
        if lat >= 38.5: return "İç Anadolu (Batı)"
        return "Akdeniz (Batı)"
    if lon < 36.5:
        if lat >= 40.5: return "Orta Karadeniz"
        if lat >= 38.0: return "İç Anadolu"
        return "Akdeniz"
    if lon < 40.5:
        if lat >= 40.0: return "Doğu Karadeniz"
        if lat >= 37.5: return "Doğu Anadolu (Batı)"
        return "Güneydoğu Anadolu (Batı)"
    return "Kuzeydoğu Anadolu" if lat >= 38.5 else "Güneydoğu Anadolu"


# ---------------------------------------------------------------------------
# State fetchers
# ---------------------------------------------------------------------------
def _get_states_opensky() -> list | None:
    r = requests.get(
        f"{OPENSKY_BASE}/states/all",
        params=TURKEY_BOUNDS,
        auth=_opensky_auth(),
        timeout=15,
    )
    if r.status_code == 429:
        return None
    r.raise_for_status()
    return r.json().get("states") or []


def _get_states() -> list | None:
    airlabs_states = _get_states_airlabs()
    if airlabs_states is not None:
        return airlabs_states
        
    return _get_states_opensky()


def _get_flight_info(icao24: str) -> dict:
    empty = {"dep_airport": "N/A", "arr_airport": "N/A", "dep_time": "N/A", "arr_time": "N/A"}
    if not icao24 or icao24 == "N/A":
        return empty
    cached = _flight_info_cache.get(icao24.lower())
    if cached:
        return cached
    try:
        now   = int(time.time())
        begin = now - 12 * 3600
        r = requests.get(
            f"{OPENSKY_BASE}/flights/aircraft",
            params={"icao24": icao24, "begin": begin, "end": now},
            auth=_opensky_auth(),
            timeout=10,
        )
        if r.status_code != 200:
            return empty
        flights = r.json()
        if not flights:
            return empty
        latest = max(flights, key=lambda f: f.get("lastSeen") or 0)
        return {
            "dep_airport": latest.get("estDepartureAirport") or "N/A",
            "arr_airport": latest.get("estArrivalAirport")   or "N/A",
            "dep_time":    ts_to_tr(latest.get("firstSeen")),
            "arr_time":    ts_to_tr(latest.get("lastSeen")),
        }
    except Exception:
        return empty


def _get_location(lat: float, lon: float) -> str:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "accept-language": "tr", "zoom": 10},
            headers={"User-Agent": "TurkeyFlightBot/1.0 (telegram-bot)"},
            timeout=8,
        )
        if r.status_code != 200:
            return "N/A"
        addr = r.json().get("address", {})
        district = (addr.get("county") or addr.get("city_district") or addr.get("town") or addr.get("village") or "N/A")
        province = addr.get("state") or addr.get("province") or ""
        if province and district != "N/A":
            return f"{district}, {province}"
        return district
    except Exception:
        return "N/A"


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------
async def fetch_states() -> list | None:
    return await asyncio.get_event_loop().run_in_executor(None, _get_states)


async def fetch_flight_info(icao24: str) -> dict:
    return await asyncio.get_event_loop().run_in_executor(None, _get_flight_info, icao24)


async def fetch_location(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "N/A"
    async with get_nom_sem():
        await asyncio.sleep(0.15)
        return await asyncio.get_event_loop().run_in_executor(None, _get_location, lat, lon)


# ---------------------------------------------------------------------------
# Keyboards (Yenilenen İHA Menüsü)
# ---------------------------------------------------------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛰 İHA Takibi",          callback_data="menu_iha")],
        [InlineKeyboardButton("🛫 Meydan Takibi",       callback_data="menu_meydan")],
        [InlineKeyboardButton("🚁 Diğer Hava Araçları", callback_data="cat_diger")],
    ])


def iha_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Jandarma (J*)",   callback_data="iha_jandarma"),
         InlineKeyboardButton("🔵 KKK (T*)",        callback_data="iha_kkk")],
        [InlineKeyboardButton("🟡 SGK (SGK*)",       callback_data="iha_sgk"),
         InlineKeyboardButton("⚓ DZKK (BATU*)",     callback_data="iha_dzkk")],
        [InlineKeyboardButton("🟠 Orman (ORMAN*)",   callback_data="iha_orman"),
         InlineKeyboardButton("⚫ Diğer İHA",        callback_data="iha_diger")],
        [InlineKeyboardButton("🔴 Hepsi",            callback_data="iha_hepsi")],
        [InlineKeyboardButton("⬅️ Geri Dön",         callback_data="menu_main")],
    ])


def meydan_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛬 Elazığ (LTCP)",       callback_data="meydan_LTCP"),
         InlineKeyboardButton("🛬 Van (LTCI)",          callback_data="meydan_LTCI")],
        [InlineKeyboardButton("🛬 Şırnak/Cizre (LTCW)", callback_data="meydan_LTCW"),
         InlineKeyboardButton("🛬 Gaziantep (LTAJ)",    callback_data="meydan_LTAJ")],
        [InlineKeyboardButton("🛬 Aydın (LTBD)",        callback_data="meydan_LTBD"),
         InlineKeyboardButton("🛬 Balıkesir (LTBF)",    callback_data="meydan_LTBF")],
        [InlineKeyboardButton("⬅️ Geri Dön",           callback_data="menu_main")],
    ])


def result_kb(refresh_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Yenile",     callback_data=refresh_data)],
        [InlineKeyboardButton("⬅️ Geri Dön",  callback_data="menu_main")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Geri Dön", callback_data="menu_main")],
    ])


# ---------------------------------------------------------------------------
# Persistent message helpers
# ---------------------------------------------------------------------------
def _truncate_block(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        cost = len(line) + 1
        if used + cost > max_chars - 40:
            skipped = len(lines) - i
            kept.append(f"_\\.\\.\\. {e(str(skipped))} satır daha_")
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)


def _compose(context: ContextTypes.DEFAULT_TYPE, body: str) -> str:
    history: list[tuple[str, str]] = context.user_data.get("history", [])
    if not history:
        return body

    prev_title, prev_text = history[-1]
    label     = f"📋 _Önceki:_ *{e(prev_title)}*\n"
    separator = "\n━━━━━━━━━━━━━━━━━━━━━\n\n"

    body_budget = _MAX_MSG - len(label) - len(separator) - len(body)
    if body_budget < 0:
        body = _truncate_block(body, _MAX_MSG)
        return body

    hist_budget = min(_MAX_HIST_BLK, body_budget)
    prev_block  = _truncate_block(prev_text, hist_budget)

    return label + prev_block + separator + body


def _push_history(context: ContextTypes.DEFAULT_TYPE, title: str, text: str) -> None:
    history: list = context.user_data.setdefault("history", [])
    history.append((title, text))
    if len(history) > 2:
        history.pop(0)


async def _edit(context: ContextTypes.DEFAULT_TYPE, text: str, keyboard: InlineKeyboardMarkup) -> None:
    from telegram.error import BadRequest
    chat_id = context.user_data.get("chat_id")
    msg_id  = context.user_data.get("msg_id")
    if not chat_id or not msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text, parse_mode="MarkdownV2", reply_markup=keyboard
        )
    except BadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="MarkdownV2", reply_markup=keyboard
            )
            context.user_data["msg_id"] = msg.message_id
        except Exception:
            pass
    except Exception:
        pass


async def _edit_loading(context: ContextTypes.DEFAULT_TYPE, status_line: str, keyboard: InlineKeyboardMarkup) -> None:
    body = f"_{e(status_line)}_"
    await _edit(context, _compose(context, body), keyboard)


# ---------------------------------------------------------------------------
# Flight filtering
# ---------------------------------------------------------------------------
def _base_filter(s: list, alt_ceiling: float, allowed_cats: set[int]) -> bool:
    try:
        on_ground = s[8]
        alt_m     = s[7]
        cat       = (s[17] if len(s) > 17 else None) or 0
    except (IndexError, TypeError):
        return False
    if on_ground or s[2] != "Turkey":
        return False
    if alt_m is not None and alt_m > alt_ceiling:
        return False
    return cat in allowed_cats


def _callsign(s: list) -> str:
    return (s[1] or "").strip().upper()


def _is_airlabs(s: list) -> bool:
    return len(s) > 18 and s[18] is True


def filter_uavs(states: list) -> list:
    result = []
    for s in states:
        try:
            if s[8] or s[2] != "Turkey":
                continue
            alt_m = s[7]
            if alt_m is not None and alt_m > ALT_MAX_M_UAV:
                continue
            cat = (s[17] if len(s) > 17 else None) or 0

            if _is_airlabs(s):
                cs = _callsign(s)
                if not (cat == 13
                        or any(cs.startswith(p) for p in _KNOWN_PREFIXES)
                        or any(cs.startswith(p) for p in _MILITARY_CALL_PREFIXES)
                        or any(cs.startswith(p) for p in _PREFIX_LABEL_MAP)
                        or (s[0] or "").lower() in _ICAO_WHITELIST):
                    continue
            else:
                if cat not in {0, 13}:
                    continue
        except (IndexError, TypeError):
            continue
        result.append(s)
    return result


def filter_other(states: list) -> list:
    return [s for s in states if _base_filter(s, ALT_MAX_M_OTHER, {1, 5, 7})]


def by_prefix(flights: list, prefix: str) -> list:
    p = prefix.upper()
    result = [s for s in flights if _callsign(s).startswith(p) or _ICAO_WHITELIST.get((s[0] or "").lower()) == p]
    if p == "T":
        result = [s for s in result if not _callsign(s).startswith("THK")]
    return result


def diger_iha(flights: list) -> list:
    return [
        s for s in flights 
        if not any(_callsign(s).startswith(p) for p in _KNOWN_PREFIXES) 
        and (s[0] or "").lower() not in _ICAO_WHITELIST
    ]


def sort_flights(flights: list) -> list:
    def key(s):
        cs  = _callsign(s)
        icao24 = (s[0] or "").lower().strip()
        cat = (s[17] if len(s) > 17 else None) or 0
        if _ICAO_WHITELIST.get(icao24) == "DZKK" or cs.startswith("BATU"): return 0
        if _ICAO_WHITELIST.get(icao24) == "J" or cs.startswith("J"): return 1
        if cat == 0: return 2
        return 3
    return sorted(flights, key=key)


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def quick_list(flights: list, title: str) -> str:
    if not flights:
        return f"*{e(title)}*\n\n🔍 Şu an eşleşen uçuş bulunamadı\\."

    header = f"*{e(title)}*  —  {e(str(len(flights)))} uçuş\n"
    footer = "\n_Detay için uçuş numarasını yazın \\(örn\\: 1, 3\\)_"
    rows: list[str] = []
    shown = 0

    for i, s in enumerate(flights, 1):
        cs     = (s[1] or "N/A").strip() or "N/A"
        region = approx_region(s[6], s[5])
        label  = resolve_label(s)
        row    = f"{i}\\. *{e(cs)}*  —  {e(label)}  —  {e(region)}"

        if len(header + "\n".join(rows + [row]) + footer) > _MAX_MSG:
            rows.append(f"_\\.\\.\\. ve {e(str(len(flights) - shown))} uçuş daha_")
            break
        rows.append(row)
        shown += 1

    return header + "\n".join(rows) + footer


def detail_text(state: list, info: dict, location: str) -> str:
    icao24   = (state[0] or "N/A").strip()
    callsign = (state[1] or "N/A").strip() or "N/A"
    alt_m    = state[7]
    vel_ms   = state[9]
    alt_ft  = f"{alt_m  * 3.28084:,.0f} ft" if alt_m  is not None else "N/A"
    vel_kts = f"{vel_ms * 1.94384:.1f} kts"  if vel_ms is not None else "N/A"

    dep_ap = info["dep_airport"]
    arr_ap = info["arr_airport"]
    dep_t  = info["dep_time"]
    arr_t  = info["arr_time"]

    has_route = dep_ap != "N/A" or arr_ap != "N/A"
    route_str = f"🛫 {e(dep_ap)} {e(dep_t)} TR  →  🛬 {e(arr_ap)} {e(arr_t)} TR" if has_route else "🗺 Rota bilgisi mevcut değil"

    return (
        f"*✈️ Uçuş Detayı*\n\n"
        f"📡 *Çağrı İşareti:*  `{e(callsign)}`\n"
        f"🪪 *ICAO24:*  `{e(icao24)}`\n"
        f"🏷 *Kategori:*  {e(resolve_label(state))}\n"
        f"📍 *Konum:*  {e(location)}\n"
        f"📐 *İrtifa:*  {e(alt_ft)}\n"
        f"💨 *Hız:*  {e(vel_kts)}\n"
        f"🗺 *Rota:*  {route_str}"
    )


# ---------------------------------------------------------------------------
# Shared OpenSky fetch with inline error rendering
# ---------------------------------------------------------------------------
async def _fetch_states_safe(context: ContextTypes.DEFAULT_TYPE, error_kb: InlineKeyboardMarkup) -> list | None:
    try:
        states = await fetch_states()
    except requests.RequestException:
        body = "⚠️ *Uçuş verilerine ulaşılamadı*\n\n_Sunucular geçici olarak erişilemez durumda\\. Lütfen birkaç dakika sonra tekrar deneyin\\._"
        await _edit(context, _compose(context, body), error_kb)
        return None
    except Exception:
        body = "⚠️ *Beklenmedik bir hata oluştu*\n\n_Uçuş verisi alınamadı\\. Lütfen tekrar deneyin\\._"
        await _edit(context, _compose(context, body), error_kb)
        return None

    if states is None:
        body = "⏳ *İstek Sınırı / Kota Doldu*\n\n_Sunucu geçici olarak ücretsiz kota limitine ulaştı\\. Birkaç dakika sonra tekrar deneyin\\._"
        await _edit(context, _compose(context, body), error_kb)
        return None

    return states


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["chat_id"] = update.effective_chat.id

    body = "👋 *Türkiye Hava Takip Botu*\n\nBir kategori seçin:"
    msg  = await update.message.reply_text(body, parse_mode="MarkdownV2", reply_markup=main_menu_kb())
    context.user_data["msg_id"] = msg.message_id


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data

    context.user_data["chat_id"] = query.message.chat_id
    context.user_data["msg_id"]  = query.message.message_id

    if data == "menu_main":
        body = "👋 *Türkiye Hava Takip Botu*\n\nBir kategori seçin:"
        await _edit(context, _compose(context, body), main_menu_kb())
        return

    if data == "menu_iha":
        body = "🛰 *İHA Takibi*\n\nAlt kategori seçin:"
        await _edit(context, _compose(context, body), iha_menu_kb())
        return

    if data == "menu_meydan":
        body = "🛫 *Meydan Takibi*\n\nHangi havalimanını takip etmek istiyorsunuz?"
        await _edit(context, _compose(context, body), meydan_menu_kb())
        return

    if data.startswith("iha_"):
        error_kb = iha_menu_kb()
    elif data.startswith("meydan_"):
        error_kb = meydan_menu_kb()
    else:
        error_kb = main_menu_kb()

    await _edit_loading(context, "⏳ Veriler alınıyor…", error_kb)
    states = await _fetch_states_safe(context, error_kb)
    if states is None:
        return

    flights: list          = []
    title:   str           = ""
    cached_infos: dict     = {}

    if data.startswith("iha_"):
        uavs = filter_uavs(states)
        sub  = data[4:]
        mapping = {
            "jandarma": ("J",     "🟢 Jandarma İHA (J*)"),
            "kkk":      ("T",     "🔵 KKK Uçak/İHA (T*)"),
            "sgk":      ("SGK",   "🟡 SGK İHA (SGK*)"),
            "orman":    ("ORMAN", "🟠 Orman İHA (ORMAN*)"),
            "dzkk":     ("DZKK",  "⚓ DZKK İHA (BATU*)"),
        }
        if sub in mapping:
            prefix, title = mapping[sub]
            flights = by_prefix(uavs, prefix)
        elif sub == "diger":
            title, flights = "⚫ Diğer İHA", diger_iha(uavs)
        else:
            title, flights = "🔴 Tüm İHA'lar", uavs

    elif data.startswith("meydan_"):
        airport_code = data[7:]
        airport_name = MEYDAN_NAMES.get(airport_code, airport_code)
        title        = f"🛫 {airport_name}  ({airport_code})"

        uavs = filter_uavs(states)
        if uavs:
            await _edit_loading(context, f"⏳ {airport_name} için rota verileri kontrol ediliyor…", error_kb)
            sem = asyncio.Semaphore(5)

            async def _bounded(ic: str) -> dict:
                async with sem:
                    return await fetch_flight_info(ic)

            icao_list = [(s[0] or "").strip() for s in uavs]
            infos     = await asyncio.gather(*[_bounded(ic) for ic in icao_list])
            cached_infos = {ic: inf for ic, inf in zip(icao_list, infos)}

            for s, inf in zip(uavs, infos):
                dep = inf["dep_airport"].upper()
                arr = inf["arr_airport"].upper()
                if dep == airport_code or arr == airport_code:
                    flights.append(s)

    elif data == "cat_diger":
        title, flights = "🚁 Diğer Hava Araçları", filter_other(states)

    flights = sort_flights(flights)

    context.user_data["flights"]      = flights
    context.user_data["cached_infos"] = cached_infos
    context.user_data["active_data"]  = data
    context.user_data["active_title"] = title

    result  = quick_list(flights, title)
    ts      = now_tr()
    display = f"{result}\n\n_🕐 {e(ts)} TR_"

    full_text = _compose(context, display)
    _push_history(context, title, display)

    await _edit(context, full_text, result_kb(data))


async def handle_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    flights = context.user_data.get("flights")
    if not flights:
        msg = await update.effective_chat.send_message(
            _compose(context, "⚠️ Önce menüden bir kategori seçin\\."), parse_mode="MarkdownV2", reply_markup=main_menu_kb()
        )
        context.user_data["chat_id"] = update.effective_chat.id
        context.user_data["msg_id"]  = msg.message_id
        return

    idx = int(text) - 1
    if idx < 0 or idx >= len(flights):
        body = f"⚠️ Geçersiz numara\\. Lütfen *1* ile *{e(str(len(flights)))}* arasında bir değer girin\\."
        await _edit(context, _compose(context, body), result_kb(context.user_data.get("active_data", "menu_main")))
        return

    await _edit(context, _compose(context, f"_🔍 Detaylar alınıyor \\({e(text)}\\. uçuş\\)…_"), back_kb())

    state  = flights[idx]
    icao24 = (state[0] or "").strip()
    lat, lon = state[6], state[5]

    cached = context.user_data.get("cached_infos", {})
    if icao24 in cached:
        info     = cached[icao24]
        location = await fetch_location(lat, lon)
    else:
        info, location = await asyncio.gather(fetch_flight_info(icao24), fetch_location(lat, lon))

    callsign = (state[1] or "N/A").strip() or "N/A"
    d_title  = f"Detay: {callsign}"
    d_text   = detail_text(state, info, location)

    full_text = _compose(context, d_text)
    _push_history(context, d_title, d_text)

    await _edit(context, full_text, back_kb())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    keep_alive()
    print(f"Flask keep-alive server started on port {KEEP_ALIVE_PORT}.")

    bot = ApplicationBuilder().token(token).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CallbackQueryHandler(handle_callback))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number))

    print("Telegram bot is polling…")
    bot.run_polling()


if __name__ == "__main__":
    main()
