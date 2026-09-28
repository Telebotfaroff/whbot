# WHBot

Python Telegram bot for the tested WatchHentai discovery and player flow.

## Setup
python3 -m pip install -r requirements.txt
cp .env.example .env
python3 main.py

## Commands
/start
/latest
/episode <WatchHentai episode URL>

The provider resolves episode metadata, previous/next/all-episodes navigation, player sources and MP4 qualities.
