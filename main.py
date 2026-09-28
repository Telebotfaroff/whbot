import asyncio
import html
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from providers.watchhentai import WatchHentai, ProviderError
from crawler.catalog import DB_PATH, init_db
from publisher import publish_pending, publish_ids

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PYROGRAM_WORKDIR = Path(os.getenv("PYROGRAM_WORKDIR", "./.pyrogram"))
PYROGRAM_WORKDIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CALLBACK_URLS = {}
AUTO_LOCK = asyncio.Lock()
AUTO_STOP = threading.Event()
CRAWL_STATES = {}

app = Client(
    "whbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(PYROGRAM_WORKDIR),
    parse_mode=ParseMode.HTML,
)
provider = WatchHentai()

# Background crawling is intentionally OFF by default.
CRAWL_INTERVAL = max(int(os.getenv("CRAWL_INTERVAL", "1800")), 60)
CRAWL_PAGES = max(int(os.getenv("CRAWL_PAGES", "1")), 1)
PUBLISH_LIMIT = max(int(os.getenv("PUBLISH_LIMIT", "20")), 1)
INITIAL_CRAWL_PAGES = max(int(os.getenv("INITIAL_CRAWL_PAGES", "0")), 0)
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "false").lower() not in {"0", "false", "no", "off"}
BACKGROUND_LOCK = threading.Lock()


def enc(value):
    import hashlib
    token = hashlib.sha256(value.encode()).hexdigest()[:12]
    CALLBACK_URLS[token] = value
    return token


def dec(value):
    if value not in CALLBACK_URLS:
        raise RuntimeError("This button has expired. Open the episode again.")
    return CALLBACK_URLS[value]


def format_duration(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "Unknown"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def video_metadata(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"duration": 0, "width": 0, "height": 0}

    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        return {
            "duration": int(float(fmt.get("duration") or 0)),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
        }
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}


def db_rows(query, params=()):
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


def mark_video_uploaded(post_id, message_id):
    con = init_db()
    try:
        con.execute(
            "UPDATE posts SET video_uploaded=1, video_message_id=? WHERE id=?",
            (message_id, post_id),
        )
        con.commit()
    finally:
        con.close()


def episode_keyboard(ep):
    rows = []
    navigation = ep["navigation"]

    view = [InlineKeyboardButton("▶️ View Episode", url=ep["page_url"])]
    if navigation.get("series"):
        view.append(InlineKeyboardButton("📋 All Episodes", url=navigation["series"]))
    rows.append(view)

    nav = []
    if navigation.get("previous"):
        nav.append(
            InlineKeyboardButton(
                "⬅ Previous",
                callback_data="ep:" + enc(navigation["previous"]),
            )
        )
    if navigation.get("next"):
        nav.append(
            InlineKeyboardButton(
                "Next ➡",
                callback_data="ep:" + enc(navigation["next"]),
            )
        )
    if nav:
        rows.append(nav)

    quality_buttons = [
        InlineKeyboardButton(
            "⬇ " + source["label"],
            callback_data="dl:" + enc(ep["page_url"]) + ":" + enc(source["label"]),
        )
        for source in ep["sources"]
    ]
    if quality_buttons:
        rows.append(quality_buttons)

    return InlineKeyboardMarkup(rows)


def episode_text(ep):
    title = html.escape(ep["title"])
    synopsis = html.escape(ep.get("synopsis") or "")
    if len(synopsis) > 600:
        synopsis = synopsis[:597] + "..."

    total = ep.get("series_total") or "?"
    lines = [
        f"🎬 <b>{title}</b>",
        f"📺 Episode: <b>{ep.get('episode') or '?'}</b>",
        f"📚 Total Episodes: <b>{total}</b>",
    ]

    if synopsis:
        lines += ["", "📝 <b>Synopsis</b>", synopsis]

    available = ", ".join(html.escape(s["label"]) for s in ep["sources"])
    if available:
        lines += ["", f"🎥 <b>Quality:</b> {available}"]

    return "\n".join(lines)


