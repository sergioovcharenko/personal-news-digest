"""Personal RSS news digest delivered by Telegram at 08:00 and 20:00 Kyiv time."""
import datetime as dt
import difflib
import html
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("news")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
DB_PATH = Path(os.getenv("DB_PATH", "./news.db"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_DIGEST", "12"))
HEADERS = {"User-Agent": "PersonalNewsDigest/0.1 (+RSS; contact via repository)"}
QUERIES = {
    "БпЛА та робототехніка": 'drone robotics UAV',
    "Штучний інтелект": 'artificial intelligence AI',
    "Україна та світ": 'Ukraine world news',
    "Військові технології": 'defense technology military technology',
    "Кібербезпека": 'cybersecurity cyber attack',
    "Технології та електроніка": 'technology electronics semiconductors',
    "Війна": 'Ukraine war',
    "Політика": 'Ukraine international politics',
}
# Google News RSS returns publisher links and localized summaries.
FEEDS = {
    topic: "https://news.google.com/rss/search?q="
    + quote(query + " when:1d")
    + "&hl=uk&gl=UA&ceid=UA:uk"
    for topic, query in QUERIES.items()
}
FEEDS["Україна та світ (Суспільне)"] = "https://suspilne.media/rss/"

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, normalized TEXT NOT NULL,
        topic TEXT NOT NULL, url TEXT NOT NULL UNIQUE, publisher TEXT,
        discovered TEXT NOT NULL, sent INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS deliveries(
        slot TEXT PRIMARY KEY, completed TEXT NOT NULL)""")
    conn.commit()
    return conn

def normalize(title):
    title = re.sub(r"\\s+[-–—|]\\s+[^-–—|]{3,55}$", "", title)
    title = re.sub(r"[^\\w\\s]", " ", title.casefold())
    return " ".join(title.split())

def near_duplicate(conn, norm):
    if not norm:
        return True
    rows = conn.execute("SELECT normalized FROM articles ORDER BY id DESC LIMIT 900").fetchall()
    return any(
        norm == r["normalized"]
        or (len(norm) > 24 and difflib.SequenceMatcher(None, norm, r["normalized"]).ratio() >= 0.87)
        for r in rows
    )

def collect():
    conn = connect()
    added = 0
    try:
        for topic, url in FEEDS.items():
            try:
                response = requests.get(url, headers=HEADERS, timeout=20)
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                for item in parsed.entries[:35]:
                    title = html.unescape(re.sub("<[^>]+>", "", item.get("title", ""))).strip()
                    link = item.get("link", "").strip()
                    if not title or not link:
                        continue
                    # Source priority is not a claim of independent verification.
                    norm = normalize(title)
                    if near_duplicate(conn, norm):
                        continue
                    publisher = item.get("source", {}).get("title", "") if isinstance(item.get("source", {}), dict) else ""
                    conn.execute(
                        "INSERT OR IGNORE INTO articles(title,normalized,topic,url,publisher,discovered) VALUES(?,?,?,?,?,?)",
                        (title, norm, topic, link, publisher, dt.datetime.now(dt.timezone.utc).isoformat())
                    )
                    added += 1
                conn.commit()
            except Exception:
                LOG.exception("Feed failed: %s", topic)
    finally:
        conn.close()
    LOG.info("Collected %s new articles", added)
    return added

def telegram(method, data):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    resp = requests.post("https://api.telegram.org/bot" + TOKEN + "/" + method, json=data, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram delivery error: " + str(result.get("description")))
    return result["result"]

def discover_chat():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN")
    response = requests.get("https://api.telegram.org/bot" + TOKEN + "/getUpdates", timeout=30)
    response.raise_for_status()
    updates = response.json().get("result", [])
    matches = [(u.get("message") or {}).get("chat", {}) for u in updates]
    ids = [(c.get("id"), c.get("type")) for c in matches if c.get("id")]
    print("Recent chat IDs and types:", ids or "None: send /start to your bot first")

def digest(slot=None):
    if not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_CHAT_ID after sending /start")
    now = dt.datetime.now(TZ)
    slot = slot or (now.strftime("%Y-%m-%d") + "-" + ("morning" if now.hour < 14 else "evening"))
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM deliveries WHERE slot=?", (slot,)).fetchone():
            LOG.info("Already delivered %s", slot)
            return
        articles = conn.execute(
            "SELECT * FROM articles WHERE sent=0 ORDER BY id DESC LIMIT ?", (MAX_ITEMS,)
        ).fetchall()
        header = "Новинний дайджест · " + now.strftime("%d.%m.%Y %H:%M") + " (Київ)"
        if articles:
            lines = [header, "", "Огляд джерел; повідомлення не означають незалежне підтвердження."]
            for i, article in enumerate(articles, 1):
                lines.append(
                    "\n" + str(i) + ". [" + article["topic"] + "] "
                    + article["title"] + "\n" + article["url"]
                )
            message = "\n".join(lines)
        else:
            message = header + "\n\nНових повідомлень за вибраними темами поки немає."
        # Telegram message limit: split at article boundaries if needed.
        chunks = []
        current = ""
        for paragraph in message.split("\n\n"):
            candidate = (current + "\n\n" + paragraph) if current else paragraph
            if len(candidate) > 3900 and current:
                chunks.append(current)
                current = paragraph
            else:
                current = candidate
        if current:
            chunks.append(current)
        for part in chunks:
            telegram("sendMessage", {"chat_id": CHAT_ID, "text": part[:3900], "disable_web_page_preview": True})
        for article in articles:
            conn.execute("UPDATE articles SET sent=1 WHERE id=?", (article["id"],))
        conn.execute("INSERT INTO deliveries(slot,completed) VALUES(?,?)", (slot, now.isoformat()))
        conn.commit()
        LOG.info("Delivered %s (%s articles)", slot, len(articles))
    finally:
        conn.close()

def scheduled_digest(name):
    collect()
    digest(dt.datetime.now(TZ).strftime("%Y-%m-%d") + "-" + name)

def run():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in hosting variables")
    collect()
    scheduler = BlockingScheduler(timezone=TZ)
    scheduler.add_job(collect, "interval", minutes=30, id="collect", max_instances=1, coalesce=True)
    scheduler.add_job(
        scheduled_digest, "cron", hour=int(os.getenv("MORNING_HOUR", "8")), minute=0,
        args=["morning"], id="morning", max_instances=1, misfire_grace_time=3600
    )
    scheduler.add_job(
        scheduled_digest, "cron", hour=int(os.getenv("EVENING_HOUR", "20")), minute=0,
        args=["evening"], id="evening", max_instances=1, misfire_grace_time=3600
    )
    LOG.info("Started for timezone %s", TZ)
    scheduler.start()

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "chat-id":
        discover_chat()
    elif command == "test-send":
        telegram("sendMessage", {"chat_id": CHAT_ID, "text": "Тест: агрегатор новин підключено."})
    elif command == "collect":
        collect()
    elif command == "once":
        collect()
        digest("manual-" + str(int(time.time())))
    elif command == "run":
        run()
    else:
        raise SystemExit("Use: run | collect | once | test-send | chat-id")
