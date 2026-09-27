"""Daily All News: RSS collection, multilingual headline deduplication, Telegram digests.
Requires Python >=3.11. Secrets must be provided only through environment variables.
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import json
import functools
import logging
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0

load_dotenv()
LOG = logging.getLogger("dailyallnews")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "news.db"))
MAX_ITEMS = max(1, min(30, int(os.getenv("MAX_ITEMS_PER_DIGEST", "16"))))
HEADERS = {"User-Agent": "DailyAllNews/0.3 (personal news aggregator)"}

BOT_DESCRIPTION = ("Персональний агрегатор новин України та світу: війна, політика, БпЛА, "
                   "робототехніка, ШІ, військові технології, кібербезпека й електроніка. "
                   "Групує схожі повідомлення, перекладає іноземні заголовки українською "
                   "та надсилає один дайджест о 08:00 і 20:00 за Києвом. "
                   "Посилання на першоджерела додаються до кожної події.")
BOT_SHORT_DESCRIPTION = "Головні новини без повторів. Українською, двічі на день."
TOPIC_ICONS = {
    "БпЛА та робототехніка": "🛩️", "Штучний інтелект": "🧠",
    "Україна та світ": "🌍", "Військові технології": "🛡️",
    "Кібербезпека": "🔐", "Технології та електроніка": "💻",
    "Війна": "📍", "Політика": "🏛️",
}

NOW = lambda: dt.datetime.now(dt.timezone.utc)
TOPICS = {
    "БпЛА та робототехніка": ["drone UAV robotics", "безпілотники робототехніка"],
    "Штучний інтелект": ["artificial intelligence AI"],
    "Україна та світ": ["Ukraine world international news"],
    "Військові технології": ["defense military technology", "військові технології"],
    "Кібербезпека": ["cybersecurity cyberattack"],
    "Технології та електроніка": ["semiconductor electronics technology"],
    "Війна": ["Ukraine war фронт"],
    "Політика": ["Ukraine international politics diplomacy"],
}
FEEDS = [
    (topic, f"https://news.google.com/rss/search?q={quote(query + ' when:2d')}&hl=uk&gl=UA&ceid=UA:uk")
    for topic, queries in TOPICS.items() for query in queries
]
FEEDS += [
    ("Україна та світ", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Технології та електроніка", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("Штучний інтелект", "https://techcrunch.com/feed/"),
    ("Кібербезпека", "https://www.bleepingcomputer.com/feed/"),
]
STOP = set(("the a an and or of in on for to is are with from after over about as by at be this that "
            "та і й в у на до за з із про для від під щодо після через новини news live update updates "
            "ukraine україна україни українські війна war").split())
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS events("
               "id INTEGER PRIMARY KEY, title TEXT NOT NULL, norm TEXT NOT NULL, "
               "topic TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
               "last_sent TEXT DEFAULT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS articles("
               "url TEXT PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id),"
               "publisher TEXT, seen TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_sent ON events(last_sent,first_seen)")
    db.execute("CREATE TABLE IF NOT EXISTS deliveries("
               "slot TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS translated_titles("
               "source TEXT PRIMARY KEY, ukrainian TEXT NOT NULL, translated_at TEXT NOT NULL)")
    db.commit()
    return db

def normalize(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    text = re.sub(r"\s+[-–—|]\s+[^–—|]{3,60}$", "", text)
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())

def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.
    if a == b:
        return 1.
    sa, sb = set(a.split()) - STOP, set(b.split()) - STOP
    jac = len(sa & sb) / len(sa | sb) if sa | sb else 0.
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    return max(jac if len(sa & sb) >= 3 else 0., seq)

def clean_url(url: str) -> str:
    try:
        s = urlsplit(url.strip())
        if s.scheme not in ("https", "http") or not s.netloc:
            return ""
        query = urlencode([(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
                           if k.lower() not in TRACKING and not k.lower().startswith("utm_")])
        return urlunsplit((s.scheme.lower(), s.netloc.lower(), s.path.rstrip("/") or "/", query, ""))
    except ValueError:
        return ""

def add(db: sqlite3.Connection, title: str, url: str, topic: str, publisher: str = "", now=None) -> bool:
    url, norm = clean_url(url), normalize(title)
    if not norm or not url or len(norm) < 15:
        return False
    if db.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone():
        return False
    now = now or NOW().isoformat()
    # Compare only recent events, to avoid grouping unrelated recurring stories months apart.
    since = (dt.datetime.fromisoformat(now) - dt.timedelta(hours=54)).isoformat()
    rows = db.execute("SELECT id,norm FROM events WHERE last_seen>=? ORDER BY id DESC LIMIT 1400", (since,)).fetchall()
    match = next((r["id"] for r in rows if similarity(norm, r["norm"]) >= .86), None)
    if match is None:
        cursor = db.execute(
            "INSERT INTO events(title,norm,topic,first_seen,last_seen) VALUES(?,?,?,?,?)",
            (title, norm, topic, now, now),
        )
        match = cursor.lastrowid
    else:
        db.execute("UPDATE events SET last_seen=? WHERE id=?", (now, match))
    db.execute("INSERT OR IGNORE INTO articles(url,event_id,publisher,seen) VALUES(?,?,?,?)",
               (url, match, publisher, now))
    return True

def collect() -> dict:
    db = connect()
    counts = defaultdict(int)
    try:
        for topic, url in FEEDS:
            try:
                response = requests.get(url, headers=HEADERS, timeout=18)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                for entry in feed.entries[:35]:
                    title = html.unescape(re.sub(r"<[^>]+>", "", entry.get("title", ""))).strip()
                    link = entry.get("link", "")
                    source = entry.get("source") or {}
                    publisher = source.get("title", "") if hasattr(source, "get") else ""
                    # Google RSS often appends the publisher after a dash.
                    if not publisher and " - " in title:
                        publisher = title.rsplit(" - ", 1)[-1]
                    if add(db, title, link, topic, publisher):
                        counts[topic] += 1
                db.commit()
            except Exception as exc:
                LOG.warning("Cannot fetch %s: %s", topic, exc)
    finally:
        db.close()
    LOG.info("Added article links by topic: %s", dict(counts))
    return dict(counts)

def telegram(method: str, payload: dict):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    res = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=35)
    res.raise_for_status()
    data = res.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram error"))
    return data.get("result")

def send(text: str, chat_id: str | int = CHAT_ID, parse_mode: str | None = None):
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return telegram("sendMessage", payload)

def chat_ids():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN first")
    res = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=25)
    res.raise_for_status()
    for u in res.json().get("result", []):
        c = (u.get("message") or {}).get("chat", {})
        if c:
            print("chat_id=", c.get("id"), "type=", c.get("type"))

def _candidates(db: sqlite3.Connection):
    # Distribute places across topics rather than allowing one trending topic to dominate.
    rows = db.execute(
        "SELECT e.id,e.title,e.topic,e.first_seen,"
        "(SELECT url FROM articles a WHERE a.event_id=e.id ORDER BY a.seen ASC LIMIT 1) url,"
        "(SELECT COUNT(*) FROM articles a WHERE a.event_id=e.id) sources "
        "FROM events e WHERE e.last_sent IS NULL ORDER BY e.first_seen DESC LIMIT 350"
    ).fetchall()
    groups = defaultdict(list)
    for row in rows:
        groups[row["topic"]].append(row)
    chosen = []
    while len(chosen) < MAX_ITEMS and any(groups.values()):
        for topic in TOPICS:
            if groups[topic] and len(chosen) < MAX_ITEMS:
                chosen.append(groups[topic].pop(0))
    return chosen

def translate_title(db: sqlite3.Connection, title: str) -> tuple[str, bool]:
    """Translate a foreign-language headline to Ukrainian, cache only successful translations.

    The translation service is best effort. When it is unavailable, retain the
    original instead of inventing a summary or reporting an unverified translation.
    """
    title = str(title).strip()
    row = db.execute("SELECT ukrainian FROM translated_titles WHERE source=?", (title,)).fetchone()
    if row:
        return row["ukrainian"], True
    # Titles with substantial Ukrainian Cyrillic should not be machine-translated.
    try:
        detected = detect(title)
    except Exception:
        detected = "unknown"
    if detected == "uk":
        return title, True
    try:
        translated = GoogleTranslator(source="auto", target="uk").translate(title)
        if translated and translated.strip() and translated.strip() != title:
            translated = translated.strip()
            db.execute("INSERT OR REPLACE INTO translated_titles(source,ukrainian,translated_at) "
                       "VALUES(?,?,?)", (title, translated, NOW().isoformat()))
            db.commit()
            return translated, True
    except Exception as exc:
        LOG.warning("Headline translation unavailable; original retained: %s", type(exc).__name__)
    return title, False


def setup_bot_profile():
    """Configure the bot description and visible commands without requiring BotFather UI."""
    for method, payload in (
        ("setMyDescription", {"description": BOT_DESCRIPTION, "language_code": "uk"}),
        ("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION, "language_code": "uk"}),
        ("setMyCommands", {"commands": [
            {"command": "now", "description": "Отримати свіжі новини одним повідомленням"},
            {"command": "help", "description": "Довідка та розклад"},
            {"command": "chatid", "description": "Показати Chat ID"},
        ]}),
    ):
        try:
            telegram(method, payload)
        except Exception as exc:
            LOG.warning("Cannot configure Telegram profile %s: %s", method, type(exc).__name__)


def compose_digest(items, local, limit=3900, title_transform=None):
    """Build one categorized HTML Telegram message and IDs of included events."""
    title_transform = title_transform or (lambda title: (title, True))
    header = ("📰 <b>Daily All News</b>\n" + local.strftime("%d.%m.%Y · %H:%M") +
              " · Київ\n<i>Новини з посиланнями на джерела. Повідомлення не є незалежним підтвердженням.</i>")
    if not items:
        return header + "\n\nНових повідомлень поки немає.", []
    blocks, included = [header], []
    last_topic = None
    for item in items:
        original = str(item["title"]).strip()
        title, translated = title_transform(original)
        title = title[:180].strip()
        title = html.escape(title)
        url = str(item["url"] or "")
        if len(url) > 1800 or not url.startswith(("https://", "http://")):
            continue
        topic = str(item["topic"])
        separator = ""
        if topic != last_topic:
            separator = "\n\n" + TOPIC_ICONS.get(topic, "🗞️") + " <b>" + html.escape(topic) + "</b>"
        # Note original language when a translation service is unavailable.
        note = "" if translated else "\n<i>Оригінал: автоматичний переклад недоступний</i>"
        sources = int(item["sources"])
        source_label = "публікація" if sources == 1 else "публікації" if sources in (2, 3, 4) else "публікацій"
        link = '<a href="' + html.escape(url, quote=True) + '">Читати джерело ↗</a>'
        block = (separator + "\n" + str(len(included) + 1) + ". " + title +
                 note + "\n" + str(sources) + " " + source_label + " · " + link)
        # Telegram counts visible text and entities; being conservative also
        # keeps the HTML raw payload below the official 4096-char limit.
        if len("".join(blocks)) + len(block) + 130 > limit:
            break
        blocks.append(block)
        included.append(item["id"])
        last_topic = topic
    remaining = len(items) - len(included)
    if remaining:
        blocks.append("\n\n<i>Інші " + str(remaining) + " подій залишено на наступний огляд.</i>")
    if not included:
        blocks.append("\n\nНовин у форматі короткого огляду поки немає.")
    text = "".join(blocks)
    # Reserve a little room for overflow lines. Enforce one Telegram message.
    return text[:limit], included


def digest(slot: str | None = None):
    if not CHAT_ID:
        raise RuntimeError("Configure TELEGRAM_CHAT_ID in Railway first")
    local = dt.datetime.now(TZ)
    slot = slot or local.strftime("%Y-%m-%d") + ("-AM" if local.hour < 14 else "-PM")
    db = connect()
    try:
        if db.execute("SELECT 1 FROM deliveries WHERE slot=?", (slot,)).fetchone():
            LOG.info("Digest %s already delivered", slot)
            return 0
        items = _candidates(db)
        text, ids = compose_digest(items, local, title_transform=lambda title: translate_title(db, title))
        # Exactly one HTML-formatted message per digest, including manual /now.
        send(text, parse_mode="HTML")
        when = NOW().isoformat()
        if ids:
            db.executemany("UPDATE events SET last_sent=? WHERE id=?",
                           [(when, event_id) for event_id in ids])
        db.execute("INSERT INTO deliveries(slot,sent_at) VALUES(?,?)", (slot, when))
        db.commit()
        LOG.info("Single-message digest %s delivered (%d events)", slot, len(ids))
        return len(ids)
    finally:
        db.close()

def commands():
    # /chatid works for anyone, but never auto-binds an unauthorized user's chat.
    db = connect()
    try:
        offset_row = db.execute("SELECT val FROM state WHERE key='telegram_offset'").fetchone()
        offset = int(offset_row["val"]) if offset_row else 0
        result = telegram("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
        for update in result:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id, cmd = chat.get("id"), (message.get("text") or "").split(" ", 1)[0].split("@")[0]
            if chat_id and cmd in ("/start", "/chatid"):
                send(f"Ваш Telegram Chat ID: {chat_id}\n"
                     "Додайте цей ID до TELEGRAM_CHAT_ID у захищених змінних Railway.",
                     chat_id)
            elif chat_id and str(chat_id) == CHAT_ID and cmd == "/now":
                collect()
                digest("manual-" + str(update["update_id"]))
            elif chat_id and str(chat_id) == CHAT_ID and cmd == "/help":
                send("/now — отримати свіжий дайджест\n"
                     "/chatid — дізнатися Chat ID\n"
                     "Розклад: 08:00 та 20:00 за Києвом.", chat_id)
            offset = update["update_id"] + 1
            db.execute("INSERT INTO state(key,val) VALUES('telegram_offset',?) "
                       "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (str(offset),))
            db.commit()
    finally:
        db.close()

def run():
    if not TOKEN:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN in Railway")
    setup_bot_profile()
    collect()
    scheduler = BlockingScheduler(timezone=TZ)
    scheduler.add_job(collect, "interval", minutes=30, id="collect", coalesce=True, max_instances=1)
    scheduler.add_job(commands, "interval", seconds=20, id="commands", coalesce=True, max_instances=1)
    scheduler.add_job(lambda: (collect(), digest()), "cron", hour=int(os.getenv("MORNING_HOUR", "8")),
                      minute=0, id="morning", misfire_grace_time=3600)
    scheduler.add_job(lambda: (collect(), digest()), "cron", hour=int(os.getenv("EVENING_HOUR", "20")),
                      minute=0, id="evening", misfire_grace_time=3600)
    LOG.info("Daily All News started: %s", TZ)
    scheduler.start()

if __name__ == "__main__":
    import sys
    choice = sys.argv[1] if len(sys.argv) > 1 else "run"
    if choice == "run":
        run()
    elif choice == "collect":
        print(collect())
    elif choice == "chat-id":
        chat_ids()
    elif choice == "once":
        collect()
        print("Sent:", digest("manual-" + str(int(time.time()))))
    elif choice == "test-send":
        send("Daily All News: тестове повідомлення.")
    else:
        raise SystemExit("Commands: run, collect, chat-id, once, test-send")