async def show(message, ep):
    caption = episode_text(ep)
    markup = episode_keyboard(ep)

    if ep.get("thumbnail"):
        try:
            await app.send_photo(
                message.chat.id,
                ep["thumbnail"],
                caption=caption,
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    await app.send_message(
        message.chat.id,
        caption,
        reply_markup=markup,
    )


def choose_source(ep, preferred=None):
    sources = ep.get("sources") or []
    if preferred:
        exact = next(
            (source for source in sources if source["label"].lower() == preferred.lower()),
            None,
        )
        if exact:
            return exact

    order = {"2160p": 4, "1440p": 3, "1080p": 2, "720p": 1}
    return max(sources, key=lambda source: order.get(source["label"].lower(), 0), default=None)


async def download_and_send(
    ep,
    target_chat,
    preferred_quality=None,
    status=None,
    post_id=None,
):
    source = choose_source(ep, preferred_quality)
    if not source:
        raise ProviderError("No downloadable video source is available")

    safe = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in ep["title"]
    )[:80]
    label = source["label"]
    output = DOWNLOAD_DIR / f"{safe}-{label}.mp4"
    thumb = DOWNLOAD_DIR / f"{safe}-{label}.jpg"

    if status:
        await status.edit_text(
            f"⬇️ Downloading <b>{html.escape(label)}</b> — "
            f"{html.escape(ep['title'])}"
        )

    try:
        await asyncio.to_thread(
            provider.download,
            source["url"],
            output,
            download_progress_factory(status) if status else None,
        )

        size = output.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ProviderError(
                "File is {:.2f} GB, above the 2 GB Pyrogram limit.".format(
                    size / 1073741824
                )
            )

        metadata = await asyncio.to_thread(video_metadata, output)

        if status:
            await status.edit_text("🖼 Preparing thumbnail and Telegram metadata...")

        thumb_path = None
        if ep.get("thumbnail"):
            try:
                thumb_path = await asyncio.to_thread(
                    provider.download_thumbnail,
                    ep["thumbnail"],
                    thumb,
                )
            except Exception as exc:
                print("[thumbnail] {}".format(exc), flush=True)

        duration = metadata["duration"]
        caption = (
            f"🎬 <b>{html.escape(ep['title'])}</b>\n"
            f"📺 Episode: <b>{ep.get('episode') or '?'}</b>\n"
            f"🎥 Quality: <b>{html.escape(label)}</b>\n"
            f"⏱ Duration: <b>{format_duration(duration)}</b>"
        )

        if status:
            await status.edit_text("📤 Uploading video to Telegram...")

        started = time.monotonic()
        sent = await app.send_video(
            chat_id=target_chat,
            video=str(output),
            thumb=str(thumb_path) if thumb_path else None,
            caption=caption,
            duration=duration or None,
            width=metadata["width"] or None,
            height=metadata["height"] or None,
            file_name=output.name,
            supports_streaming=True,
            progress=upload_progress,
            progress_args=(status, started),
        )

        if post_id is not None:
            mark_video_uploaded(post_id, sent.id)

        return sent
    finally:
        output.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)


@app.on_message(filters.command("help"))
async def help_command(_, message):
    await message.reply_text(
        "<b>WHBot Help</b>\n\n"
        "🔎 <b>Browse</b>\n"
        "/latest — show one latest post\n"
        "/search &lt;query&gt; — search the local catalog\n"
        "/episode &lt;URL&gt; — open a specific episode\n\n"
        "⚙️ <b>Catalog</b>\n"
        "/stats — catalog statistics\n"
        "/crawl — choose start/end pages and import them manually\n"
        "/publish — publish catalog metadata\n\n"
        "🤖 <b>Auto uploader</b>\n"
        "/auto — download and upload all pending episodes sequentially\n"
        "/auto 720p — use 720p when available\n"
        "/auto stop — stop after the current transfer\n\n"
        "⬇️ Open an episode to view qualities and download it."
    )


@app.on_message(filters.command("start"))
async def start(_, message):
    await message.reply_text(
        "WHBot ready.\n\n"
        "/latest - latest single catalog post\n"
        "/search <query> - search local catalog\n"
        "/episode <URL> - open an episode\n"
        "/stats - catalog statistics\n"
        "/crawl - manual catalog import\n"
        "/publish - publish catalog metadata\n"
        "/auto - sequentially upload pending episodes"
    )


