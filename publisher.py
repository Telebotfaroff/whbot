import html
import json
import os
import sqlite3
import time

import requests
from dotenv import load_dotenv

from crawler.catalog import DB_PATH

load_dotenv()

MIN_INTERVAL = float(os.getenv("TELEGRAM_CHANNEL_INTERVAL", "1.2"))


class TelegramPublisher:
    def __init__(self):
        bot_token = os.getenv("BOT_TOKEN")
        channel_id = os.getenv("CHANNEL_ID")
        if not bot_token or not channel_id:
            raise RuntimeError("BOT_TOKEN and CHANNEL_ID are required")
        self.base = "https://api.telegram.org/bot" + bot_token
        self.channel_id = channel_id
        self.last_send = 0.0
        self.session = requests.Session()

    def _wait(self):
        delay = MIN_INTERVAL - (time.monotonic() - self.last_send)
        if delay > 0:
            time.sleep(delay)

    def _call(self, method, data):
        while True:
            self._wait()
            r = self.session.post(
                self.base + "/" + method,
                data=data,
                timeout=(20, 60),
            )
            self.last_send = time.monotonic()

            if r.status_code == 429:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                retry = int(body.get("parameters", {}).get("retry_after", 5))
                print("[telegram] 429; sleeping {}s".format(retry), flush=True)
                time.sleep(retry + 1)
                continue

            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(body.get("description", "Telegram API error"))
            return body["result"]

    def publish_series_row(self, row):
        (
            series_id,
            series_url,
            name,
            thumbnail,
            total_episodes,
            episode_urls_json,
            scraped_at,
            published,
            telegram_message_id,
        ) = row

        try:
            episode_urls = json.loads(episode_urls_json or "[]")
        except (TypeError, ValueError):
            episode_urls = []

        lines = [
            "🎬 <b>{}</b>".format(html.escape(name or "Untitled")),
            "📚 Episodes: <b>{}</b>".format(total_episodes or len(episode_urls)),
            '🔗 Series: <a href="{}">Open Series</a>'.format(
                html.escape(series_url, quote=True)
            ),
            "",
            "<b>Episodes</b>",
        ]

        for index, episode_url in enumerate(episode_urls, 1):
            lines.append(
                '🔹 <a href="{}">Episode {}</a>'.format(
                    html.escape(episode_url, quote=True),
                    index,
                )
            )

        full_text = "\n".join(lines)

        # Telegram photo captions are limited to 1024 characters. Keep the
        # complete episode list by sending it as follow-up text chunks when
        # the full post is too large.
        if len(full_text) <= 1024:
            caption = full_text
            if thumbnail:
                try:
                    result = self._call(
                        "sendPhoto",
                        {
                            "chat_id": self.channel_id,
                            "photo": thumbnail,
                            "caption": caption,
                            "parse_mode": "HTML",
                        },
                    )
                    return int(result["message_id"])
                except Exception as exc:
                    print("[telegram] series thumbnail failed: {}".format(exc), flush=True)

            result = self._call(
                "sendMessage",
                {
                    "chat_id": self.channel_id,
                    "text": full_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            return int(result["message_id"])

        header = "\n".join(lines[:4])
        if thumbnail:
            try:
                result = self._call(
                    "sendPhoto",
                    {
                        "chat_id": self.channel_id,
                        "photo": thumbnail,
                        "caption": header,
                        "parse_mode": "HTML",
                    },
                )
            except Exception as exc:
                print("[telegram] series thumbnail failed: {}".format(exc), flush=True)
                result = self._call(
                    "sendMessage",
                    {
                        "chat_id": self.channel_id,
                        "text": header,
                        "parse_mode": "HTML",
                    },
                )
        else:
            result = self._call(
                "sendMessage",
                {
                    "chat_id": self.channel_id,
                    "text": header,
                    "parse_mode": "HTML",
                },
            )

        first_message_id = int(result["message_id"])
        chunks = []
        current = "<b>Episodes</b>\n"

        for index, episode_url in enumerate(episode_urls, 1):
            line = '🔹 <a href="{}">Episode {}</a>\n'.format(
                html.escape(episode_url, quote=True),
                index,
            )
            if len(current) + len(line) > 4096:
                chunks.append(current.rstrip())
                current = line
            else:
                current += line

        if current.strip():
            chunks.append(current.rstrip())

        for chunk in chunks:
            self._call(
                "sendMessage",
                {
                    "chat_id": self.channel_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

        return first_message_id

    def publish_row(self, row):
        _, url, title, episode, synopsis, thumbnail, series_url, *_rest = row
        total_episodes = 0

        if series_url:
            with sqlite3.connect(DB_PATH, timeout=30) as count_con:
                total_episodes = count_con.execute(
                    "SELECT COUNT(*) FROM posts WHERE series_url=?",
                    (series_url,),
                ).fetchone()[0]

        lines = [
            "🎬 <b>{}</b>".format(html.escape(title or "Untitled")),
        ]
        if episode is not None:
            lines.append("📺 Episode: <b>{}</b>".format(episode))
        if total_episodes:
            lines.append("📚 Total Episodes: <b>{}</b>".format(total_episodes))

        if series_url:
            lines.append(
                '📋 <a href="{}">All Episodes</a>'.format(
                    html.escape(series_url, quote=True)
                )
            )

        lines.append(
            '🔗 <a href="{}">View Episode</a>'.format(
                html.escape(url, quote=True)
            )
        )

        caption = "\n".join(lines)

        if thumbnail:
            try:
                result = self._call(
                    "sendPhoto",
                    {
                        "chat_id": self.channel_id,
                        "photo": thumbnail,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                )
                return int(result["message_id"])
            except Exception as exc:
                print(
                    "[telegram] thumbnail failed: {}".format(exc),
                    flush=True,
                )

        result = self._call(
            "sendMessage",
            {
                "chat_id": self.channel_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        return int(result["message_id"])


def _mark_published(con, post_id, message_id):
    con.execute(
        "UPDATE posts SET published=1, telegram_message_id=? WHERE id=?",
        (message_id, post_id),
    )
    con.commit()


def publish_ids(post_ids):
    publisher = TelegramPublisher()
    con = sqlite3.connect(DB_PATH, timeout=30)
    count = 0

    try:
        for post_id in post_ids:
            row = con.execute(
                """SELECT id,url,title,episode,synopsis,thumbnail,series_url,
                previous_url,next_url,player_url,quality_720p,quality_1080p,
                scraped_at,published,telegram_message_id
                FROM posts WHERE id=?""",
                (post_id,),
            ).fetchone()

            if not row or row[13]:
                continue

            try:
                message_id = publisher.publish_row(row)
                _mark_published(con, post_id, message_id)
                count += 1
                print(
                    "[published] {} -> {}".format(row[2], message_id),
                    flush=True,
                )
            except Exception as exc:
                print(
                    "[publish failed] {}: {}".format(row[2], exc),
                    flush=True,
                )
    finally:
        con.close()

    return count


def publish_pending(limit=20):
    publisher = TelegramPublisher()
    con = sqlite3.connect(DB_PATH, timeout=30)
    count = 0

    try:
        rows = con.execute(
            """SELECT id,url,title,episode,synopsis,thumbnail,series_url,previous_url,
            next_url,player_url,quality_720p,quality_1080p,scraped_at,published,telegram_message_id
            FROM posts WHERE published=0 ORDER BY id ASC LIMIT ?""",
            (limit,),
        ).fetchall()

        for row in rows:
            try:
                message_id = publisher.publish_row(row)
                _mark_published(con, row[0], message_id)
                count += 1
                print("[published] {} -> {}".format(row[2], message_id), flush=True)
            except Exception as exc:
                print("[publish failed] {}: {}".format(row[2], exc), flush=True)
                break
    finally:
        con.close()

    return count


def _mark_series_published(con, series_id, message_id):
    con.execute(
        "UPDATE series SET published=1, telegram_message_id=? WHERE id=?",
        (message_id, series_id),
    )
    con.commit()


def publish_series_ids(series_ids):
    publisher = TelegramPublisher()
    con = sqlite3.connect(DB_PATH, timeout=30)
    count = 0

    try:
        for series_id in series_ids:
            row = con.execute(
                """SELECT id,url,name,thumbnail,total_episodes,episode_urls,
                   scraped_at,published,telegram_message_id
                   FROM series WHERE id=?""",
                (series_id,),
            ).fetchone()

            if not row or row[7]:
                continue

            try:
                message_id = publisher.publish_series_row(row)
                _mark_series_published(con, series_id, message_id)
                count += 1
                print(
                    "[series published] {} -> {}".format(row[2], message_id),
                    flush=True,
                )
            except Exception as exc:
                print(
                    "[series publish failed] {}: {}".format(row[2], exc),
                    flush=True,
                )
                break
    finally:
        con.close()

    return count


def publish_pending_series(limit=20):
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = con.execute(
            """SELECT id,url,name,thumbnail,total_episodes,episode_urls,
               scraped_at,published,telegram_message_id
               FROM series WHERE published=0 ORDER BY id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return publish_series_ids([row[0] for row in rows])
    finally:
        con.close()
