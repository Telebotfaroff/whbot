# WHBot

Python Telegram bot built with **Pyrogram** and the WatchHentai provider.

## Features

- `/start` help
- `/latest` latest episodes
- `/search <query>` title search
- `/episode <URL>` direct episode lookup
- Title, episode, thumbnail and synopsis metadata
- Previous / next / all-episodes navigation
- 720p / 1080p source selection when available
- Direct MP4 downloading without yt-dlp for this provider
- Download progress
- Pyrogram Telegram upload progress
- 2 GiB application-side file-size guard
- Local video automatically deleted only after successful Telegram upload
- Failed uploads keep the local file for inspection/retry

## Setup

Create a Telegram API application and obtain `API_ID` and `API_HASH`. Also create a Telegram bot and obtain `BOT_TOKEN`.

Install:

```bash
python3 -m pip install -r requirements.txt
```

Create `.env`:

```env
API_ID=
API_HASH=
BOT_TOKEN=

WATCHHENTAI_BASE_URL=https://watchhentai.net
DOWNLOAD_DIR=./downloads
```

Run:

```bash
python3 main.py
```

## Flow

```
Telegram
   ↓
Search / episode
   ↓
WatchHentai metadata + player
   ↓
Decode direct MP4 sources
   ↓
Stream MP4 to local disk
   ↓
Pyrogram uploads video
   ↓
Successful upload → delete local file
```

The WatchHentai provider does not require yt-dlp for its tested direct-MP4 player flow.
