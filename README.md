# WHBot

Python Telegram bot for the tested WatchHentai discovery and player flow.

## Flow

Homepage -> episode URLs -> episode metadata -> primary player -> whJwSources -> decoded MP4 sources -> streamed download -> Telegram.

## Setup

Python 3.10+ recommended.

    python3 -m pip install -r requirements.txt
    cp .env.example .env

Set BOT_TOKEN in .env, then:

    python3 main.py

## Commands

- /start
- /latest
- /episode <WatchHentai episode URL>

Episode buttons expose Previous, All Episodes and Next when the source page provides them. Quality buttons resolve/download the selected source.

MAX_UPLOAD_MB defaults to 50. Larger downloads are kept locally and reported instead of claiming Telegram upload succeeded.

## Notes

The provider uses the same request context that was verified during development: User-Agent + WatchHentai Referer. Source URLs are resolved fresh rather than cached as permanent media URLs.
