import html
import os
import sqlite3
import time
import requests
from crawler.catalog import DB_PATH

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MIN_INTERVAL = float(os.getenv("TELEGRAM_CHANNEL_INTERVAL", "1.2"))

class TelegramPublisher:
    def __init__(self):
        if not BOT_TOKEN or not CHANNEL_ID:
            raise RuntimeError("BOT_TOKEN and CHANNEL_ID are required")
        self.base = "https://api.telegram.org/bot" + BOT_TOKEN
        self.last_send = 0.0
        self.session = requests.Session()

    def _wait(self):
        delay = MIN_INTERVAL - (time.monotonic() - self.last_send)
        if delay > 0:
            time.sleep(delay)

    def _call(self, method, data):
        while True:
            self._wait()
            r = self.session.post(self.base + "/" + method, data=data, timeout=(20, 60))
            self.last_send = time.monotonic()
            if r.status_code == 429:
                body = r.json()
                retry = int(body.get("parameters", {}).get("retry_after", 5))
                print("[telegram] FloodWait/429; sleeping {}s".format(retry))
                time.sleep(retry + 1)
                continue
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(body.get("description", "Telegram API error"))
            return body["result"]

    def publish_row(self, row):
        _, url, title, episode, synopsis, thumbnail, series_url, *_rest = row
        q720 = row[10]
        q1080 = row[11]
        lines = ["🎬 <b>{}</b>".format(html.escape(title or "Untitled"))]
        if episode is not None:
            lines.append("📺 Episode: <b>{}</b>".format(episode))
        if synopsis:
            text = synopsis.strip()
            if len(text) > 700:
                text = text[:697] + "..."
            lines += ["", "📝 <b>Synopsis</b>", html.escape(text)]
        qualities = [q for q, value in (("1080p", q1080), ("720p", q720)) if value]
        if qualities:
            lines += ["", "🎥 <b>Quality:</b> " + ", ".join(qualities)]
        lines += ["", "🔗 <a href="{}">Open post</a>".format(html.escape(url, quote=True))]
        if series_url:
            lines.append("📋 <a href="{}">All episodes</a>".format(html.escape(series_url, quote=True)))
        caption = "\n".join(lines)
        if thumbnail:
            try:
                result = self._call("sendPhoto", {
                    "chat_id": CHANNEL_ID, "photo": thumbnail,
                    "caption": caption, "parse_mode": "HTML"
                })
                return int(result["message_id"])
            except Exception as exc:
                print("[telegram] thumbnail failed: {}".format(exc))
        result = self._call("sendMessage", {
            "chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML",
            "disable_web_page_preview": False
        })
        return int(result["message_id"])

def publish_pending(limit=20):
    publisher = TelegramPublisher()
    con = sqlite3.connect(DB_PATH)
    count = 0
    try:
        rows = con.execute(
            """SELECT id,url,title,episode,synopsis,thumbnail,series_url,previous_url,
            next_url,player_url,quality_720p,quality_1080p,scraped_at,published,telegram_message_id
            FROM posts WHERE published=0 ORDER BY id ASC LIMIT ?""", (limit,)
        ).fetchall()
        for row in rows:
            try:
                message_id = publisher.publish_row(row)
                con.execute("UPDATE posts SET published=1, telegram_message_id=? WHERE id=?",
                            (message_id, row[0]))
                con.commit()
                count += 1
                print("[published] {} -> {}".format(row[2], message_id))
            except Exception as exc:
                print("[publish failed] {}: {}".format(row[2], exc))
                break
    finally:
        con.close()
    return count
