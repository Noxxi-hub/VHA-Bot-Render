import discord
from discord.ext import commands
import os
import re
import time
import asyncio
import threading
import logging
import json
import sqlite3
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
print("✅ .env Datei wurde geladen!")

# Lokale Spracherkennung - kein API Call mehr
try:
    from langdetect import detect_langs, DetectorFactory
    DetectorFactory.seed = 0
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False

# ────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────
# ────────────────────────────────────────────────
# LOGGING — mit Farben im Terminal + Datei
# ────────────────────────────────────────────────

class _ColorFormatter(logging.Formatter):
    """Farbige Log-Ausgabe im Terminal für bessere Übersicht."""
    GREY    = "[38;5;240m"
    CYAN    = "[36m"
    YELLOW  = "[33m"
    RED     = "[31m"
    BOLD_RED= "[1;31m"
    GREEN   = "[32m"
    RESET   = "[0m"
    BLUE    = "[34m"
    MAGENTA = "[35m"

    LEVEL_COLORS = {
        logging.DEBUG:    GREY,
        logging.INFO:     CYAN,
        logging.WARNING:  YELLOW,
        logging.ERROR:    RED,
        logging.CRITICAL: BOLD_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        # Spezielle Farben für bestimmte Log-Kategorien
        msg = record.getMessage()
        if "✅" in msg or "OK" in msg or "geladen" in msg.lower():
            color = self.GREEN
        elif "PERF" in msg:
            color = self.BLUE
        elif "SKIP" in msg:
            color = self.GREY
        elif "FALLBACK" in msg:
            color = self.MAGENTA
        elif "DB" in msg:
            color = self.YELLOW if record.levelno < logging.ERROR else self.RED

        formatter = logging.Formatter(
            f"{self.GREY}%(asctime)s{self.RESET} {color}[%(levelname)s]{self.RESET} %(message)s",
            datefmt="%H:%M:%S"
        )
        return formatter.format(record)


_file_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

_file_handler   = logging.FileHandler("translator_bot.log", encoding="utf-8")
_file_handler.setFormatter(_file_formatter)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_ColorFormatter())

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
log = logging.getLogger("VHATranslator")

# ────────────────────────────────────────────────
# KONFIGURATION
# ────────────────────────────────────────────────

LOGO_URL = (
    "https://cdn.discordapp.com/attachments/1498221186025259108/"
    "1516400553645834472/Picsart_26-06-16_13-04-08-364.png"
    "?ex=6a328191&is=6a313011&hm=72f5b3e3960a3ad8637eeb59e07cca15bc4ce08d9f506e8b72a61d5297cc9bb7&"
)

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
]
GEMINI_MODEL = GEMINI_MODELS[0]

BOT_LOG_CHANNEL_ID = 1498221186025259108

# Feste Zielsprachen dieses Bots (PT + EN immer aktiv)
FIXED_LANGS = {"PT", "EN"}

# ────────────────────────────────────────────────
# GLOBALS
# ────────────────────────────────────────────────

# ── Persistent Message Dedup (SQLite) ──────────────────
import sqlite3
msg_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_msgs.db")
msg_db = sqlite3.connect(msg_db_path, check_same_thread=False)
msg_db.execute("CREATE TABLE IF NOT EXISTS processed (msg_id INTEGER PRIMARY KEY)")
msg_db.execute("CREATE INDEX IF NOT EXISTS idx_processed_id ON processed(msg_id)")
msg_db.commit()

# Beim Start: IDs laden + alte Einträge (>24h) aufräumen
import time as _time_mod
_cutoff = _time_mod.time() - 86400
_rows = msg_db.execute(
    "SELECT msg_id FROM processed WHERE msg_id > ? ORDER BY msg_id DESC LIMIT 500",
    (_cutoff,)
).fetchall()
processed_messages_set: set[int] = {r[0] for r in _rows}
# Cleanup alter Einträge
msg_db.execute("DELETE FROM processed WHERE msg_id <= ?", (_cutoff,))
msg_db.commit()
log.info(f"💾 SQLite Dedup geladen: {len(processed_messages_set)} IDs cached")

translate_active = True

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY_TRANSLATOR"))

# Semaphore: max. 4 gleichzeitige Gemini-Calls
gemini_semaphore = asyncio.Semaphore(8)

import concurrent.futures as _futures
_gemini_executor = _futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="gemini_t")

user_last_translation: dict[int, float] = {}
TRANSLATION_COOLDOWN = 2.0  # reduziert von 8.0 für Gemini (höheres Rate-Limit)

token_counter = {"prompt": 0, "completion": 0, "total": 0}

# Caches
lang_cache: dict[str, str] = {}
translation_cache: dict[str, dict] = {}


# ────────────────────────────────────────────────
# WÜRFEL-STATISTIKEN — SQLite (lokal) oder MongoDB (Render)
# ────────────────────────────────────────────────

_DICE_USE_MONGO = bool(os.getenv("MONGODB_URI"))

if _DICE_USE_MONGO:
    from mongo_client import get_db as _get_mongo_db
    log.info("💾 Dice-Stats-Backend: MongoDB")
else:
    log.info("💾 Dice-Stats-Backend: SQLite (lokal)")

_DICE_DB_PATH = "/home/botdata/botdata.sqlite"

def _get_dice_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DICE_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _dice_col():
    return _get_mongo_db()["dice_stats"]


def db_update_stats(user_id: int, display_name: str, result: str):
    if _DICE_USE_MONGO:
        try:
            col = _dice_col()
            inc = {"games": 1}
            if result == "win":
                inc.update({"wins": 1, "points": 10})
            elif result == "loss":
                inc.update({"losses": 1, "points": -5})
            else:
                inc.update({"draws": 1, "points": 3})
            col.update_one(
                {"_id": str(user_id)},
                {
                    "$inc": inc,
                    "$set": {"name": display_name},
                    "$setOnInsert": {
                        k: 0 for k in ("wins", "losses", "draws", "games", "points") if k not in inc
                    },
                },
                upsert=True,
            )
            log.debug(f"DB stats ✅ {display_name} ({user_id}) → {result}")
        except Exception as e:
            log.error(f"DB stats ❌ FEHLER für {display_name} ({user_id}) | {type(e).__name__}: {e}")
        return

    try:
        conn = _get_dice_db()
        row = conn.execute("SELECT wins, losses, draws, games, points FROM dice_stats WHERE user_id = ?",
                           (str(user_id),)).fetchone()
        if row is None:
            conn.execute("INSERT INTO dice_stats (user_id, name) VALUES (?, ?)",
                         (str(user_id), display_name))

        if result == "win":
            conn.execute("UPDATE dice_stats SET wins = wins + 1, games = games + 1, points = points + 10, name = ? WHERE user_id = ?",
                         (display_name, str(user_id)))
        elif result == "loss":
            conn.execute("UPDATE dice_stats SET losses = losses + 1, games = games + 1, points = points - 5, name = ? WHERE user_id = ?",
                         (display_name, str(user_id)))
        else:
            conn.execute("UPDATE dice_stats SET draws = draws + 1, games = games + 1, points = points + 3, name = ? WHERE user_id = ?",
                         (display_name, str(user_id)))
        conn.commit()
        conn.close()
        log.debug(f"DB stats ✅ {display_name} ({user_id}) → {result}")
    except Exception as e:
        log.error(f"DB stats ❌ FEHLER für {display_name} ({user_id}) | {type(e).__name__}: {e}")


def db_add_points(user_id: int, display_name: str, delta: int):
    """Fuegt Punkte hinzu (kann negativ sein)."""
    if _DICE_USE_MONGO:
        try:
            col = _dice_col()
            col.update_one(
                {"_id": str(user_id)},
                {
                    "$inc": {"points": delta},
                    "$set": {"name": display_name},
                    "$setOnInsert": {"wins": 0, "losses": 0, "draws": 0, "games": 0},
                },
                upsert=True,
            )
            log.debug(f"DB points ✅ {display_name} ({user_id}) → {delta:+d}")
        except Exception as e:
            log.error(f"DB points ❌ FEHLER für {display_name} ({user_id}) | {type(e).__name__}: {e}")
        return

    try:
        conn = _get_dice_db()
        row = conn.execute("SELECT points FROM dice_stats WHERE user_id = ?", (str(user_id),)).fetchone()
        if row is None:
            conn.execute("INSERT INTO dice_stats (user_id, name) VALUES (?, ?)",
                         (str(user_id), display_name))
        conn.execute("UPDATE dice_stats SET points = points + ?, name = ? WHERE user_id = ?",
                     (delta, display_name, str(user_id)))
        conn.commit()
        conn.close()
        log.debug(f"DB points ✅ {display_name} ({user_id}) → {delta:+d}")
    except Exception as e:
        log.error(f"DB points ❌ FEHLER für {display_name} ({user_id}) | {type(e).__name__}: {e}")


