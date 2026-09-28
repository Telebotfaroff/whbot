import argparse
import json
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

    columns = {row[1] for row in con.execute("PRAGMA table_info(posts)").fetchall()}
    if "video_uploaded" not in columns:
        con.execute("ALTER TABLE posts ADD COLUMN video_uploaded INTEGER DEFAULT 0")
    if "video_message_id" not in columns:
        con.execute("ALTER TABLE posts ADD COLUMN video_message_id INTEGER")

    con.execute("""CREATE TABLE IF NOT EXISTS series (
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE NOT NULL,
        name TEXT,
        thumbnail TEXT,
        total_episodes INTEGER,
        episode_urls TEXT,
        scraped_at INTEGER,
        published INTEGER DEFAULT 0,
        telegram_message_id INTEGER
    )""")

    series_columns = {row[1] for row in con.execute("PRAGMA table_info(series)").fetchall()}
    if "episode_urls" not in series_columns:
        con.execute("ALTER TABLE series ADD COLUMN episode_urls TEXT")
    if "published" not in series_columns:
        con.execute("ALTER TABLE series ADD COLUMN published INTEGER DEFAULT 0")
    if "telegram_message_id" not in series_columns:
        con.execute("ALTER TABLE series ADD COLUMN telegram_message_id INTEGER")

    # Remove legacy non-detail series URLs that may have been stored by older crawls.
    # This includes filtered archive URLs and pagination URLs.
    con.execute(
        "DELETE FROM series WHERE url LIKE '%?%' OR url LIKE '%/series/page/%'"
    )

    con.execute("CREATE INDEX IF NOT EXISTS idx_series_url ON series(url)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_posts_url ON posts(url)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_posts_series_episode ON posts(series_url, episode, id)")
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


def save_series(con, series, episode_urls=None):
    episode_urls = episode_urls if episode_urls is not None else series.get("episode_urls", [])
    episode_urls = list(dict.fromkeys(episode_urls))

    con.execute("""INSERT INTO series
    (url,name,thumbnail,total_episodes,episode_urls,scraped_at)
    VALUES (?,?,?,?,?,?)
    ON CONFLICT(url) DO UPDATE SET
    name=excluded.name,
    thumbnail=excluded.thumbnail,
    total_episodes=excluded.total_episodes,
    episode_urls=excluded.episode_urls,
    scraped_at=excluded.scraped_at""",
    (series["series_url"], series["name"], series.get("thumbnail"),
     series.get("total_episodes"), json.dumps(episode_urls, ensure_ascii=False),
     int(time.time())))


def crawl_series(start_page=1, end_page=None, delay=0.25, on_series=None, on_progress=None):
    """
    Crawl inclusive series listing pages.

    Page 1 -> /series/
    Page 2 -> /series/page/2/
    Page 3 -> /series/page/3/
    """
    if end_page is None:
        end_page = start_page
    if start_page < 1 or end_page < start_page:
        raise ValueError("Series crawl pages must start at 1 and end >= start")

    provider = WatchHentai()
    con = init_db()
    processed = 0
    new_ids = []

    try:
        for page in range(start_page, end_page + 1):
            print("[series page {}] discovering series...".format(page), flush=True)

            try:
                items = provider.series_latest(page)
            except Exception as exc:
                print("  ! page failed: {}".format(exc), flush=True)
                continue

            print("  found {} series URLs".format(len(items)), flush=True)
            if on_progress:
                on_progress(page, end_page, processed, len(new_ids), None)

            for item in items:
                url = item["series_url"]
                existing = con.execute(
                    "SELECT id FROM series WHERE url=?", (url,)
                ).fetchone()

                try:
                    series = provider.get_series(url)
                    episode_items = provider.series_episodes(url)
                    episode_urls = [item["page_url"] for item in episode_items]

                    # Keep the series record as the fast catalog, but also
                    # index its episode pages into posts. This makes /latest,
/search and /stats useful after a /crawl without resolving video sources.
                    series["episode_urls"] = episode_urls
                    series["total_episodes"] = (
                        series.get("total_episodes") or len(episode_urls)
                    )

                    save_series(con, series, episode_urls)

                    episode_indexed = 0
                    for episode_item in episode_items:
                        episode_url = episode_item["page_url"]
                        try:
                            # Metadata only: source resolution is intentionally
                            # disabled during crawling to keep the crawl fast.
                            ep = provider.get_episode(episode_url, False)
                            save_episode(con, ep)
                            episode_indexed += 1
                        except Exception as episode_exc:
                            print(
                                "    ! episode index failed {}: {}".format(
                                    episode_url, episode_exc
                                ),
                                flush=True,
                            )

                    con.commit()

                    row = con.execute(
                        "SELECT id FROM series WHERE url=?", (url,)
                    ).fetchone()

                    is_new = bool(row and not existing)
                    if is_new:
                        new_ids.append(row[0])

                    processed += 1
                    print(
                        "  + {} ({} episodes)".format(
                            series["name"], len(episode_urls)
                        ),
                        flush=True,
                    )

                    if on_series:
                        on_series(
                            series,
                            row[0] if row else None,
                            is_new,
                        )
                    if on_progress:
                        on_progress(page, end_page, processed, len(new_ids), series)

                except Exception as exc:
                    con.rollback()
                    print("  ! {}: {}".format(url, exc), flush=True)

                if delay:
                    time.sleep(delay)
    finally:
        con.close()

    print(
        "Processed {} series; {} new".format(processed, len(new_ids)),
        flush=True,
    )
    return {"processed": processed, "new_ids": new_ids}


def crawl(start_page=0, end_page=None, delay=0.25, on_episode=None):
    if end_page is None:
        end_page = start_page
    if start_page < 0 or end_page < start_page:
        raise ValueError("Invalid crawl range")

    provider = WatchHentai()
    con = init_db()
    processed = 0
    new_ids = []

    try:
        for page in range(start_page, end_page + 1):
            print("[page {}] discovering posts...".format(page), flush=True)
            try:
                items = provider.latest(page)
            except Exception as exc:
                print("  ! page failed: {}".format(exc), flush=True)
                continue

            print("  found {} episode URLs".format(len(items)), flush=True)
            for item in items:
                url = item["page_url"]
                existing = con.execute(
                    "SELECT id FROM posts WHERE url=?", (url,)
                ).fetchone()
                try:
                    ep = provider.get_episode(url, False)
                    save_episode(con, ep)
                    con.commit()
                    row = con.execute(
                        "SELECT id FROM posts WHERE url=?", (url,)
                    ).fetchone()
                    if row and not existing:
                        new_ids.append(row[0])
                    processed += 1
                    print("  + {}".format(ep["title"]), flush=True)
                    if on_episode:
                        on_episode(ep, row[0] if row else None, not bool(existing))
                except Exception as exc:
                    con.rollback()
                    print("  ! {}: {}".format(url, exc), flush=True)
                if delay:
                    time.sleep(delay)
    finally:
        con.close()

    print(
        "Processed {} episode(s); {} new".format(processed, len(new_ids)),
        flush=True,
    )
    return {"processed": processed, "new_ids": new_ids}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    crawl_series(args.start, args.end, args.delay)
