# MONOLIT NEWS AI v0.2

Personal Ukrainian-language Telegram news digest, collecting news about:
- БпЛА та робототехніка
- Штучний інтелект
- Україна та світ
- Військові технології
- Кібербезпека
- Технології та електроніка
- Війна
- Політика

## Implemented

- Google News RSS (Ukrainian locale) and independent BBC, TechCrunch and BleepingComputer RSS.
- Feed refresh every 30 minutes; event clustering using multilingual title normalization, token/character similarity and removal of tracking URL parameters.
- Balanced selection across configured subjects rather than allowing a single topic to dominate.
- Persistent SQLite source history and restart-safe per-event delivery tracking.
- Telegram delivery at **08:00 and 20:00 Europe/Kyiv** including daylight saving time.
- Bot commands: `/chatid` or `/start` to get your chat ID, `/now` to request a digest, `/help`.
- `python -m unittest discover -s tests -v` for offline tests; GitHub Actions CI.

This is not a claim of exhaustive world-news coverage. Similar headlines may still refer to different events or translated duplicates may not match. No AI fact-checking, Telegram channel reading, image analysis, or content licensing beyond source links is implemented yet.

## Connect Telegram securely

1. Create a Telegram bot through [@BotFather](https://t.me/BotFather) (already done if continuing previous setup).
2. In Railway project `personal-news-digest`, add the **secret** variable `TELEGRAM_BOT_TOKEN`. Do not put it in GitHub files, issues or chat.
3. Send `/start` to your Telegram bot. Locally run `python main.py chat-id` after setting your bot token, or deploy the worker (with token only) and send `/chatid` to get your chat ID in Telegram.
4. Add your numeric `TELEGRAM_CHAT_ID` as another Railway variable. Once both variables exist, Railway will run the scheduled worker and deliver digests only to that chat.
5. With both configured, run `python main.py test-send` or send `/now` to your bot.

## Railway deployment

You need one available service slot in your Railway workspace. The previous eBay project's removal was staged but requires **your two-factor verification** in Railway. Do not remove its PostgreSQL service or data volume before exporting a database backup.

Deploy from `sergioovcharenko/personal-news-digest` (branch `main`). The repository includes `railway.json` with `python main.py run` as the start command.

Set environment variables:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Secret supplied by BotFather |
| `TELEGRAM_CHAT_ID` | Telegram numeric Chat ID |
| `TIMEZONE` | `Europe/Kyiv` |
| `MORNING_HOUR` | `8` |
| `EVENING_HOUR` | `20` |
| `MAX_ITEMS_PER_DIGEST` | `16` |
| `DB_PATH` | `/data/news.db` |

Attach a persistent Railway volume mounted at `/data` to retain article history and deduplication after restart. Do not run multiple worker replicas, and do not enable auto-sleep if you want reliable scheduled delivery.

## Local usage

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# Edit the private .env locally with your BotFather token and chat ID.
python main.py collect
python main.py chat-id
python main.py test-send
python main.py once
python main.py run
```

The `.env` file is in `.gitignore` and must never be committed. Use Railway Variables for hosted deployments.

## Future upgrades

- Authorized channel ingestion via Telegram API (public or permitted private channels; not available using only a BotFather token).
- Optional semantic embeddings for multilingual deduplication and AI summaries, gated by human-readable source references.
- Topic controls and source allow/block lists in the Telegram bot and a private web interface.