def db_get_ranking(limit: int = 10) -> list:
    if _DICE_USE_MONGO:
        try:
            col = _dice_col()
            cursor = col.find(
                {"$or": [{"games": {"$gt": 0}}, {"points": {"$ne": 0}}]}
            ).sort([("points", -1), ("wins", -1), ("games", 1)]).limit(limit)
            return [{
                "user_id": doc["_id"], "name": doc.get("name", "?"),
                "wins": doc.get("wins", 0), "losses": doc.get("losses", 0),
                "draws": doc.get("draws", 0), "games": doc.get("games", 0),
                "points": doc.get("points", 0),
            } for doc in cursor]
        except Exception as e:
            log.error(f"DB ranking ❌ {type(e).__name__}: {e}")
            return []

    try:
        conn = _get_dice_db()
        rows = conn.execute(
            "SELECT user_id, name, wins, losses, draws, games, points FROM dice_stats "
            "WHERE games > 0 OR points != 0 ORDER BY points DESC, wins DESC, games ASC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [dict(zip(["user_id", "name", "wins", "losses", "draws", "games", "points"], r)) for r in rows]
    except Exception as e:
        log.error(f"DB ranking ❌ {type(e).__name__}: {e}")
        return []


def db_get_player(user_id: int) -> dict | None:
    if _DICE_USE_MONGO:
        try:
            doc = _dice_col().find_one({"_id": str(user_id)})
            if doc is None:
                return None
            return {
                "user_id": doc["_id"], "name": doc.get("name", "?"),
                "wins": doc.get("wins", 0), "losses": doc.get("losses", 0),
                "draws": doc.get("draws", 0), "games": doc.get("games", 0),
                "points": doc.get("points", 0),
            }
        except Exception as e:
            log.error(f"DB player ❌ {type(e).__name__}: {e}")
            return None

    try:
        conn = _get_dice_db()
        row = conn.execute(
            "SELECT user_id, name, wins, losses, draws, games, points FROM dice_stats WHERE user_id = ?",
            (str(user_id),)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return dict(zip(["user_id", "name", "wins", "losses", "draws", "games", "points"], row))
    except Exception as e:
        log.error(f"DB player ❌ {type(e).__name__}: {e}")
        return None


def get_active_languages() -> set:
    """Gibt aktive Sprachen zurück — liest aus tsprachen.py (SQLite)."""
    try:
        from tsprachen import get_active_langs
        return get_active_langs()
    except Exception:
        return {"PT", "EN"}  # Fallback





# ────────────────────────────────────────────────
# GEMINI ASYNC WRAPPER mit Retry - OPTIMIERT
# ────────────────────────────────────────────────

async def gemini_call(model: str, messages: list, temperature: float = 0.1,
                      max_tokens: int = 500, retries: int = 3) -> str:
    loop = asyncio.get_event_loop()

    system_text = None
    contents = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            system_text = content
        elif role == "user":
            if isinstance(content, str):
                contents.append(types.Content(role="user", parts=[types.Part(text=content)]))

    last_error = None
    for model_name in GEMINI_MODELS:
        use_thinking = "2.5" in model_name
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_text,
            thinking_config=types.ThinkingConfig(thinking_budget=0) if use_thinking else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        wait = 4
        for attempt in range(retries):
            async with gemini_semaphore:
                try:
                    resp = await loop.run_in_executor(
                        _gemini_executor,
                        lambda: gemini_client.models.generate_content(
                            model=model_name,
                            contents=contents,
                            config=config,
                        )
                    )
                    if resp.usage_metadata:
                        total = (resp.usage_metadata.prompt_token_count or 0) + (resp.usage_metadata.candidates_token_count or 0)
                        token_counter["prompt"]     += resp.usage_metadata.prompt_token_count or 0
                        token_counter["completion"] += resp.usage_metadata.candidates_token_count or 0
                        token_counter["total"]      += total
                        log.info(f"Tokens: +{total} (heute gesamt: {token_counter['total']})")

                    if model_name != GEMINI_MODELS[0]:
                        log.info(f"FALLBACK OK → {model_name}")
                    return resp.text.strip()

                except Exception as e:
                    err = str(e).lower()
                    last_error = str(e)
                    if "429" in err or "quota" in err or "resource_exhausted" in err or "rate" in err:
                        log.warning(f"⚠️  RATE-LIMIT {model_name} (Versuch {attempt+1}/{retries}) — warte {wait}s...")
                        await asyncio.sleep(wait)
                        wait = min(wait * 2, 60)
                    elif "503" in err or "500" in err or "502" in err or "unavailable" in err or "server" in err:
                        log.warning(f"⚠️  SERVER-FEHLER {model_name} — versuche nächstes Modell...")
                        break
                    else:
                        log.error(f"❌ GEMINI-FEHLER {model_name}: {type(e).__name__}: {e}")
                        break

        log.warning(f"⚠️  FALLBACK: {model_name} fehlgeschlagen, versuche nächstes Modell...")

    raise Exception(f"Alle Gemini-Modelle down. Letzter Fehler: {last_error}")


# ────────────────────────────────────────────────
# SPRACHE ERKENNEN — LOKAL, KEIN API-CALL
# ────────────────────────────────────────────────

_NEUTRAL = {
    "ok","okay","lol","gg","wp","xd","haha","hahaha","😂","👍","👋","gn","gm",
    "afk","brb","thx","ty","np","omg","wtf","irl","imo","btw","fyi","asap",
}

def _script_detect(text: str) -> str | None:
    """Erkennt Sprache anhand von Unicode-Blöcken."""
    cjk    = sum(1 for c in text if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
    hira   = sum(1 for c in text if "぀" <= c <= "ゟ")
    kata   = sum(1 for c in text if "゠" <= c <= "ヿ")
    hangul = sum(1 for c in text if "가" <= c <= "힣")
    arabic = sum(1 for c in text if "؀" <= c <= "ۿ")
    cyril  = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    total  = max(len(text), 1)

    if (hira + kata) / total > 0.15: return "JA"
    if hangul / total > 0.15:        return "KO"
    if cjk / total > 0.15:           return "ZH"
    if arabic / total > 0.15:        return "AR"
    if cyril / total > 0.15:         return "RU"
    return None


async def detect_language_llm(text: str) -> str:
    """Erkennt Sprache LOKAL — 0 API-Calls, optimiert für kurze DE/FR."""
    stripped = text.strip()

    if not stripped or len(stripped) < 2:
        return "OTHER"
    words_lower = {w.strip(".,!?") for w in stripped.lower().split()}
    if words_lower <= _NEUTRAL:
        return "OTHER"
    if re.match(r"^[\d\s\W]+$", stripped):
        return "OTHER"

    # Script-Erkennung zuerst
    script_lang = _script_detect(stripped)
    if script_lang:
        return script_lang

    key = stripped.lower()[:80]
    if key in lang_cache:
        return lang_cache[key]

    t = f" {stripped.lower()} "

    # NEU: kurze Texte (<20 Zeichen) – harte Heuristik
    if len(stripped) < 20:
        de_markers = [' ich ', ' bin ', ' da ', ' ne ', ' ja ', ' nein ', ' was ', ' du ', ' nicht ', ' mal ', ' hab ', ' habe ', ' ist ', ' ein ', ' der ', ' die ', ' das ', ' und ', ' ne bin ', ' was sagst ']
        fr_markers = [' je ', ' suis ', ' pas ', ' oui ', ' non ', ' tu ', ' vous ', ' est ', ' le ', ' la ', ' et ', ' pour ', ' quoi ']
        tr_markers = [' ne ', ' ya ', ' nasıl', ' nasılsın', ' napıyor', ' yapıyor', ' var ', ' yok ', ' evet', ' hayır', ' ben ', ' sen ', ' senin', ' benim', ' değil', ' çok ', ' ama ', ' çünkü', ' mı ', ' mi ', ' mu ', ' mü ']
        de_hits = sum(1 for w in de_markers if w in t)
        fr_hits = sum(1 for w in fr_markers if w in t)
        tr_hits = sum(1 for w in tr_markers if w in t)
        if de_hits > 0 and de_hits >= fr_hits and de_hits >= tr_hits:
            lang_cache[key] = "DE"
            return "DE"
        if fr_hits > 0 and fr_hits > de_hits and fr_hits > tr_hits:
            lang_cache[key] = "FR"
            return "FR"
        if tr_hits > 0 and tr_hits > de_hits and tr_hits > fr_hits:
            lang_cache[key] = "TR"
            return "TR"
        if any(c in stripped for c in 'äöüßÄÖÜ'):
            lang_cache[key] = "DE"
            return "DE"

    lang = "OTHER"
    if LANGDETECT_AVAILABLE:
        try:
            langs = detect_langs(stripped)
            code = langs[0].lang.upper()
            prob = langs[0].prob
            mapping = {"PT": "PT", "EN": "EN", "DE": "DE", "FR": "FR", "ES": "ES", "RU": "RU", "JA": "JA", "ZH-CN": "ZH", "ZH": "ZH", "KO": "KO", "TR": "TR"}
            if prob > 0.7:
                lang = mapping.get(code, "OTHER")
        except:
            lang = "OTHER"
    else:
        if re.search(r'\b(der|die|das|und|ich|nicht)\b', stripped.lower()): lang = "DE"
        elif re.search(r'\b(the|and|you|for)\b', stripped.lower()): lang = "EN"
        elif re.search(r'\b(le|la|et|vous|pour)\b', stripped.lower()): lang = "FR"
        elif re.search(r'\b(o|a|e|que|para)\b', stripped.lower()): lang = "PT"
        elif re.search(r'\b(ne|ya|nasıl|nasılsın|napıyor|yapıyor|var|yok|evet|hayır|ben|sen|senin|benim|değil|çok|ama|çünkü)\b', stripped.lower()): lang = "TR"

    if lang == "OTHER":
        if any(w in t for w in [' der ', ' die ', ' das ', ' und ', ' ich ', ' nicht ']):
            lang = "DE"
        elif any(w in t for w in [' le ', ' la ', ' et ', ' vous ', ' je ']):
            lang = "FR"
        elif any(w in t for w in [' the ', ' and ', ' you ']):
            lang = "EN"
        elif any(w in t for w in [' ne ', ' ya ', ' nasıl', ' nasılsın', ' var ', ' yok ', ' evet', ' hayır', ' ben ', ' sen ', ' değil', ' çok ', ' ama ']):
            lang = "TR"
        else:
            lang = "EN"

    known = {"DE","FR","PT","EN","ES","RU","JA","ZH","KO","TR","OTHER"}
    if lang not in known:
        lang = "OTHER"

    lang_cache[key] = lang
    if len(lang_cache) > 800:
        for k in list(lang_cache.keys())[:200]:
            del lang_cache[k]
    return lang


# ────────────────────────────────────────────────
# ÜBERSETZEN - MIT CACHE
# ────────────────────────────────────────────────

async def translate_all(text: str, target_langs: list, context: str = "") -> dict:
    if not target_langs:
        log.info(f"⏭️  SKIP kein Ziel [{guild_name}] #{channel_name} | user:{message.author.display_name} | lang:{lang} bereits in aktiven Sprachen oder keine Zielsprache übrig")
        return {}

    codes = [code for code, _, _ in target_langs]
    cache_key = f"{text[:200]}_{'_'.join(codes)}"

    if cache_key in translation_cache:
        log.debug(f"💾 CACHE HIT | '{text[:40]}'")
        return translation_cache[cache_key]

    codes_str = ", ".join(f"{code}={lang_name}" for code, lang_name, _ in target_langs)
    json_keys = ", ".join(f'"{code}": "..."' for code in codes)
    estimated = max(800, min(4000, int(len(text) * 2.5 * len(target_langs))))

    try:
        result = await gemini_call(
            model=GEMINI_MODEL,
            temperature=0.1,
            max_tokens=estimated,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Du bist ein intelligenter Übersetzer für eine internationale Gaming-Community (Discord).\n"
                        f"Übersetze den Text in diese {len(codes)} Sprachen: {codes_str}.\n\n"
                        + (f"GESPRÄCHSKONTEXT (letzte Nachrichten im Kanal — NUR zum Verstehen, NICHT übersetzen):\n{context}\n\n" if context else "")
                        + f"DEINE MISSION:\n"
                        f"1. ANALYSE: Erkenne den Tonfall — ist es ein privates/liebevolles Gespräch oder geht es um Spiel/Allianz-Organisation? Übersetze entsprechend.\n"
                        f"2. NATÜRLICHKEIT: Übersetze den SINN. Klinge wie ein Muttersprachler im Chat, nicht wie ein Lexikon.\n"
                        f"3. TON: Wenn ein Satz witzig, frech, emotional oder liebevoll ist, übersetze ihn genauso — nicht steif.\n"
                        f"4. DU-FORM: Verwende IMMER 'Du' (Deutsch), 'Tu/Toi' (Französisch) — niemals 'Sie' oder 'Vous'.\n"
                        f"5. KOSENAMEN: 'schatz'→chéri/chérie (FR), honey/darling (EN); 'süße/süßer'→ma chérie/mon chéri (FR), sweetie (EN).\n"
                        f"5b. Diese Kosenamen NIE übersetzen: baby, babe, bby — bleiben in allen Sprachen gleich.\n"
                        f"6. NO-GO: Spielernamen, @mentions, R1/R2/R3/R4/R5, Koordinaten, Allianz-Namen NIEMALS übersetzen.\n"
                        f"7. Emojis bleiben exakt unverändert.\n"
                        f"8. Jedes Sprachfeld MUSS in der richtigen Zielsprache sein — DE=Deutsch, FR=Französisch, EN=Englisch, PT=Portugiesisch, TR=Türkisch.\n"
                        f"9. WICHTIG: Alle {len(codes)} Sprachfelder MÜSSEN befüllt sein — auch bei sehr kurzen Sätzen.\n"
                        f"10. Antworte NUR mit diesem JSON, kein Markdown, kein Extra-Text:\n"
                        f"{{{json_keys}}}"
                    )
                },
                {"role": "user", "content": text}
            ]
        )

        clean = result.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()

        parsed = json.loads(clean)
        translations = {}
        max_len = max(len(text) * 6, 500)

        original_words = set(re.sub(r'[^\w\s]', '', text.lower()).split())

        for code in codes:
            val = parsed.get(code, "").strip()
            if not val:
                continue

            # Identisch-Check: bei kurzen Texten (<3 Wörter) oder TR nicht so streng
            # Kurze Texte wie "Merhaba", "Teşekkürler" etc. können im Original verbleiben
            _short_text = len(original_words) <= 3 and code not in ("TR",)
            if val.lower() == text.lower():
                if _short_text:
                    log.warning(f"⚠️  ÜBERSETZUNG IDENTISCH mit Original ({code}) — bei kurzem Text erlaubt | '{val[:50]}'")
                else:
                    log.warning(f"⚠️  ÜBERSETZUNG IDENTISCH mit Original ({code}) — verworfen | '{val[:50]}'")
                    continue
            # Zu ähnlich zum Original — nur bei 5+ Wörtern und nicht für EN
            if code != "EN" and len(original_words) >= 5:
                val_words = set(re.sub(r'[^\w\s]', '', val.lower()).split())
                overlap = len(original_words & val_words) / len(original_words)
                if overlap > 0.80:
                    log.warning(f"⚠️  ÜBERSETZUNG ZU ÄHNLICH ({code}): {overlap:.0%} Überlappung — verworfen | '{val[:50]}'")
                    continue

            words = val.split()
            if words:
                most_common = max(set(words), key=words.count)
                if words.count(most_common) > 15:
                    log.warning(f"⚠️  LOOP ERKANNT ({code}) — verworfen | Wiederholung: '{most_common}'")
                    continue

            if len(val) > max_len:
                val = val[:max_len]

            translations[code] = val

        # Cache speichern
        if translations:
            translation_cache[cache_key] = translations
            if len(translation_cache) > 500:
                # Alte Einträge löschen
                for k in list(translation_cache.keys())[:100]:
                    del translation_cache[k]

        return translations

    except Exception as e:
        log.error(f"❌ GEMINI ÜBERSETZUNG FEHLGESCHLAGEN | {type(e).__name__}: {e}")
        log.error(f"   → Text war: '{text[:80]}'")
        log.error(f"   → Zielsprachen: {[c for c,_,_ in target_langs]}")
        return {}


