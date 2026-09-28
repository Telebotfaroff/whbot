# WHBot

Python Telegram bot using Pyrogram and the WatchHentai provider.

## 🚀 Google Colab

This branch is designed for **Google Colab**.

### Open in Google Colab

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Telebotfaroff/whbot/blob/recovered-colab/WHBot_Colab.ipynb)

The notebook is designed to:
1. Mount Google Drive.
2. Clone this branch.
3. Install dependencies.
4. Create persistent folders.
5. Load the complete environment configuration from **one `.env` block**.
6. Start WHBot.

> **Important:** Colab runtimes are temporary. Google Drive keeps the SQLite database and downloads, but the bot stops when the Colab runtime disconnects.

## 🔐 Environment variables

You can paste the **entire `.env` file at once** when the notebook asks for it. Do not enter each variable separately.

Use this template:

```env
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token

WATCHHENTAI_BASE_URL=https://watchhentai.net

DOWNLOAD_DIR=/content/drive/MyDrive/whbot/downloads
WH_DB_PATH=/content/drive/MyDrive/whbot/data/watchhentai.db

CHANNEL_ID=your_channel_id
TELEGRAM_CHANNEL_INTERVAL=1.2

SCHEDULER_ENABLED=true
CRAWL_INTERVAL=1800
CRAWL_PAGES=1
PUBLISH_LIMIT=20
INITIAL_CRAWL_PAGES=0
```

The notebook writes that complete block to `.env`, so there is **no need to manually create the file or enter variables one by one**.

### Initial catalog import

Start with:

```env
INITIAL_CRAWL_PAGES=0
```

After confirming the bot works, set:

```env
INITIAL_CRAWL_PAGES=121
```

to perform the historical crawl. After it completes, set it back to `0`.

## 💾 Google Drive persistence

```text
/content/drive/MyDrive/whbot/
├── data/
│   └── watchhentai.db
└── downloads/
```

This keeps the SQLite catalog and downloaded files across Colab runtime restarts.

## ⚙️ Colab setup

If running manually:

```bash
git clone -b recovered-colab https://github.com/Telebotfaroff/whbot.git
cd whbot
pip install -r requirements.txt
python main.py
```

For the easiest setup, use the **Open in Colab** button above.

## 🤖 Bot commands

```text
/start
/help
/latest
/search <query>
/episode <URL>
/stats
/crawl
/publish
```

## 🤖 Sequential video uploader\n\nUse `/auto` to process pending videos one at a time. Episodes are ordered by series and episode number, so one series is completed before the next series starts.\n\n```text\n/auto\n/auto 720p\n/auto stop\n```\n\nThe uploader downloads the selected/highest available quality, prepares the episode thumbnail, reads video duration/dimensions when `ffprobe` is available, uploads the video to the configured Telegram channel, and records the Telegram message ID in SQLite. Already uploaded episodes are skipped.\n\n## 📢 Telegram publishing

The publisher:
- Spaces channel messages using `TELEGRAM_CHANNEL_INTERVAL`.
- Handles Telegram `429 retry_after` responses.
- Stores Telegram message IDs in SQLite.
- Marks successfully published posts to avoid normal duplicate publishing.
- Publishes catalog metadata, thumbnail and source page link.

## 🧪 Local development

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

For local paths:

```env
WH_DB_PATH=./data/watchhentai.db
DOWNLOAD_DIR=./downloads
```
