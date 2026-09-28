# WHBot

Python Telegram bot using Pyrogram and the WatchHentai provider.

## Railway deployment

The railway-ready branch is intended to run as a single persistent Railway service.

Railway can deploy directly from a GitHub repository and supports custom start commands such as python main.py. See the Railway services documentation.

Railway settings:

    GitHub repository: Telebotfaroff/whbot
    Branch: railway-ready
    Build command: pip install -r requirements.txt
    Start command: python main.py

No public web server is required. The process is a long-running Telegram bot.

## Required variables

Add these in Railway Variables:

    API_ID=your_api_id
    API_HASH=your_api_hash
    BOT_TOKEN=your_bot_token
    CHANNEL_ID=@your_channel

Also add:

    WATCHHENTAI_BASE_URL=https://watchhentai.net
    WH_DB_PATH=/app/data/watchhentai.db
    DOWNLOAD_DIR=/app/data/downloads
    TELEGRAM_CHANNEL_INTERVAL=1.2
    SCHEDULER_ENABLED=true
    CRAWL_INTERVAL=1800
    CRAWL_PAGES=1
    PUBLISH_LIMIT=20
    INITIAL_CRAWL_PAGES=0

## Persistent volume

Railway's normal service filesystem is ephemeral. Data that must survive deployments should be stored on a Railway Volume. Railway documents that application paths are under /app, so mount the volume at /app/data.

Create a Railway Volume and set its Mount Path to:

    /app/data

Then use:

    WH_DB_PATH=/app/data/watchhentai.db
    DOWNLOAD_DIR=/app/data/downloads

The SQLite catalog and downloaded files will then use the persistent volume.

Railway currently lists 0.5 GB volumes for Free/Trial plans and 5 GB for Hobby.

## First deployment

For the first deployment, leave:

    INITIAL_CRAWL_PAGES=0

Deploy the bot first and verify that it starts.

If you want the full historical catalog imported automatically, temporarily set:

    INITIAL_CRAWL_PAGES=121

After the initial import finishes, change it back to:

    INITIAL_CRAWL_PAGES=0

The normal scheduler checks the newest page every 30 minutes and publishes newly discovered entries.

## Telegram publishing

The publisher spaces channel sends by at least 1.2 seconds and handles Telegram HTTP 429 retry_after responses.

It stores the Telegram message ID in SQLite and marks successful posts as published, preventing normal duplicate publishing.

The publisher posts catalog metadata, thumbnail and source page link. It does not automatically upload source video files to the channel.

## Bot commands

    /start
    /help
    /latest
    /search <query>
    /episode <URL>
    /stats
    /crawl
    /publish

## Local development

    python3 -m pip install -r requirements.txt
    python3 main.py

Local paths can be overridden with:

    WH_DB_PATH=./data/watchhentai.db
    DOWNLOAD_DIR=./downloads