# ────────────────────────────────────────────────
# FLAGGEN & SPRACHNAMEN
# ────────────────────────────────────────────────

LANG_FLAGS = {
    "DE": "🇩🇪", "FR": "🇫🇷", "PT": "🇧🇷", "EN": "🇬🇧",
    "JA": "🇯🇵", "ES": "🇪🇸", "RU": "🇷🇺",
    "ZH": "🇨🇳", "KO": "🇰🇷", "TR": "🇹🇷",
}

LANG_NAMES = {
    "DE": "German",               "FR": "French",
    "PT": "Brazilian Portuguese", "EN": "English",
    "JA": "Japanese",             "ES": "Spanish",
    "RU": "Russian",              "ZH": "Chinese",
    "KO": "Korean",               "TR": "Turkish",
}

ALL_LANGS = [
    ("PT", "Brazilian Portuguese", "🇧🇷 Português"),
    ("EN", "English",              "🇬🇧 English"),
    ("JA", "Japanese",             "🇯🇵 日本語"),
    ("ZH", "Chinese",              "🇨🇳 中文"),
    ("KO", "Korean",               "🇰🇷 한국어"),
    ("ES", "Spanish",              "🇪🇸 Español"),
    ("RU", "Russian",              "🇷🇺 Русский"),
    ("TR", "Turkish",              "🇹🇷 Türkçe"),
]

# ────────────────────────────────────────────────
# BOT SETUP
# ────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=["!t", "!"],
    intents=intents,
    help_command=None,
    case_insensitive=True
)

bot_ready = False

@bot.event
async def on_ready():
    global bot_ready
    if bot_ready:
        return
    bot_ready = True
    errors = []

    try:
        await bot.load_extension("tsprachen")
        log.info("✅ tsprachen.py geladen")
    except Exception as e:
        errors.append(f"❌ tsprachen: {e}")
        log.error(f"❌ TSPRACHEN LADEN FEHLGESCHLAGEN | {type(e).__name__}: {e}")
        log.error("   → Spracheinstellungen nicht verfügbar! Fallback: PT + EN")

    try:
        await bot.load_extension("traumsprachen")
        log.info("✅ traumsprachen.py geladen")
    except Exception as e:
        errors.append(f"❌ traumsprachen: {e}")
        log.error(f"❌ TRAUMSPRACHEN LADEN FEHLGESCHLAGEN | {type(e).__name__}: {e}")

    log.info("=" * 60)
    log.info(f"🤖 BOT ONLINE: {bot.user} ({bot.user.id})")
    log.info(f"📅 Zeit: {discord.utils.utcnow():%Y-%m-%d %H:%M:%S UTC}")
    log.info(f"🌍 Server: {len(bot.guilds)} → {', '.join(g.name for g in bot.guilds)}")
    log.info(f"🗣️  Aktive Sprachen: {sorted(get_active_languages())}")
    log.info(f"🔍 Spracherkennung: {'langdetect ✅' if LANGDETECT_AVAILABLE else '⚠️  Fallback-Heuristik (pip install langdetect)'}")
    log.info(f"📋 Commands: {[c.name for c in bot.commands]}")
    log.info("=" * 60)
    if not LANGDETECT_AVAILABLE:
        log.warning("⚠️  langdetect nicht installiert — Spracherkennung ungenauer! Fix: pip install langdetect")

    if BOT_LOG_CHANNEL_ID:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if channel:
            if errors:
                msg = "⚠️ **Übersetzer-Bot gestartet mit Fehlern:**\n" + "\n".join(errors)
            else:
                msg = (
                    "✅ **Übersetzer-Bot erfolgreich gestartet!**\n"
                    "🔧 tsprachen.py + traumsprachen.py • geladen\n"
                    "💾 Datenbank: SQLite\n"
                    "⚡ Optimiert: Lokale Spracherkennung, AFC deaktiviert, Cache aktiv"
                )
            await channel.send(msg)


# ────────────────────────────────────────────────
# COMMAND ERROR HANDLER — loggt alle Command-Fehler
# ────────────────────────────────────────────────

@bot.event
async def on_command(ctx):
    log.info(f"✅ COMMAND CALLED: {ctx.command} | user: {ctx.author} | channel: {ctx.channel.id} | content: {ctx.message.content[:100]}")

@bot.event
async def on_command_error(ctx, error):
    log.error(f"❌ COMMAND ERROR: {ctx.command} | {type(error).__name__}: {error}")
    if isinstance(error, commands.CommandNotFound):
        return  # ignorieren — kein nicht existierender Befehl ist normal
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Keine Berechtigung.", delete_after=5)
        return
    # Alle anderen Errors nach außengeben
    try:
        await ctx.send(f"❌ Fehler beim Ausführen: {error}", delete_after=8)
    except Exception:
        pass

# ────────────────────────────────────────────────
# BEFEHLE
# ────────────────────────────────────────────────

