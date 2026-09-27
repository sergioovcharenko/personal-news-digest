# MONOLIT News — Telegram news digest

A personal Ukrainian-language news digest for these topics: drones and robotics, AI, Ukraine and world news, military technology, cybersecurity, technology and electronics, war, and politics.

**Current status:** RSS/Google News collection, basic duplicate-title filtering, SQLite history, Telegram delivery and scheduling are implemented. This version **does not yet read Telegram channels**, verify claims automatically, or use AI embeddings. Google News/RSS do not represent every news source worldwide.

## Install and test

Requires Python 3.11+. Run:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

Create your Telegram bot with [@BotFather](https://t.me/BotFather) and save its token in your *local* `.env`. Never commit the actual `.env` to GitHub, publish the token, or paste it into chat.

Send `/start` to your bot in Telegram. Then run `python main.py chat-id` to see your chat ID, which goes in `TELEGRAM_CHAT_ID` in `.env`.

```bash
python main.py test-send
python main.py once
python main.py run
```

The default schedule is 08:00 and 20:00 in the `Europe/Kyiv` timezone. RSS collection occurs every 30 minutes. Adjust the schedule with `MORNING_HOUR` and `EVENING_HOUR`.

## Railway

1. Make this repository **private** using GitHub Settings > General > Danger Zone > Change repository visibility.
2. In Railway, create a new project from this GitHub repository and add a worker service. Railway free-tier resource limits may prevent creating a project until resources are freed or the plan is upgraded. Do not remove existing projects unless you intend to.
3. In Railway Variables, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; optionally set `TIMEZONE=Europe/Kyiv`, `MORNING_HOUR=8`, `EVENING_HOUR=20`, and `MAX_ITEMS_PER_DIGEST=12`.
4. Start command: `python main.py run`. Attach a persistent volume mounted at `/data`; set `DB_PATH=/data/news.db`.
5. Use one running worker instance. Do not turn on sleep/scale-to-zero for continuous collection.

## Notes

Near-duplicate filtering compares normalized Ukrainian/English headlines. Significantly paraphrased or translated duplicates may pass. Links are included so you can inspect original reporting. Political coverage is descriptive and should include multiple sources rather than inferring any editorial endorsement. A follow-up version can add authorized Telegram channel reading and better multilingual event clustering.

**Secrets:** a Telegram bot token grants control over the bot. Never place it in a public repository or commit history. For Telegram-channel access, a separate authorized account/API configuration is required; the bot token alone cannot read arbitrary channels.