@app.on_message(filters.command("latest"))
async def latest(_, message):
    status = await message.reply_text("🔎 Loading latest post...")
    try:
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT url FROM posts ORDER BY id DESC LIMIT 1",
        )
        if not rows:
            await status.edit_text("Catalog is empty. Run /crawl first.")
            return

        ep = await asyncio.to_thread(provider.get_episode, rows[0][0], True)
        await status.delete()
        await show(message, ep)
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("search"))
async def search(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /search <title>")
        return

    query = " ".join(message.command[1:]).strip()
    status = await message.reply_text(
        f"🔎 Searching local catalog for <b>{html.escape(query)}</b>..."
    )
    try:
        like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT url FROM posts WHERE title LIKE ? ESCAPE '\\' "
            "OR synopsis LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 20",
            (like, like),
        )
        if not rows:
            await status.edit_text("No matching posts in local catalog.")
            return

        await status.delete()
        # Search intentionally shows results one at a time, each with navigation.
        for (url,) in rows[:10]:
            try:
                ep = await asyncio.to_thread(provider.get_episode, url, True)
                await show(message, ep)
            except Exception as exc:
                await message.reply_text("⚠️ Could not load result: " + str(exc))
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("stats"))
async def stats(_, message):
    try:
        row = await asyncio.to_thread(
            db_rows,
            "SELECT COUNT(*), "
            "SUM(CASE WHEN published=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN video_uploaded=1 THEN 1 ELSE 0 END) FROM posts",
        )
        total, published, uploaded = row[0]
        await message.reply_text(
            f"📊 <b>Catalog</b>\n\n"
            f"Posts: <b>{total or 0}</b>\n"
            f"Metadata published: <b>{published or 0}</b>\n"
            f"Videos uploaded: <b>{uploaded or 0}</b>"
        )
    except Exception as exc:
        await message.reply_text("❌ " + str(exc))


@app.on_message(filters.command("crawl"))
async def crawl_command(_, message):
    CRAWL_STATES[message.chat.id] = {"stage": "start"}
    await message.reply_text(
        "🕷 <b>Catalog crawler</b>\n\n"
        "Send the <b>starting page number</b>.\n"
        "Use <b>0</b> for https://watchhentai.net/videos/.\n"
        "Example: <code>0</code>"
    )


@app.on_message(filters.text)
async def crawl_input(_, message):
    state = CRAWL_STATES.get(message.chat.id)
    if not state or message.text.startswith("/"):
        return

    try:
        value = int(message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "❌ Send a whole number such as <b>0</b>, <b>7</b> or <b>121</b>."
        )
        return

    if state["stage"] == "start":
        state["start"] = value
        state["stage"] = "end"
        await message.reply_text(
            "✅ Starting page: <b>{}</b>\n\n"
            "Now send the <b>ending page number</b>.\n"
            "It must be greater than or equal to the starting page.".format(value)
        )
        return

    start_page = state["start"]
    end_page = value

    if end_page < start_page:
        await message.reply_text("❌ Ending page cannot be smaller than starting page.")
        return

    CRAWL_STATES.pop(message.chat.id, None)

    status = await message.reply_text(
        "🕷 <b>Crawling pages {} → {}</b>\n"
        "0 = /videos/; other numbers = /videos/page/N/\n\n"
        "Every discovered post will be resolved completely before it is saved.".format(
            start_page, end_page
        )
    )

    try:
        from crawler.catalog import crawl

        with BACKGROUND_LOCK:
            result = await asyncio.to_thread(crawl, start_page, end_page, 0.25)

        new_ids = result.get("new_ids", [])
        published = 0

        if new_ids:
            await status.edit_text(
                "📢 Crawl complete. Publishing {} newly crawled post(s)...".format(
                    len(new_ids)
                )
            )
            with BACKGROUND_LOCK:
                published = await asyncio.to_thread(publish_ids, new_ids)

        await status.edit_text(
            "✅ <b>Crawl completed</b>\n\n"
            "Pages: <b>{} → {}</b>\n"
            "Posts processed: <b>{}</b>\n"
            "New posts: <b>{}</b>\n"
            "Channel posts published: <b>{}</b>".format(
                start_page,
                end_page,
                result.get("processed", 0),
                len(new_ids),
                published,
            )
        )
    except Exception as exc:
        await status.edit_text("❌ Crawl failed: " + html.escape(str(exc)))


