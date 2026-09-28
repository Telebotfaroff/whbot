# WHBot

Telegram bot for discovering WatchHentai episodes, resolving the tested player sources, downloading MP4 files with the required request context, and sending them to Telegram when they fit the configured upload limit.

## Setup
1. Copy .env.example to .env.
2. Set BOT_TOKEN.
3. npm install
4. npm start

## Commands
/latest
/episode <WatchHentai episode URL>

The episode UI exposes Previous, All Episodes, Next when the source page provides them, plus available quality buttons.

MAX_UPLOAD_MB defaults to 50. Larger files are downloaded but not uploaded through the standard Bot API.
