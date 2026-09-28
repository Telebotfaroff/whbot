# WHBot

Python Telegram bot using Pyrogram and the WatchHentai provider.

## Local catalog crawler

The catalog uses **local SQLite only**. No Supabase, MongoDB or Redis.

Import pages 1 through 121:

```bash
python3 -m crawler.catalog --pages 121
```

Database:

```
data/watchhentai.db
```

The importer extracts episode URLs, deduplicates them, fetches metadata and stores the catalog locally. It can be stopped and restarted; already imported URLs are skipped.

Run the incremental checker:

```bash
python3 -m crawler.scheduler --interval 1800 --pages 1
```

This checks the newest page every 30 minutes by default and inserts only new posts.

## Bot

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

Environment:

```env
API_ID=
API_HASH=
BOT_TOKEN=
WATCHHENTAI_BASE_URL=https://watchhentai.net
DOWNLOAD_DIR=./downloads
WH_DB_PATH=./data/watchhentai.db
```