@bot.command(name="ping")
async def cmd_ping(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Übersetzer-Bot", color=0x57F287 if latency < 200 else 0xF39C12)
    embed.add_field(name="📡 Latenz", value=f"`{latency}ms`", inline=True)
    embed.add_field(name="📊 Tokens heute", value=f"`{token_counter['total']}`", inline=True)
    embed.add_field(name="🌐 Aktive Sprachen", value=", ".join(sorted(get_active_languages())), inline=False)
    embed.add_field(name="💾 Cache", value=f"Lang: {len(lang_cache)} | Trans: {len(translation_cache)}", inline=True)
    embed.set_footer(text="VHA Übersetzer-Bot • Optimiert", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


# ────────────────────────────────────────────────
# SPIELE-ÜBERSICHT 🎮
# ────────────────────────────────────────────────

@bot.command(name="games", aliases=["spiele", "spieleliste", "hilfe", "help", "jeux", "jogos"])
async def cmd_games(ctx):
    """Zeigt alle verfügbaren Spiele mit Befehlen und Regeln."""
    embed = discord.Embed(
        title="🎮 VHA Spiele-Übersicht / Liste des jeux / Lista de jogos / Game List",
        color=0x9B59B6
    )

    embed.add_field(
        name="🎲 Würfeln  —  `!würfel`",
        value=(
            "🇩🇪 Wirf einen W6. Auch `!würfel 20` für andere Würfel.\n"
            "🇫🇷 Lance un dé. `!würfel 20` pour d'autres dés.\n"
            "🇧🇷 Role um dado. `!würfel 20` para outros dados.\n"
            "🇬🇧 Roll a die. `!würfel 20` for other dice."
        ),
        inline=False
    )

    embed.add_field(
        name="⚔️ Würfelduell  —  `!duell`",
        value=(
            "🇩🇪 Startet ein Gruppenduell (bis 10 Spieler). Beitreten per Button. Höchster Würfelwurf gewinnt. **+10 Pkt** Sieg / **-5 Pkt** Niederlage.\n"
            "🇫🇷 Lance un duel de groupe. Rejoindre via bouton. Le plus haut dé gagne. **+10 pts** victoire / **-5 pts** défaite.\n"
            "🇧🇷 Inicia um duelo em grupo. Entrar via botão. Maior dado ganha. **+10 pts** vitória / **-5 pts** derrota.\n"
            "🇬🇧 Starts a group duel. Join via button. Highest roll wins. **+10 pts** win / **-5 pts** loss."
        ),
        inline=False
    )

    embed.add_field(
        name="💣 Bomben-Entschärfer  —  `!bombe`",
        value=(
            "🇩🇪 Drei Drähte (🔴🔵🟡) — nur einer entschärft die Bombe! Wer zuerst drückt entscheidet. **+15 Pkt** richtig / **-10 Pkt** falsch.\n"
            "🇫🇷 Trois fils — un seul désamorce la bombe ! **+15 pts** correct / **-10 pts** mauvais.\n"
            "🇧🇷 Três fios — apenas um desarma a bomba! **+15 pts** certo / **-10 pts** errado.\n"
            "🇬🇧 Three wires — only one defuses the bomb! First to press decides. **+15 pts** correct / **-10 pts** wrong."
        ),
        inline=False
    )

    embed.add_field(
        name="📈 Höher oder Tiefer  —  `!hot`",
        value=(
            "🇩🇪 Rate ob die nächste Zahl höher oder tiefer ist. Streak-Bonus: +3/+6/+9... Pkt pro richtigem Tipp. Falsch = alle Punkte weg! Stop = Punkte sichern.\n"
            "🇫🇷 Devinez si le prochain chiffre est plus haut ou bas. Bonus de série: +3/+6/+9... Mauvais = tout perdu ! Stop = sécuriser.\n"
            "🇧🇷 Adivinhe se o próximo número é maior ou menor. Bônus: +3/+6/+9... Errado = tudo perdido! Stop = garantir.\n"
            "🇬🇧 Guess if the next number is higher or lower. Streak bonus: +3/+6/+9... Wrong = all lost! Stop = cash out."
        ),
        inline=False
    )

    embed.add_field(
        name="🎰 Russisches Roulette  —  `!roulette`",
        value=(
            "🇩🇪 Mindestens 2 Spieler per Button beitreten. Nach 30s wird zufällig ein Verlierer gezogen. **-20 Pkt** Verlierer / **+8 Pkt** Überlebende.\n"
            "🇫🇷 Min. 2 joueurs via bouton. Après 30s un perdant est tiré au sort. **-20 pts** perdant / **+8 pts** survivants.\n"
            "🇧🇷 Mín. 2 jogadores via botão. Após 30s um perdedor é sorteado. **-20 pts** perdedor / **+8 pts** sobreviventes.\n"
            "🇬🇧 Min. 2 players via button. After 30s a random loser is picked. **-20 pts** loser / **+8 pts** survivors."
        ),
        inline=False
    )

    embed.add_field(
        name="🦹 Raubzug  —  `!raub @Spieler`",
        value=(
            "🇩🇪 30% Chance Punkte zu stehlen. Bei Misserfolg: Punkte verloren ODER 5 Min Gefängnis (kein Raubzug möglich).\n"
            "🇫🇷 30% de chance de voler des points. En cas d'échec: points perdus OU 5 min de prison.\n"
            "🇧🇷 30% de chance de roubar pontos. Em caso de falha: pontos perdidos OU 5 min de prisão.\n"
            "🇬🇧 30% chance to steal points. On failure: points lost OR 5 min jail (no robbery possible)."
        ),
        inline=False
    )

    embed.add_field(
        name="🏆 Ranking  —  `!ranking` / `!ranking @Spieler`",
        value=(
            "🇩🇪 Zeigt die Top 10 mit Punkten & Statistiken. `!ranking @Name` für Details eines Spielers.\n"
            "🇫🇷 Affiche le Top 10 avec points & stats. `!ranking @Nom` pour les détails d'un joueur.\n"
            "🇧🇷 Mostra o Top 10 com pontos & estatísticas. `!ranking @Nome` para detalhes.\n"
            "🇬🇧 Shows Top 10 with points & stats. `!ranking @Name` for a player's details."
        ),
        inline=False
    )

    embed.set_footer(text="VHA Spiele-Bot  •  !games für diese Übersicht", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


# ────────────────────────────────────────────────
# WÜRFELSPIEL 🎲
# ────────────────────────────────────────────────

import random as _random

DICE_FACES = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}

# Aktive Würfelduell-Herausforderungen: {channel_id: {challenger_id, challenger_name, roll}}
_dice_challenges: dict = {}


@bot.command(name="würfel", aliases=["dice", "roll", "dé", "dado", "кубик", "w6"])
async def cmd_wuerfel(ctx, seiten: int = 6):
    """
    Würfelspiel — mehrere Modi:
    !würfel          → wirf einen W6
    !würfel 20       → wirf einen W20 (oder beliebige Seitenzahl)
    !würfel duell    → fordere den nächsten Spieler zum Duell heraus
    """
    # Sonderfall: "duell" als Argument
    if ctx.invoked_with in ("würfel", "dice", "roll", "dé", "dado", "кубик", "w6"):
        # Prüfe ob erstes Argument "duell" ist — aber seiten wäre dann kein int
        pass

    if not (2 <= seiten <= 1000):
        await ctx.send("❌ Seitenzahl muss zwischen 2 und 1000 liegen.", delete_after=6)
        return

    result = _random.randint(1, seiten)
    # Bewertung
    pct = result / seiten

    if pct == 1.0:
        de, fr, en = "🏆 **MAXIMUM!** Unglaublich!", "🏆 **MAXIMUM!** Incroyable!", "🏆 **MAXIMUM!** Incredible!"
        color = 0xF1C40F
    elif pct >= 0.8:
        de, fr, en = "🔥 Starker Wurf!", "🔥 Beau lancer!", "🔥 Strong roll!"
        color = 0x2ECC71
    elif pct >= 0.5:
        de, fr, en = "👍 Solider Wurf.", "👍 Lancer correct.", "👍 Solid roll."
        color = 0x3498DB
    elif pct >= 0.2:
        de, fr, en = "😬 Könnte besser sein...", "😬 Peut mieux faire...", "😬 Could be better..."
        color = 0xF39C12
    else:
        de, fr, en = "💀 Kritischer Misserfolg!", "💀 Échec critique!", "💀 Critical failure!"
        color = 0xE74C3C

    embed = discord.Embed(title=f"W{seiten}-Wurf", description=f"# {result}", color=color)
    embed.add_field(name=ctx.author.display_name, value=f"🇩🇪 {de}  🇫🇷 {fr}  🇬🇧 {en}", inline=False)
    embed.set_footer(text=f"1–{seiten} möglich / possible", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


# _dice_challenges: { channel_id: { "host_id": int, "host_name": str, "players": [...] } }

MAX_DUEL_PLAYERS = 10
DUEL_TIMER = 60  # Sekunden bis automatische Auswertung


async def _resolve_duel(channel, channel_id: int, view_ref=None):
    """Wertet das Duell aus und schickt das Ergebnis-Embed."""
    if channel_id not in _dice_challenges:
        return
    ch = _dice_challenges.pop(channel_id)
    players = ch["players"]

    # Button deaktivieren
    if view_ref is not None:
        for item in view_ref.children:
            item.disabled = True
        try:
            await view_ref.msg.edit(view=view_ref)
        except Exception:
            pass

    if len(players) < 2:
        await channel.send(
            "⏰ 🇩🇪 **Zeit abgelaufen!** Zu wenige Spieler — Duell abgebrochen.\n"
            "⏰ 🇫🇷 **Temps écoulé !** Pas assez de joueurs — duel annulé.\n"
            "⏰ 🇧🇷 **Tempo esgotado!** Jogadores insuficientes — duelo cancelado.\n"
            "⏰ 🇬🇧 **Time's up!** Not enough players — duel cancelled."
        )
        return

    players_sorted = sorted(players, key=lambda p: p["roll"], reverse=True)
    max_roll = players_sorted[0]["roll"]
    winners  = [p for p in players_sorted if p["roll"] == max_roll]
    is_draw  = len(winners) > 1

    place_emojis = ["🥇", "🥈", "🥉"] + [f"`{i+1}.`" for i in range(3, MAX_DUEL_PLAYERS)]
    lines = []
    last_roll = None
    place = 0
    for p in players_sorted:
        if p["roll"] != last_roll:
            place += 1
            last_roll = p["roll"]
        medal = place_emojis[place - 1] if place - 1 < len(place_emojis) else f"`{place}.`"
        lines.append(f"{medal} **{p['name']}** — `{p['roll']}`")

    if is_draw:
        result_de = f"🤝 **Unentschieden!** {' & '.join(w['name'] for w in winners)}"
        result_fr = f"🤝 **Égalité !** {' & '.join(w['name'] for w in winners)}"
        result_pt = f"🤝 **Empate!** {' & '.join(w['name'] for w in winners)}"
        result_en = f"🤝 **Draw!** {' & '.join(w['name'] for w in winners)}"
        color = 0x9B59B6
    else:
        w = winners[0]
        result_de = f"🏆 **{w['name']}** gewinnt!"
        result_fr = f"🏆 **{w['name']}** gagne !"
        result_pt = f"🏆 **{w['name']}** venceu!"
        result_en = f"🏆 **{w['name']}** wins!"
        color = 0xF1C40F

    try:
        for p in players:
            if is_draw and p["roll"] == max_roll:
                res = "draw"
            elif p["roll"] == max_roll:
                res = "win"
            else:
                res = "loss"
            db_update_stats(p["id"], p["name"], res)
    except Exception as e:
        log.error(f"DB stats error: {e}")

    embed = discord.Embed(
        title="🎲 Würfelduell — Ergebnis! / Résultat ! / Resultado! / Result!",
        description="\n".join(lines),
        color=color
    )
    embed.add_field(
        name="Ergebnis / Résultat / Resultado / Result",
        value=f"🇩🇪 {result_de}\n🇫🇷 {result_fr}\n🇧🇷 {result_pt}\n🇬🇧 {result_en}",
        inline=False
    )
    embed.set_footer(text="VHA Würfelduell / Duel de dés / Duelo de dados / Dice Duel", icon_url=LOGO_URL)
    await channel.send(embed=embed)


class DuellJoinView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=DUEL_TIMER)
        self.channel_id = channel_id
        self.msg = None  # wird nach dem Senden gesetzt

    @discord.ui.button(
        label="🎲 Beitreten / Rejoindre / Entrar / Join",
        style=discord.ButtonStyle.primary,
        custom_id="duell_join"
    )
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = _dice_challenges.get(self.channel_id)
        if not ch:
            await interaction.response.send_message(
                "🇩🇪 Duell nicht mehr aktiv.\n🇫🇷 Duel plus actif.\n🇬🇧 Duel no longer active.",
                ephemeral=True
            )
            return
        uid  = interaction.user.id
        name = interaction.user.display_name
        if any(p["id"] == uid for p in ch["players"]):
            await interaction.response.send_message(
                f"🇩🇪 Du bist bereits dabei, **{name}**!\n"
                f"🇫🇷 Tu es déjà inscrit !\n"
                f"🇧🇷 Você já entrou!\n"
                f"🇬🇧 You already joined!",
                ephemeral=True
            )
            return
        if len(ch["players"]) >= MAX_DUEL_PLAYERS:
            await interaction.response.send_message(
                f"🇩🇪 Duell ist voll! (max. {MAX_DUEL_PLAYERS})\n"
                f"🇫🇷 Duel complet !\n🇬🇧 Duel is full!",
                ephemeral=True
            )
            return
        roll = _random.randint(1, 6)
        ch["players"].append({"id": uid, "name": name, "roll": roll})
        count = len(ch["players"])
        # Ephemeral nur als kurze Bestätigung für den Drückenden
        await interaction.response.send_message(
            f"✅ Du bist dabei, **{name}**!",
            ephemeral=True
        )
        # Öffentliche Nachricht für alle sichtbar
        await interaction.channel.send(
            f"🎲 **{name}** 🇩🇪 ist beigetreten / 🇫🇷 a rejoint / 🇧🇷 entrou / 🇬🇧 joined! **({count}/{MAX_DUEL_PLAYERS})**"
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.msg:
            try:
                await self.msg.edit(view=self)
            except Exception:
                pass


@bot.command(name="duell", aliases=["duel", "duel🎲"])
async def cmd_duell(ctx):
    """Gruppenduell bis 10 Spieler — Beitreten per Button, Auswertung nach 60s."""
    channel_id = ctx.channel.id
    user_id    = ctx.author.id
    name       = ctx.author.display_name

    if channel_id in _dice_challenges:
        await ctx.send(
            "🇩🇪 Ein Duell läuft bereits! Klicke den **Beitreten**-Button.\n"
            "🇫🇷 Un duel est déjà en cours ! Clique sur le bouton.\n"
            "🇧🇷 Um duelo já está em andamento! Clique no botão.\n"
            "🇬🇧 A duel is already running! Click the Join button.",
            delete_after=8
        )
        return

    roll = _random.randint(1, 6)
    _dice_challenges[channel_id] = {
        "host_id":   user_id,
        "host_name": name,
        "players":   [{"id": user_id, "name": name, "roll": roll}],
    }

    view = DuellJoinView(channel_id)
    embed = discord.Embed(title="🎲 Würfelduell / Duel de dés / Duelo / Dice Duel", color=0x9B59B6)
    embed.add_field(
        name=f"👑 {name}",
        value=(
            f"🇩🇪 **{name}** startet ein Gruppenduell! (bis {MAX_DUEL_PLAYERS} Spieler)\n"
            f"🇫🇷 **{name}** lance un duel de groupe ! (jusqu'à {MAX_DUEL_PLAYERS} joueurs)\n"
            f"🇧🇷 **{name}** inicia um duelo em grupo! (até {MAX_DUEL_PLAYERS} jogadores)\n"
            f"🇬🇧 **{name}** starts a group duel! (up to {MAX_DUEL_PLAYERS} players)\n\n"
            f"🇩🇪 Klicke den Button zum Beitreten — Auswertung in **{DUEL_TIMER}s**!\n"
            f"🇫🇷 Clique sur le bouton pour rejoindre — résultat dans **{DUEL_TIMER}s** !\n"
            f"🇧🇷 Clique no botão para entrar — resultado em **{DUEL_TIMER}s**!\n"
            f"🇬🇧 Click the button to join — result in **{DUEL_TIMER}s**!\n\n"
            "🇩🇪 *(Würfel werden erst am Ende aufgedeckt)*\n"
            "🇫🇷 *(Dés révélés à la fin)*\n"
            "🇧🇷 *(Dados revelados no final)*\n"
            "🇬🇧 *(Dice revealed at the end)*"
        ),
        inline=False
    )
    embed.set_footer(
        text=f"⏱️ {DUEL_TIMER}s • 1/{MAX_DUEL_PLAYERS} • läuft... / en cours... / em andamento... / running...",
        icon_url=LOGO_URL
    )
    msg = await ctx.send(embed=embed, view=view)
    view.msg = msg

    await asyncio.sleep(DUEL_TIMER)
    await _resolve_duel(ctx.channel, channel_id, view_ref=view)


# ────────────────────────────────────────────────
# WÜRFEL-RANKING
# ────────────────────────────────────────────────

MEDALS = ["🥇", "🥈", "🥉"]

@bot.command(name="ranking", aliases=["rank", "stats", "leaderboard", "top", "classement", "rang"])
async def cmd_ranking(ctx, member: discord.Member = None):
    if member is not None:
        try:
            data = db_get_player(member.id)
        except Exception as e:
            await ctx.send(f"❌ DB-Fehler: {e}", delete_after=8)
            return
        if not data:
            await ctx.send(
                f"🇩🇪 **{member.display_name}** hat noch keine Spiele gespielt.  "
                f"🇫🇷 Aucune partie.  🇬🇧 No games yet.",
                delete_after=8
            )
            return
        wins   = data.get("wins",   0)
        losses = data.get("losses", 0)
        draws  = data.get("draws",  0)
        games  = data.get("games",  0)
        pts    = data.get("points", 0)
        wr     = round(wins / games * 100) if games else 0
        embed = discord.Embed(
            title=f"🎮 {data.get('name', member.display_name)}",
            color=0xF1C40F if pts >= 0 else 0xE74C3C
        )
        embed.add_field(
            name="🇩🇪 Statistik  /  🇫🇷 Statistiques  /  🇬🇧 Stats",
            value=(
                f"💰 **{pts}** Punkte / Points\n"
                f"🏆 **{wins}** Siege / Victoires / Wins\n"
                f"💀 **{losses}** Niederlagen / Défaites / Losses\n"
                f"🤝 **{draws}** Unentschieden / Égalités / Draws\n"
                f"🎲 **{games}** Spiele / Parties / Games\n"
                f"📊 **{wr}%** Winrate"
            ),
            inline=False
        )
        embed.add_field(
            name="🎮 Spiele / Jeux / Games",
            value="`!würfel` `!duell` `!bombe` `!hot` `!roulette` `!raub @User`",
            inline=False
        )
        embed.set_footer(text="VHA Spiele-Ranking", icon_url=LOGO_URL)
        await ctx.send(embed=embed)
        return

    try:
        top = db_get_ranking(10)
    except Exception as e:
        await ctx.send(f"❌ DB-Fehler: {e}", delete_after=8)
        return
    if not top:
        await ctx.send(
            "🇩🇪 Noch keine Duelle gespielt.  🇫🇷 Aucune donnée.  🇬🇧 No data yet.",
            delete_after=8
        )
        return
    lines = []
    for i, p in enumerate(top):
        medal  = MEDALS[i] if i < 3 else f"`{i+1}.`"
        wins   = p.get("wins",   0)
        losses = p.get("losses", 0)
        draws  = p.get("draws",  0)
        games  = p.get("games",  0)
        pts    = p.get("points", 0)
        wr     = round(wins / games * 100) if games else 0
        lines.append(f"{medal} **{p['name']}** — 💰 **{pts}** Pkt  *(🏆{wins}W / 💀{losses}L / 📊{wr}%)*")
    embed = discord.Embed(
        title="🏆 VHA Spiele-Ranking / Classement / Leaderboard",
        description="\n".join(lines),
        color=0xF1C40F
    )
    embed.add_field(
        name="🎮 Spiele / Jeux / Games",
        value="`!würfel` `!duell` `!bombe` `!hot` `!roulette` `!raub @User`",
        inline=False
    )
    embed.set_footer(text="VHA Spiele-Ranking  •  !ranking @User für Details", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


# ────────────────────────────────────────────────
# BOMBEN-ENTSCHÄRFER 💣
# ────────────────────────────────────────────────

class BombenView(discord.ui.View):
    def __init__(self, safe: str):
        super().__init__(timeout=30)
        self.safe     = safe
        self.resolved = False
        self.msg      = None  # wird nach dem Senden gesetzt

    async def _handle(self, interaction: discord.Interaction, color: str):
        if self.resolved:
            await interaction.response.send_message(
                "🇩🇪 Die Bombe wurde bereits entschärft!\n"
                "🇫🇷 La bombe a déjà été désamorcée !\n"
                "🇬🇧 The bomb has already been defused!",
                ephemeral=True
            )
            return
        self.resolved = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        name = interaction.user.display_name
        uid  = interaction.user.id
        if color == self.safe:
            pts = 15
            db_add_points(uid, name, pts)
            db_update_stats(uid, name, "win")
            embed = discord.Embed(title="💣 ENTSCHÄRFT! / DÉSAMORCÉE ! / DEFUSED!", color=0x2ECC71)
            embed.add_field(
                name=f"✅ {name}",
                value=(
                    f"🇩🇪 **{name}** hat den richtigen Draht (**{color}**) durchtrennt!\n"
                    f"🇫🇷 **{name}** a coupé le bon fil (**{color}**) !\n"
                    f"🇧🇷 **{name}** cortou o fio certo (**{color}**)!\n"
                    f"🇬🇧 **{name}** cut the right wire (**{color}**)!\n\n"
                    f"💰 **+{pts} Punkte / Points**"
                ),
                inline=False
            )
        else:
            pts = 10
            db_add_points(uid, name, -pts)
            db_update_stats(uid, name, "loss")
            embed = discord.Embed(title="💥 EXPLOSION! / EXPLOSION ! / EXPLOSION!", color=0xE74C3C)
            embed.add_field(
                name=f"💀 {name}",
                value=(
                    f"🇩🇪 **{name}** hat den falschen Draht (**{color}**) durchtrennt — BOOM! 💥\n"
                    f"🇫🇷 **{name}** a coupé le mauvais fil (**{color}**) — BOOM! 💥\n"
                    f"🇧🇷 **{name}** cortou o fio errado (**{color}**) — BOOM! 💥\n"
                    f"🇬🇧 **{name}** cut the wrong wire (**{color}**) — BOOM! 💥\n\n"
                    f"💸 **-{pts} Punkte / Points**\n"
                    f"ℹ️ 🇩🇪 Richtiger Draht war: **{self.safe}**  🇫🇷 Bon fil: **{self.safe}**  🇬🇧 Right wire: **{self.safe}**\n\n"
                    f"ℹ️ 🇩🇪 Bei falscher Wahl verlierst du **{pts} Punkte**.\n"
                    f"ℹ️ 🇫🇷 Mauvais choix = **{pts} points** perdus.\n"
                    f"ℹ️ 🇬🇧 Wrong choice = **{pts} points** lost."
                ),
                inline=False
            )
        embed.set_footer(text="VHA Bomben-Entschärfer", icon_url=LOGO_URL)
        await interaction.followup.send(embed=embed)

    @discord.ui.button(label="🔴 Rot / Rouge / Red", style=discord.ButtonStyle.danger, custom_id="bombe_rot")
    async def btn_rot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "rot")

    @discord.ui.button(label="🔵 Blau / Bleu / Blue", style=discord.ButtonStyle.primary, custom_id="bombe_blau")
    async def btn_blau(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "blau")

    @discord.ui.button(label="🟡 Gelb / Jaune / Yellow", style=discord.ButtonStyle.secondary, custom_id="bombe_gelb")
    async def btn_gelb(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "gelb")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.msg:
            try:
                await self.msg.edit(view=self)
                # Nur senden wenn noch niemand gedrückt hat
                if not self.resolved:
                    await self.msg.channel.send(
                        "⏰ 🇩🇪 **Zeit abgelaufen!** Die Bombe hat niemand entschärft — Explosion!\n"
                        "⏰ 🇫🇷 **Temps écoulé !** Personne n'a désamorcé la bombe — Explosion !\n"
                        "⏰ 🇧🇷 **Tempo esgotado!** Ninguém desarmou a bomba — Explosão!\n"
                        "⏰ 🇬🇧 **Time's up!** Nobody defused the bomb — Explosion! 💥"
                    )
            except Exception:
                pass


@bot.command(name="bombe", aliases=["bomb", "bombe💣"])
async def cmd_bombe(ctx):
    """Bomben-Entschärfer — schneide den richtigen Draht!"""
    safe = _random.choice(["rot", "blau", "gelb"])
    view = BombenView(safe=safe)
    embed = discord.Embed(title="💣 BOMBEN-ENTSCHÄRFER / DÉMINEUR / BOMB DEFUSER", color=0xE74C3C)
    embed.add_field(
        name="⚠️ Achtung / Attention / Warning",
        value=(
            "🇩🇪 Eine Bombe wurde entdeckt! Schneide den richtigen Draht durch!\n"
            "🇫🇷 Une bombe a été détectée ! Coupe le bon fil !\n"
            "🇧🇷 Uma bomba foi detectada! Corte o fio certo!\n"
            "🇬🇧 A bomb has been detected! Cut the right wire!\n\n"
            "🎯 **Wer zuerst drückt, entscheidet das Schicksal! / Whoever presses first decides the fate!**\n\n"
            "💰 🇩🇪 Richtig: **+15 Punkte** • Falsch: **-10 Punkte**\n"
            "💰 🇫🇷 Correct: **+15 points** • Mauvais: **-10 points**\n"
            "💰 🇧🇷 Correto: **+15 pontos** • Errado: **-10 pontos**\n"
            "💰 🇬🇧 Correct: **+15 points** • Wrong: **-10 points**"
        ),
        inline=False
    )
    embed.set_footer(text="⏱️ 30s • Jeder kann drücken! • VHA Bomben-Entschärfer", icon_url=LOGO_URL)
    msg = await ctx.send(embed=embed, view=view)
    view.msg = msg


# ────────────────────────────────────────────────
# HÖHER ODER TIEFER 📈
# ────────────────────────────────────────────────

class HotView(discord.ui.View):
    def __init__(self, author_id: int, current: int, streak: int, points: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.current   = current
        self.streak    = streak
        self.points    = points
        self.msg       = None  # wird nach dem Senden gesetzt
        self.finished  = False

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "🇩🇪 Das ist nicht dein Spiel!\n🇫🇷 Ce n'est pas ton jeu !\n🇬🇧 This is not your game!",
                ephemeral=True
            )
            return False
        return True

    async def _guess(self, interaction: discord.Interaction, higher: bool):
        if not await self._check_user(interaction):
            return
        next_roll = _random.randint(1, 6)
        tie     = next_roll == self.current
        correct = (higher and next_roll > self.current) or (not higher and next_roll < self.current)
        name = interaction.user.display_name
        uid  = interaction.user.id
        self.finished = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        if tie:
            embed = discord.Embed(title="🤝 Unentschieden! / Égalité ! / Tie!", color=0x9B59B6)
            embed.add_field(
                name=f"➡️ {name}",
                value=(
                    f"🇩🇪 Nächste Zahl war auch **{next_roll}** — Unentschieden! Punkte bleiben.\n"
                    f"🇫🇷 Le prochain chiffre était aussi **{next_roll}** — Égalité !\n"
                    f"🇧🇷 O próximo número também foi **{next_roll}** — Empate!\n"
                    f"🇬🇧 Next number was also **{next_roll}** — Tie! Points kept.\n\n"
                    f"💰 Gesichert / Secured: **{self.points} Punkte / Points**"
                ),
                inline=False
            )
            db_add_points(uid, name, self.points)
            db_update_stats(uid, name, "draw")
            embed.set_footer(text="VHA Höher oder Tiefer", icon_url=LOGO_URL)
            await interaction.followup.send(embed=embed)
            return

        if correct:
            self.streak += 1
            gained = 3 * self.streak
            self.points += gained
            new_view = HotView(author_id=uid, current=next_roll, streak=self.streak, points=self.points)
            new_view.finished = False
            embed = discord.Embed(title="✅ Richtig! / Correct ! / Correct!", color=0x2ECC71)
            embed.add_field(
                name=f"🔥 Streak x{self.streak} — {name}",
                value=(
                    f"🇩🇪 Nächste Zahl: **{next_roll}** — Richtig! **+{gained} Punkte**\n"
                    f"🇫🇷 Prochain chiffre: **{next_roll}** — Correct ! **+{gained} points**\n"
                    f"🇧🇷 Próximo número: **{next_roll}** — Correto! **+{gained} pontos**\n"
                    f"🇬🇧 Next number: **{next_roll}** — Correct! **+{gained} points**\n\n"
                    f"💰 Gesamt / Total: **{self.points} Punkte**\n\n"
                    f"🇩🇪 Weitermachen und riskieren oder Punkte sichern?\n"
                    f"🇫🇷 Continuer et risquer ou sécuriser les points ?\n"
                    f"🇧🇷 Continuar e arriscar ou garantir os pontos?\n"
                    f"🇬🇧 Keep going and risk it or secure your points?"
                ),
                inline=False
            )
            embed.set_footer(text=f"⏱️ 30s • Aktuelle Zahl: {next_roll} • VHA Höher oder Tiefer", icon_url=LOGO_URL)
            await interaction.followup.send(embed=embed, view=new_view)
        else:
            lost = self.points
            if lost > 0:
                db_add_points(uid, name, -lost)
            db_update_stats(uid, name, "loss")
            embed = discord.Embed(title="❌ Falsch! / Faux ! / Wrong!", color=0xE74C3C)
            embed.add_field(
                name=f"💀 {name}",
                value=(
                    f"🇩🇪 Nächste Zahl war **{next_roll}** — Falsch! Alle Punkte verloren!\n"
                    f"🇫🇷 Le prochain chiffre était **{next_roll}** — Faux ! Tous les points perdus !\n"
                    f"🇧🇷 O próximo número foi **{next_roll}** — Errado! Todos os pontos perdidos!\n"
                    f"🇬🇧 Next number was **{next_roll}** — Wrong! All points lost!\n\n"
                    f"💸 **-{lost} Punkte / Points** verloren / lost\n\n"
                    f"ℹ️ 🇩🇪 Bei falscher Antwort verlierst du alle gesammelten Punkte dieser Runde.\n"
                    f"ℹ️ 🇫🇷 Mauvaise réponse = tous les points de ce tour sont perdus.\n"
                    f"ℹ️ 🇬🇧 Wrong answer = all points collected this round are lost."
                ),
                inline=False
            )
            embed.set_footer(text="VHA Höher oder Tiefer", icon_url=LOGO_URL)
            await interaction.followup.send(embed=embed)

    @discord.ui.button(label="⬆️ Höher / Plus haut / Higher", style=discord.ButtonStyle.success, custom_id="hot_higher")
    async def btn_higher(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guess(interaction, higher=True)

    @discord.ui.button(label="⬇️ Tiefer / Plus bas / Lower", style=discord.ButtonStyle.danger, custom_id="hot_lower")
    async def btn_lower(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guess(interaction, higher=False)

    @discord.ui.button(label="💰 Stop & Sichern / Sécuriser / Cash Out", style=discord.ButtonStyle.secondary, custom_id="hot_stop")
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        name = interaction.user.display_name
        uid  = interaction.user.id
        db_add_points(uid, name, self.points)
        db_update_stats(uid, name, "win")
        embed = discord.Embed(title="💰 Gesichert! / Sécurisé ! / Cashed Out!", color=0xF1C40F)
        embed.add_field(
            name=f"✅ {name}",
            value=(
                f"🇩🇪 **{name}** sichert **{self.points} Punkte**!\n"
                f"🇫🇷 **{name}** sécurise **{self.points} points** !\n"
                f"🇧🇷 **{name}** garantiu **{self.points} pontos**!\n"
                f"🇬🇧 **{name}** cashes out **{self.points} points**!"
            ),
            inline=False
        )
        embed.set_footer(text="VHA Höher oder Tiefer", icon_url=LOGO_URL)
        await interaction.followup.send(embed=embed)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        if self.msg:
            try:
                await self.msg.edit(view=self)
            except Exception:
                pass
        # Punkte automatisch sichern falls vorhanden
        if self.points > 0:
            try:
                # Wir können author_id nutzen um Punkte zu sichern
                col = _get_dice_col()
                col.update_one(
                    {"user_id": self.author_id},
                    {"$setOnInsert": {"user_id": self.author_id, "wins": 0, "losses": 0, "draws": 0, "games": 0, "points": 0}},
                    upsert=True,
                )
                col.update_one(
                    {"user_id": self.author_id},
                    {"$inc": {"points": self.points}},
                )
            except Exception:
                pass
            if self.msg:
                try:
                    await self.msg.channel.send(
                        f"⏰ 🇩🇪 **Zeit abgelaufen!** Gesammelte **{self.points} Punkte** wurden automatisch gesichert.\n"
                        f"⏰ 🇫🇷 **Temps écoulé !** **{self.points} points** collectés ont été sauvegardés automatiquement.\n"
                        f"⏰ 🇧🇷 **Tempo esgotado!** **{self.points} pontos** coletados foram salvos automaticamente.\n"
                        f"⏰ 🇬🇧 **Time's up!** Collected **{self.points} points** were automatically secured."
                    )
                except Exception:
                    pass
        else:
            if self.msg:
                try:
                    await self.msg.channel.send(
                        "⏰ 🇩🇪 **Zeit abgelaufen!** Keine Punkte zu sichern.\n"
                        "⏰ 🇫🇷 **Temps écoulé !** Aucun point à sauvegarder.\n"
                        "⏰ 🇬🇧 **Time's up!** No points to secure."
                    )
                except Exception:
                    pass


@bot.command(name="hot", aliases=["höher", "highlow", "hochtief"])
async def cmd_hot(ctx):
    """Höher oder Tiefer mit Streak-Bonus."""
    start = _random.randint(1, 6)
    view  = HotView(author_id=ctx.author.id, current=start, streak=0, points=0)
    embed = discord.Embed(
        title="📈 HÖHER ODER TIEFER / PLUS HAUT OU PLUS BAS / HIGHER OR LOWER",
        color=0x3498DB
    )
    embed.add_field(
        name=f"🎲 {ctx.author.display_name}",
        value=(
            f"🇩🇪 Aktuelle Zahl: **{start}** — Ist die nächste höher oder tiefer?\n"
            f"🇫🇷 Chiffre actuel: **{start}** — Le suivant est-il plus haut ou plus bas ?\n"
            f"🇧🇷 Número atual: **{start}** — O próximo é maior ou menor?\n"
            f"🇬🇧 Current number: **{start}** — Is the next one higher or lower?\n\n"
            f"🔥 🇩🇪 Jeder richtige Tipp = mehr Punkte (Streak-Bonus x3)!\n"
            f"🔥 🇫🇷 Chaque bonne réponse = plus de points (bonus de série x3) !\n"
            f"🔥 🇧🇷 Cada resposta correta = mais pontos (bônus x3)!\n"
            f"🔥 🇬🇧 Each correct guess = more points (streak bonus x3)!\n\n"
            f"💸 🇩🇪 Falsch geraten = **alle Punkte verloren!**\n"
            f"💸 🇫🇷 Mauvaise réponse = **tous les points perdus !**\n"
            f"💸 🇬🇧 Wrong guess = **all points lost!**"
        ),
        inline=False
    )
    embed.set_footer(
        text=f"⏱️ 30s • Nur {ctx.author.display_name} kann klicken • VHA Höher oder Tiefer",
        icon_url=LOGO_URL
    )
    msg = await ctx.send(embed=embed, view=view)
    view.msg = msg


# ────────────────────────────────────────────────
# RUSSISCHES ROULETTE 🎰
# ────────────────────────────────────────────────

ROULETTE_TIMER = 30

_roulette_games: dict = {}


class RouletteJoinView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=ROULETTE_TIMER)
        self.channel_id = channel_id
        self.msg        = None

    @discord.ui.button(
        label="🔫 Beitreten / Rejoindre / Entrar / Join",
        style=discord.ButtonStyle.danger,
        custom_id="roulette_join"
    )
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = _roulette_games.get(self.channel_id)
        if not game:
            await interaction.response.send_message("❌ Spiel nicht mehr aktiv.", ephemeral=True)
            return
        uid  = interaction.user.id
        name = interaction.user.display_name
        if any(p["id"] == uid for p in game["players"]):
            await interaction.response.send_message(
                "🇩🇪 Du bist bereits dabei!\n🇫🇷 Tu es déjà inscrit !\n🇬🇧 You already joined!",
                ephemeral=True
            )
            return
        game["players"].append({"id": uid, "name": name})
        count = len(game["players"])
        # Ephemeral-Bestätigung nur für den Drückenden
        await interaction.response.send_message(
            f"✅ Du bist dabei, **{name}**!",
            ephemeral=True
        )
        # Öffentliche Nachricht für alle sichtbar
        await interaction.channel.send(
            f"🔫 **{name}** 🇩🇪 ist beigetreten / 🇫🇷 a rejoint / 🇧🇷 entrou / 🇬🇧 joined! **({count})**"
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.msg:
            try:
                await self.msg.edit(view=self)
            except Exception:
                pass


@bot.command(name="roulette", aliases=["russisch", "russischeroulette"])
async def cmd_roulette(ctx):
    """Russisches Roulette — min. 2 Spieler, einer verliert Punkte."""
    channel_id = ctx.channel.id
    if channel_id in _roulette_games:
        await ctx.send(
            "🇩🇪 Es läuft bereits ein Roulette!\n"
            "🇫🇷 Une roulette est déjà en cours !\n"
            "🇬🇧 A roulette is already running!",
            delete_after=6
        )
        return

    _roulette_games[channel_id] = {
        "players": [{"id": ctx.author.id, "name": ctx.author.display_name}]
    }

    view = RouletteJoinView(channel_id)
    embed = discord.Embed(
        title="🎰 RUSSISCHES ROULETTE / ROULETTE RUSSE / RUSSIAN ROULETTE",
        color=0xE74C3C
    )
    embed.add_field(
        name="🔫 Wer wagt es?",
        value=(
            f"🇩🇪 **{ctx.author.display_name}** startet Russisches Roulette!\n"
            f"🇫🇷 **{ctx.author.display_name}** lance la roulette russe !\n"
            f"🇧🇷 **{ctx.author.display_name}** inicia a roleta russa!\n"
            f"🇬🇧 **{ctx.author.display_name}** starts Russian Roulette!\n\n"
            f"🇩🇪 Klicke Beitreten! Auswertung in **{ROULETTE_TIMER}s** (min. 2 Spieler nötig!)\n"
            f"🇫🇷 Clique Rejoindre ! Résultat dans **{ROULETTE_TIMER}s** (min. 2 joueurs requis !)\n"
            f"🇧🇷 Clique Entrar! Resultado em **{ROULETTE_TIMER}s** (mín. 2 jogadores!)\n"
            f"🇬🇧 Click Join! Result in **{ROULETTE_TIMER}s** (min. 2 players needed!)\n\n"
            f"💀 🇩🇪 Verlierer: **-20 Punkte** | 🇫🇷 Perdant: **-20 points** | 🇬🇧 Loser: **-20 points**\n"
            f"🏆 🇩🇪 Überlebende: **+8 Punkte** | 🇫🇷 Survivants: **+8 points** | 🇬🇧 Survivors: **+8 points**"
        ),
        inline=False
    )
    embed.set_footer(text=f"⏱️ {ROULETTE_TIMER}s • min. 2 Spieler • VHA Russisches Roulette", icon_url=LOGO_URL)
    msg = await ctx.send(embed=embed, view=view)
    view.msg = msg

    await asyncio.sleep(ROULETTE_TIMER)

    game = _roulette_games.pop(channel_id, None)
    if not game:
        return

    players = game["players"]
    for item in view.children:
        item.disabled = True
    try:
        await msg.edit(view=view)
    except Exception:
        pass

    if len(players) < 2:
        await ctx.send(
            "⏰ 🇩🇪 **Zeit abgelaufen!** Niemand ist beigetreten — Roulette abgebrochen. Mindestens 2 Spieler nötig!\n"
            "⏰ 🇫🇷 **Temps écoulé !** Personne n'a rejoint — roulette annulée. Minimum 2 joueurs requis !\n"
            "⏰ 🇧🇷 **Tempo esgotado!** Ninguém entrou — roleta cancelada. Mínimo 2 jogadores!\n"
            "⏰ 🇬🇧 **Time's up!** Nobody joined — roulette cancelled. Minimum 2 players needed!"
        )
        return

    loser   = _random.choice(players)
    winners = [p for p in players if p["id"] != loser["id"]]

    try:
        db_add_points(loser["id"], loser["name"], -20)
        db_update_stats(loser["id"], loser["name"], "loss")
        for w in winners:
            db_add_points(w["id"], w["name"], 8)
            db_update_stats(w["id"], w["name"], "win")
    except Exception as e:
        log.error(f"Roulette DB error: {e}")

    winner_names = " • ".join(w["name"] for w in winners)

    embed_result = discord.Embed(
        title="🎰 ROULETTE — ERGEBNIS / RÉSULTAT / RESULTADO / RESULT",
        color=0xE74C3C
    )
    embed_result.add_field(
        name=f"💀 Verlierer / Perdant / Perdedor / Loser: {loser['name']}",
        value=(
            f"🇩🇪 **{loser['name']}** hat das Pech gehabt! **-20 Punkte**\n"
            f"🇫🇷 **{loser['name']}** a eu la malchance ! **-20 points**\n"
            f"🇧🇷 **{loser['name']}** teve azar! **-20 pontos**\n"
            f"🇬🇧 **{loser['name']}** had the bad luck! **-20 points**"
        ),
        inline=False
    )
    embed_result.add_field(
        name=f"🏆 Überlebende / Survivants / Sobreviventes / Survivors: {winner_names}",
        value=(
            f"🇩🇪 Alle anderen: **+8 Punkte** pro Spieler\n"
            f"🇫🇷 Tous les autres: **+8 points** par joueur\n"
            f"🇧🇷 Todos os outros: **+8 pontos** por jogador\n"
            f"🇬🇧 Everyone else: **+8 points** per player"
        ),
        inline=False
    )
    embed_result.set_footer(text="VHA Russisches Roulette", icon_url=LOGO_URL)
    await ctx.send(embed=embed_result)


# ────────────────────────────────────────────────
# RAUBZUG 🦹
# ────────────────────────────────────────────────

_jail: dict = {}
JAIL_MINUTES = 5
JAIL_MINUTES = 5

@bot.command(name="raub", aliases=["steal", "vol", "roubo"])
async def cmd_raub(ctx, target: discord.Member = None):
    """Raubzug — stehle Punkte von einem anderen Spieler!"""
    if target is None:
        await ctx.send(
            "🇩🇪 Benutzung: `!raub @Spieler`\n"
            "🇫🇷 Utilisation: `!raub @joueur`\n"
            "🇧🇷 Uso: `!raub @jogador`\n"
            "🇬🇧 Usage: `!raub @player`",
            delete_after=8
        )
        return
    if target.id == ctx.author.id:
        await ctx.send(
            "🇩🇪 Du kannst dich nicht selbst ausrauben!\n"
            "🇫🇷 Tu ne peux pas te voler toi-même !\n"
            "🇬🇧 You can't rob yourself!",
            delete_after=6
        )
        return
    if target.bot:
        await ctx.send(
            "🇩🇪 Bots haben keine Punkte!\n"
            "🇫🇷 Les bots n'ont pas de points !\n"
            "🇬🇧 Bots have no points!",
            delete_after=6
        )
        return

    now = time.time()
    if ctx.author.id in _jail and now < _jail[ctx.author.id]:
        remaining = int((_jail[ctx.author.id] - now) / 60) + 1
        await ctx.send(
            f"🔒 🇩🇪 **{ctx.author.display_name}** sitzt noch **{remaining} Min.** im Gefängnis — kein Raubzug möglich!\n"
            f"🔒 🇫🇷 **{ctx.author.display_name}** est encore en prison pour **{remaining} min** !\n"
            f"🔒 🇧🇷 **{ctx.author.display_name}** ainda está na prisão por **{remaining} min**!\n"
            f"🔒 🇬🇧 **{ctx.author.display_name}** is still in jail for **{remaining} min** — no robbery!",
            delete_after=10
        )
        return

    attacker_name = ctx.author.display_name
    defender_name = target.display_name
    success = _random.random() < 0.30

    if success:
        stolen = _random.randint(5, 20)
        db_add_points(ctx.author.id, attacker_name, stolen)
        db_add_points(target.id, defender_name, -stolen)
        db_update_stats(ctx.author.id, attacker_name, "win")
        db_update_stats(target.id, defender_name, "loss")
        embed = discord.Embed(
            title="🦹 RAUBZUG ERFOLGREICH! / BRAQUAGE RÉUSSI ! / ROBBERY SUCCESSFUL!",
            color=0x2ECC71
        )
        embed.add_field(
            name=f"💰 {attacker_name} → {defender_name}",
            value=(
                f"🇩🇪 **{attacker_name}** hat **{defender_name}** erfolgreich ausgeraubt! **+{stolen} Punkte**\n"
                f"🇫🇷 **{attacker_name}** a réussi à voler **{defender_name}** ! **+{stolen} points**\n"
                f"🇧🇷 **{attacker_name}** roubou com sucesso de **{defender_name}**! **+{stolen} pontos**\n"
                f"🇬🇧 **{attacker_name}** successfully robbed **{defender_name}**! **+{stolen} points**\n\n"
                f"💸 **{defender_name}**: **-{stolen} Punkte / Points**"
            ),
            inline=False
        )
    else:
        jail = _random.random() < 0.50
        if jail:
            _jail[ctx.author.id] = now + JAIL_MINUTES * 60
            db_update_stats(ctx.author.id, attacker_name, "loss")
            embed = discord.Embed(
                title="🔒 ERWISCHT & VERHAFTET! / ARRÊTÉ ! / CAUGHT & JAILED!",
                color=0xE74C3C
            )
            embed.add_field(
                name=f"👮 {attacker_name}",
                value=(
                    f"🇩🇪 **{attacker_name}** wurde beim Rauben erwischt — ab ins Gefängnis!\n"
                    f"🇫🇷 **{attacker_name}** a été arrêté — direction la prison !\n"
                    f"🇧🇷 **{attacker_name}** foi pego roubando — para a prisão!\n"
                    f"🇬🇧 **{attacker_name}** was caught robbing — off to jail!\n\n"
                    f"⛓️ 🇩🇪 Gesperrt für **{JAIL_MINUTES} Minuten** — kein Raubzug möglich!\n"
                    f"⛓️ 🇫🇷 Bloqué pendant **{JAIL_MINUTES} minutes** — aucun vol possible !\n"
                    f"⛓️ 🇧🇷 Bloqueado por **{JAIL_MINUTES} minutos** — sem roubo!\n"
                    f"⛓️ 🇬🇧 Locked for **{JAIL_MINUTES} minutes** — no robbery possible!\n\n"
                    f"ℹ️ 🇩🇪 **Gefängnis**: Du kannst {JAIL_MINUTES} Minuten lang nicht rauben.\n"
                    f"ℹ️ 🇫🇷 **Prison**: Tu ne peux pas voler pendant {JAIL_MINUTES} minutes.\n"
                    f"ℹ️ 🇬🇧 **Jail**: You cannot rob for {JAIL_MINUTES} minutes."
                ),
                inline=False
            )
        else:
            penalty = _random.randint(5, 15)
            db_add_points(ctx.author.id, attacker_name, -penalty)
            db_add_points(target.id, defender_name, penalty)
            db_update_stats(ctx.author.id, attacker_name, "loss")
            embed = discord.Embed(
                title="😤 RAUBZUG GESCHEITERT! / BRAQUAGE RATÉ ! / ROBBERY FAILED!",
                color=0xF39C12
            )
            embed.add_field(
                name=f"🛡️ {defender_name} hat sich gewehrt!",
                value=(
                    f"🇩🇪 **{defender_name}** hat **{attacker_name}** erwischt! **-{penalty} Punkte** für {attacker_name}\n"
                    f"🇫🇷 **{defender_name}** a attrapé **{attacker_name}** ! **-{penalty} points** pour {attacker_name}\n"
                    f"🇧🇷 **{defender_name}** pegou **{attacker_name}**! **-{penalty} pontos** para {attacker_name}\n"
                    f"🇬🇧 **{defender_name}** caught **{attacker_name}**! **-{penalty} points** for {attacker_name}\n\n"
                    f"💰 **{defender_name}**: **+{penalty} Punkte / Points**\n\n"
                    f"ℹ️ 🇩🇪 **Raubzug**: 30% Chance. Bei Misserfolg: Punkte verloren ODER Gefängnis.\n"
                    f"ℹ️ 🇫🇷 **Braquage**: 30% de chance. En cas d'échec: points perdus OU prison.\n"
                    f"ℹ️ 🇬🇧 **Robbery**: 30% chance. On failure: points lost OR jail."
                ),
                inline=False
            )

    embed.set_footer(text="🦹 VHA Raubzug • 30% Erfolgs-Chance", icon_url=LOGO_URL)
    await ctx.send(embed=embed)


@bot.command(name="translate")
@commands.has_permissions(manage_messages=True)
async def cmd_translate(ctx, action: str = None):
    global translate_active
    if action is None:
        await ctx.send("❓ Benutzung: `!ttranslate on` / `!ttranslate off` / `!ttranslate status`")
        return
    action = action.lower()
    if action == "on":
        translate_active = True
        await ctx.send("✅ Übersetzer-Bot **aktiviert**.")
    elif action == "off":
        translate_active = False
        await ctx.send("🔴 Übersetzer-Bot **deaktiviert**.")
    elif action == "status":
        status = "✅ Aktiv" if translate_active else "🔴 Inaktiv"
        await ctx.send(f"**Übersetzer-Bot Status:** {status}\n**Sprachen:** {', '.join(sorted(get_active_languages()))}")
    else:
        await ctx.send("❓ Unbekannte Option.")



# ────────────────────────────────────────────────
# SPRACHEN & RAUMSPRACHEN
# ────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    global processed_messages, processed_messages_set, translate_active

    if message.author.bot:
        return

    if (
        any(a.filename.lower().endswith(".gif") or (a.content_type and "gif" in a.content_type.lower())
            for a in message.attachments)
        or re.search(r'https?://\S*(?:tenor\.com|giphy\.com|youtube\.com|youtu\.be|youtube-nocookie\.com|yt\.be)\S*', message.content, re.IGNORECASE)
        or message.stickers
    ):
        return

    # Befehle (!...) zuerst verarbeiten — niemals durch Dedup blockieren
    msg_stripped = message.content.strip()
    if msg_stripped and msg_stripped.startswith("!"):
        await bot.process_commands(message)
        return

    # Dedup: RAM-Set (schnell) + SQLite (persistent) — nur für Übersetzungen
    if message.id in processed_messages_set:
        return
    processed_messages_set.add(message.id)
    try:
        msg_db.execute("INSERT OR IGNORE INTO processed (msg_id) VALUES (?)", (message.id,))
        msg_db.commit()
    except Exception:
        pass

    if not translate_active:
        return

    content = message.content.strip()
    if not content or len(content) < 2:
        return

    if re.match(r'^https?://\S+$', content):
        return

    content_cleaned = re.sub(r'https?://\S+', '', content).strip()
    if not content_cleaned or len(content_cleaned) < 2:
        return
    content = content_cleaned

    now = time.time()
    cooldown_remaining = TRANSLATION_COOLDOWN - (now - user_last_translation.get(message.author.id, 0))
    if cooldown_remaining > 0:
        log.debug(f"⏳ COOLDOWN [{getattr(message.guild, 'name', 'DM')}] #{getattr(message.channel, 'name', '?')} | user:{message.author.display_name} | noch {cooldown_remaining:.1f}s")
        return
    user_last_translation[message.author.id] = now

    FORUM_CHANNEL_ID = 1478065008960077866
    channel_id = message.channel.id
    parent_id = getattr(message.channel, 'parent_id', None)
    guild_name = message.guild.name if message.guild else "DM"
    channel_name = getattr(message.channel, 'name', str(channel_id))

    lang = await detect_language_llm(content)
    log.debug(f"🔤 Sprache erkannt: [{guild_name}] #{channel_name} | '{content[:40]}' → {lang}")
    if lang == "OTHER":
        log.info(f"⏭️  SKIP OTHER [{guild_name}] #{channel_name} | user:{message.author.display_name} | '{content[:40]}'")
        return

    # HARDCODED Räume — immer diese Sprachen, kein DB-Lookup
    HARDCODED_ROOMS = {
        1498224449529577595: {"FR", "EN"},
        1508963594862067793: set(),  # Hermes Dev-Terminal — kein Übersetzen
    }

    if channel_id in HARDCODED_ROOMS:
        room_setting = HARDCODED_ROOMS[channel_id]
        log.info(f"🔒 HARDCODED [{guild_name}] #{channel_name} → Sprachen: {room_setting}")
    elif channel_id == FORUM_CHANNEL_ID or parent_id == FORUM_CHANNEL_ID:
        room_setting = {"PT", "EN", "DE", "FR"}
        log.info(f"📋 FORUM [{guild_name}] #{channel_name} → Sprachen: {room_setting}")
    else:
        try:
            from tsprachen import get_room_langs
            room_setting = get_room_langs(message.channel.id)
            if room_setting is None and hasattr(message.channel, "parent_id") and message.channel.parent_id:
                log.debug(f"🔍 Raumsprache: Kanal {channel_id} nicht gefunden, suche Parent {message.channel.parent_id}...")
                room_setting = get_room_langs(message.channel.parent_id)
                if room_setting is not None:
                    log.info(f"✅ Raumsprache via Parent [{guild_name}] #{channel_name} (parent:{message.channel.parent_id}) → {room_setting}")
                else:
                    log.debug(f"⬜ Kein Raumsprachen-Eintrag [{guild_name}] #{channel_name} → globale Einstellungen")
            elif room_setting is not None:
                log.info(f"✅ Raumsprache [{guild_name}] #{channel_name} (id:{channel_id}) → {room_setting}")
            else:
                log.debug(f"⬜ Kein Raumsprachen-Eintrag [{guild_name}] #{channel_name} (id:{channel_id}) → globale Einstellungen")
        except Exception as e:
            log.error(f"❌ Raumsprachen-Fehler [{guild_name}] #{channel_name} | {type(e).__name__}: {e}")
            room_setting = None

    if room_setting is not None:
        if len(room_setting) == 0:
            log.info(f"🚫 ÜBERSETZUNG DEAKTIVIERT [{guild_name}] #{channel_name} (explizit disabled)")
            return
        active_langs = room_setting
    else:
        active_langs = get_active_languages()
        log.debug(f"🌐 Globale Sprachen [{guild_name}] #{channel_name} → {active_langs}")

    ALL_LANGS_FULL = [
        ("DE", "German",               "🇩🇪 Deutsch"),
        ("FR", "French",               "🇫🇷 Français"),
        ("PT", "Brazilian Portuguese", "🇧🇷 Português"),
        ("EN", "English",              "🇬🇧 English"),
        ("JA", "Japanese",             "🇯🇵 日本語"),
        ("ZH", "Chinese",              "🇨🇳 中文"),
        ("KO", "Korean",               "🇰🇷 한국어"),
        ("ES", "Spanish",              "🇪🇸 Español"),
        ("RU", "Russian",              "🇷🇺 Русский"),
        ("TR", "Turkish",              "🇹🇷 Türkçe"),
    ]

    lang_pool = ALL_LANGS_FULL

    target_langs = [
        t for t in lang_pool
        if t[0] != lang and t[0] in active_langs
    ]

    if not target_langs:
        return

    author_name = message.author.display_name

    def make_embed(fields: list) -> discord.Embed:
        embed = discord.Embed(title=f"💬 • {author_name}", color=0x2ECC71)
        for flag, text in fields:
            if len(text) <= 1000:
                embed.add_field(name=flag, value=text, inline=False)
            else:
                chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
                embed.add_field(name=flag, value=chunks[0], inline=False)
                for chunk in chunks[1:]:
                    embed.add_field(name="↳", value=chunk, inline=False)
        embed.set_footer(text="Noxxi's Übersetzer", icon_url=LOGO_URL)
        return embed

    # ── PERFORMANCE LOGGING START ──
    import time as _time
    perf_start = _time.perf_counter()
    discord_delay_ms = int((_time.time() - message.created_at.timestamp()) * 1000)

    # Cache-Key vorhersagen (wie in translate_all)
    codes = [c for c, _, _ in target_langs]
    cache_key = f"{content[:200]}_{'_'.join(codes)}"
    cache_hit = cache_key in translation_cache

    # Kontext: letzte 4 Nachrichten aus dem Kanal laden
    context_lines = []
    try:
        async for ctx_msg in message.channel.history(limit=5):
            if ctx_msg.id == message.id:
                continue
            if ctx_msg.author.bot:
                continue
            if ctx_msg.content and len(ctx_msg.content.strip()) > 1:
                context_lines.append(f"{ctx_msg.author.display_name}: {ctx_msg.content.strip()[:150]}")
            if len(context_lines) >= 4:
                break
        context_lines.reverse()
    except Exception:
        pass
    context_str = "\n".join(context_lines)

    try:
        translations = await translate_all(content, target_langs, context=context_str)
        fields = []
        for code, _, label in target_langs:
            translation = translations.get(code, "")
            if translation:
                fields.append((label, translation))

        if fields:
            await message.reply(embed=make_embed(fields), mention_author=False)

        total_ms = int((_time.perf_counter() - perf_start) * 1000)

        # Log mit allen Details
        fields_count = len(fields)
        log.info(
            f"✅ ÜBERSETZT [{guild_name}] #{channel_name} | "
            f"user:{message.author.display_name} | "
            f"lang:{lang}→{','.join(codes)} | "
            f"felder:{fields_count}/{len(target_langs)} | "
            f"discord:{discord_delay_ms}ms | cache:{'HIT 💾' if cache_hit else 'MISS'} | "
            f"total:{total_ms}ms | zeichen:{len(content)}"
        )

    except Exception as e:
        total_ms = int((_time.perf_counter() - perf_start) * 1000)
        log.error(
            f"❌ ÜBERSETZUNGSFEHLER [{guild_name}] #{channel_name} | "
            f"user:{message.author.display_name} | lang:{lang} | "
            f"nach {total_ms}ms | {type(e).__name__}: {e}"
        )
        try:
            await message.add_reaction("⚠️")
        except Exception:
            pass


# ────────────────────────────────────────────────
# FLASK KEEPALIVE (nur für Render Web Service)
# Render Free-Tier braucht eingehenden HTTP-Traffic,
# sonst schläft der Service nach 15 Min Inaktivität ein.
# Mit UptimeRobot/cron-job.org regelmäßig diese URL
# anpingen, damit der Bot durchläuft.
# ────────────────────────────────────────────────

from flask import Flask

_keepalive_app = Flask("vha_translate_bot_keepalive")


@_keepalive_app.route("/")
def _keepalive_root():
    status = "online" if bot.is_ready() else "starting"
    return {"status": status, "bot": str(bot.user) if bot.user else None}, 200


def _run_keepalive_server():
    port = int(os.getenv("PORT", 8080))
    # use_reloader=False ist wichtig, sonst startet Flask den Prozess doppelt
    _keepalive_app.run(host="0.0.0.0", port=port, use_reloader=False)


# ────────────────────────────────────────────────
# START
# ────────────────────────────────────────────────

if __name__ == "__main__":

    token = os.getenv("DISCORD_TOKEN_TRANSLATOR")
    if not token:
        log.error("DISCORD_TOKEN_TRANSLATOR fehlt!")
        exit(1)

    # Nur auf Render (oder wenn PORT gesetzt ist) den Keepalive-Server starten,
    # lokal beim Testen läuft der Bot ganz normal ohne HTTP-Server.
    if os.getenv("PORT"):
        threading.Thread(target=_run_keepalive_server, daemon=True).start()
        log.info("🌐 Flask-Keepalive gestartet (Render Web Service Modus)")

    bot.run(token)