@app.on_message(filters.command("publish"))
async def publish_command(_, message):
    status = await message.reply_text("📢 Publishing pending catalog posts...")
    try:
        with BACKGROUND_LOCK:
            count = await asyncio.to_thread(publish_pending, 20)
        await status.edit_text(
            "✅ Published <b>{}</b> pending post(s).".format(count)
        )
    except Exception as exc:
        await status.edit_text("❌ Publish failed: " + str(exc))


@app.on_message(filters.command("episode"))
async def episode(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /episode <URL>")
        return

    try:
        ep = await asyncio.to_thread(
            provider.get_episode, " ".join(message.command[1:]), True
        )
        await show(message, ep)
    except Exception as exc:
        await message.reply_text("❌ " + str(exc))


def format_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def progress_text(prefix, current, total, started):
    elapsed = max(time.monotonic() - started, 0.001)
    speed = current / elapsed
    percent = current * 100 / total if total else 0
    eta = (total - current) / speed if total and speed else 0

    size = f"{current / 1073741824:.2f}"
    total_size = f"{total / 1073741824:.2f}" if total else "?"
    speed_mb = speed / 1048576

    return (
        f"{prefix}\n"
        f"Progress: <b>{percent:.1f}%</b>\n"
        f"Size: <b>{size} / {total_size} GB</b>\n"
        f"Speed: <b>{speed_mb:.2f} MB/s</b>\n"
        f"ETA: <b>{format_time(eta)}</b>"
    )


async def safe_edit(status, text):
    if status is None:
        return
    try:
        await status.edit_text(text)
    except RPCError:
        pass


async def upload_progress(current, total, status, started):
    if status is None:
        return
    now = time.monotonic()
    last = getattr(upload_progress, "_last", 0.0)
    if now - last < 1.0 and current < total:
        return
    upload_progress._last = now
    await safe_edit(
        status,
        progress_text("📤 Uploading...", current, total, started),
    )


def download_progress_factory(status):
    state = {"last": 0.0}

    def progress(current, total, started):
        if status is None:
            return
        now = time.monotonic()
        if now - state["last"] < 1.0 and (not total or current < total):
            return
        state["last"] = now
        asyncio.run_coroutine_threadsafe(
            safe_edit(
                status,
                progress_text("⬇️ Downloading...", current, total, started),
            ),
            app.loop,
        )

    return progress


async def run_auto(status, preferred_quality=None):
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID is required for /auto")

    con = init_db()
    try:
        rows = con.execute(
            """SELECT id,url,title,episode,synopsis,thumbnail,series_url,
                      video_uploaded
               FROM posts
               WHERE video_uploaded=0
               ORDER BY COALESCE(series_url, url) ASC,
                        CASE WHEN episode IS NULL THEN 2147483647 ELSE episode END ASC,
                        id ASC"""
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await status.edit_text("✅ Auto queue is empty. All catalog videos are uploaded.")
        return

    AUTO_STOP.clear()
    total = len(rows)
    completed = 0
    current_series = None

    for index, row in enumerate(rows, 1):
        if AUTO_STOP.is_set():
            await status.edit_text(
                f"⏹ Auto uploader stopped. Completed {completed}/{total}."
            )
            return

        post_id, url, title, episode_number, synopsis, thumbnail, series_url, _ = row
        series_key = series_url or url

        if series_key != current_series:
            current_series = series_key
            await status.edit_text(
                f"🤖 <b>Auto uploader</b>\n"
                f"Series: <b>{html.escape(series_key.rsplit('/', 1)[-1].replace('-', ' '))}</b>\n"
                f"Queue: {index}/{total}"
            )

        try:
            ep = await asyncio.to_thread(provider.get_episode, url, True)
            await download_and_send(
                ep,
                CHANNEL_ID,
                preferred_quality=preferred_quality,
                status=status,
                post_id=post_id,
            )
            completed += 1
            await status.edit_text(
                f"✅ Uploaded <b>{html.escape(ep['title'])}</b>\n"
                f"Progress: <b>{completed}/{total}</b>"
            )
        except Exception as exc:
            await status.edit_text(
                f"⚠️ Failed <b>{html.escape(title or url)}</b>\n"
                f"{html.escape(str(exc))}\n"
                f"Progress: {completed}/{total}. Continuing..."
            )
            await asyncio.sleep(1)

    await status.edit_text(
        f"🎉 <b>Auto uploader finished</b>\n"
        f"Uploaded: <b>{completed}/{total}</b>"
    )


@app.on_message(filters.command("auto"))
async def auto_command(_, message):
    args = message.command[1:]
    if args and args[0].lower() == "stop":
        AUTO_STOP.set()
        await message.reply_text("⏹ Auto uploader will stop after the current transfer.")
        return

    preferred = args[0] if args else None
    if preferred and preferred.lower() not in {"720p", "1080p", "1440p", "2160p"}:
        await message.reply_text("Usage: /auto [720p|1080p|1440p|2160p] or /auto stop")
        return

    if AUTO_LOCK.locked():
        await message.reply_text("⚠️ Auto uploader is already running.")
        return

    status = await message.reply_text("🤖 Starting sequential auto uploader...")
    async with AUTO_LOCK:
        try:
            await run_auto(status, preferred)
        except Exception as exc:
            await status.edit_text("❌ Auto uploader failed: " + str(exc))


@app.on_callback_query()
async def callback(_, query):
    await query.answer()

    try:
        if query.data.startswith("ep:"):
            ep = await asyncio.to_thread(
                provider.get_episode, dec(query.data[3:]), True
            )
            await show(query.message, ep)
            return

        if not query.data.startswith("dl:"):
            return

        encoded_page, encoded_label = query.data[3:].split(":", 1)
        page_url = dec(encoded_page)
        label = dec(encoded_label)
        ep = await asyncio.to_thread(provider.get_episode, page_url, True)

        status = await query.message.reply_text(
            f"⬇️ Preparing <b>{html.escape(label)}</b>..."
        )
        try:
            await download_and_send(
                ep,
                query.message.chat.id,
                preferred_quality=label,
                status=status,
            )
            await status.delete()
        except Exception:
            raise

    except Exception as exc:
        await query.message.reply_text("❌ " + str(exc))


def scheduler_loop():
    from crawler.scheduler import check_once

    if INITIAL_CRAWL_PAGES:
        try:
            print(
                "[scheduler] initial crawl: {} page(s)".format(INITIAL_CRAWL_PAGES),
                flush=True,
            )
            with BACKGROUND_LOCK:
                from crawler.catalog import crawl
                crawl(INITIAL_CRAWL_PAGES, 0.25)
        except Exception as exc:
            print("[scheduler] initial crawl failed: {}".format(exc), flush=True)

    while True:
        try:
            print(
                "[scheduler] checking first {} page(s)...".format(CRAWL_PAGES),
                flush=True,
            )
            with BACKGROUND_LOCK:
                added = check_once(CRAWL_PAGES)
                published = publish_pending(PUBLISH_LIMIT)
            print(
                "[scheduler] added={}, published={}".format(added, published),
                flush=True,
            )
        except Exception as exc:
            print("[scheduler] check failed: {}".format(exc), flush=True)

        time.sleep(CRAWL_INTERVAL)


def start_background_scheduler():
    if not SCHEDULER_ENABLED:
        print("[scheduler] disabled (manual /crawl and /publish only)", flush=True)
        return

    thread = threading.Thread(
        target=scheduler_loop,
        name="catalog-scheduler",
        daemon=True,
    )
    thread.start()
    print(
        "[scheduler] enabled: every {}s, pages={}, publish_limit={}".format(
            CRAWL_INTERVAL, CRAWL_PAGES, PUBLISH_LIMIT
        ),
        flush=True,
    )


if __name__ == "__main__":
    print("WHBot starting", flush=True)
    start_background_scheduler()
    app.run()
