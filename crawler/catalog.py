import argparse
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from providers.watchhentai import WatchHentai

load_dotenv()

DB_PATH = Path(os.getenv("WH_DB_PATH", "./data/watchhentai.db"))


def init_db(db=DB_PATH):
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT,
        episode INTEGER, synopsis TEXT, thumbnail TEXT, series_url TEXT,
        previous_url TEXT, next_url TEXT, player_url TEXT,
        quality_720p TEXT, quality_1080p TEXT, scraped_at INTEGER,
        published INTEGER DEFAULT 0, telegram_message_id INTEGER,
        video_uploaded INTEGER DEFAULT 0, video_message_id INTEGER
    )""")

    columns = {
        row[1]
        for row in con.execute("PRAGMA table_info(posts)").fetchall()
    }
    if "video_uploaded" not in columns:
        con.execute(
            "ALTER TABLE posts ADD COLUMN video_uploaded INTEGER DEFAULT 0"
        )
    if "video_message_id" not in columns:
        con.execute(
            "ALTER TABLE posts ADD COLUMN video_message_id INTEGER"
        )

    con.execute("CREATE INDEX IF NOT EXISTS idx_posts_url ON posts(url)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_posts_series_episode "
        "ON posts(series_url, episode, id)"
    )
    con.commit()
    return con


def save_episode(con, ep):
    sources = {s["label"]: s["url"] for s in ep.get("sources", [])}
    nav = ep.get("navigation", {})
    con.execute("""INSERT INTO posts
    (url,title,episode,synopsis,thumbnail,series_url,previous_url,next_url,
     player_url,quality_720p,quality_1080p,scraped_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(url) DO UPDATE SET
    title=excluded.title, episode=excluded.episode, synopsis=excluded.synopsis,
    thumbnail=excluded.thumbnail, series_url=excluded.series_url,
    previous_url=excluded.previous_url, next_url=excluded.next_url,
    player_url=excluded.player_url, quality_720p=excluded.quality_720p,
    quality_1080p=excluded.quality_1080p, scraped_at=excluded.scraped_at""",
    (ep["page_url"], ep["title"], ep.get("episode"), ep.get("synopsis"),
     ep.get("thumbnail"), nav.get("series"), nav.get("previous"),
     nav.get("next"), ep.get("player_url"), sources.get("720p"),
     sources.get("1080p"), int(time.time())))


def crawl(max_page=121, delay=0.25):
    provider = WatchHentai()
    con = init_db()
    total = 0
    try:
        for page in range(1, max_page + 1):
            print("[page {}/{}] discovering posts...".format(page, max_page), flush=True)
            try:
                items = provider.latest(page)
            except Exception as exc:
                print("  ! page failed: {}".format(exc), flush=True)
                continue

            print("  found {} episode URLs".format(len(items)), flush=True)
            for item in items:
                url = item["page_url"]
                if con.execute("SELECT 1 FROM posts WHERE url=?", (url,)).fetchone():
                    continue
                try:
                    ep = provider.get_episode(url, True)
                    save_episode(con, ep)
                    con.commit()
                    total += 1
                    print("  + {}".format(ep["title"]), flush=True)
                except Exception as exc:
                    con.rollback()
                    print("  ! {}: {}".format(url, exc), flush=True)
                if delay:
                    time.sleep(delay)
    finally:
        con.close()
    print("Imported {} new posts into {}".format(total, DB_PATH), flush=True)
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=121)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    crawl(args.pages, args.delay)
