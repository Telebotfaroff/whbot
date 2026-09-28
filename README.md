# WHBot

Python Telegram bot using Pyrogram and the WatchHentai provider.

## Render deployment

The `render-ready` branch is configured as a Render Web Service.

Render settings:

    Build Command: pip install -r requirements.txt
    Start Command: python main.py
    Health Check Path: /health

`main.py` starts a small HTTP health server on Render's `PORT` while Pyrogram runs the Telegram bot. The catalog checker and Telegram channel publisher run in the same service as a background thread, so a separate worker service is not required.

### Render environment variables

Required:

    API_ID=
    API_HASH=
    BOT_TOKEN=
    CHANNEL_ID=@your_channel

Recommended:

    WATCHHENTAI_BASE_URL=https://watchhentai.net
    WH_DB_PATH=./data/watchhentai.db
    DOWNLOAD_DIR=./downloads
    TELEGRAM_CHANNEL_INTERVAL=1.2
    SCHEDULER_ENABLED=true
    CRAWL_INTERVAL=1800
    CRAWL_PAGES=1
    PUBLISH_LIMIT=20
    INITIAL_CRAWL_PAGES=0

`INITIAL_CRAWL_PAGES=0` avoids a large crawl during every restart. For the first deployment, set it to `121` if you want the full catalog imported in the background, then change it back to `0` after the initial import.

The normal background checker scans the newest page every 30 minutes and publishes newly discovered catalog entries.

## Persistent SQLite storage on Render

Render's normal service filesystem is ephemeral. If the catalog must survive redeploys/restarts, attach a Render Persistent Disk and set:

    WH_DB_PATH=/data/watchhentai.db
    DOWNLOAD_DIR=/data/downloads

The free Render instance does not provide persistent-disk storage, so a free deployment can lose its local SQLite catalog when the service is replaced.

The bot does not require a public website; `/health` only exists to satisfy Render's Web Service health requirement.

## Local catalog crawler

The catalog uses local SQLite only. No Supabase, MongoDB or Redis.

Import pages 1 through 121:

    python3 -m crawler.catalog --pages 121

Database:

    data/watchhentai.db

The importer extracts episode URLs, deduplicates them, fetches metadata and stores the catalog locally. It can be stopped and restarted; already imported URLs are skipped.

Run the incremental checker manually:

    python3 -m crawler.scheduler --interval 1800 --pages 1

## Bot

    python3 -m pip install -r requirements.txt
    python3 main.py

Commands:

    /start
    /help
    /latest
    /search <query>
    /episode <URL>
    /stats
    /crawl
    /publish

## Telegram channel publishing

Set:

    CHANNEL_ID=@your_channel
    TELEGRAM_CHANNEL_INTERVAL=1.2

The scheduler automatically publishes unpublished catalog entries. It stores the Telegram message ID in SQLite so published posts are not sent again.

Manual publishing:

    /publish

The publisher spaces channel sends by at least 1.2 seconds and handles Telegram HTTP 429 `retry_after` responses before retrying. It posts catalog metadata/thumbnail and the source page link; it does not automatically upload source video files to the channel.