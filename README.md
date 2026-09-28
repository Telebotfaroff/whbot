# WHBot

Python Telegram bot using **Pyrogram** for Telegram communication and large video uploads.

## Features

- WatchHentai episode metadata and navigation
- 720p/1080p source selection when available
- Streaming download to disk
- Pyrogram video upload
- Upload progress updates
- 2 GiB application-side upload cap
- Local file cleanup after upload

## Setup

Create a Telegram API application to obtain `API_ID` and `API_HASH`, then put those values and your bot token in `.env`.

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
python3 main.py
```

## Environment

```env
API_ID=
API_HASH=
BOT_TOKEN=

WATCHHENTAI_BASE_URL=https://watchhentai.net
DOWNLOAD_DIR=./downloads
```

## Commands

- `/start`
- `/latest`
- `/episode <WatchHentai episode URL>`

The bot downloads the selected MP4 to disk and uploads it with Pyrogram. The local file is removed after the upload attempt.
