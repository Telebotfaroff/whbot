import asyncio
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from providers.watchhentai import WatchHentai, ProviderError
from crawler.catalog import DB_PATH, init_db, crawl_series
from publisher import publish_pending, publish_ids, publish_series_ids

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DOWNLOAD_CHANNEL_ID = os.getenv("DOWNLOAD_CHANNEL_ID")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PYROGRAM_WORKDIR = Path(os.getenv("PYROGRAM_WORKDIR", "./.pyrogram"))
PYROGRAM_WORKDIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
# Small pause before each Telegram media upload to reduce request bursts.
TELEGRAM_UPLOAD_DELAY = float(os.getenv("TELEGRAM_UPLOAD_DELAY", "3.0"))
TELEGRAM_PROGRESS_INTERVAL = max(float(os.getenv("TELEGRAM_PROGRESS_INTERVAL", "4.0")), 2.0)
# Pyrogram uploads files in parallel chunks. Increase this for better throughput.
# Keep it configurable because very high values can trigger Telegram flood control.
MAX_CONCURRENT_TRANSMISSIONS = max(int(os.getenv("MAX_CONCURRENT_TRANSMISSIONS", "4")), 1)
CALLBACK_URLS = {}
AUTO_LOCK = asyncio.Lock()
AUTO_STOP = threading.Event()
CRAWL_STATES = {}
DOWNLOAD_STATES = {}

app = Client(
    "whbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(PYROGRAM_WORKDIR),
    parse_mode=ParseMode.HTML,
    max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
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

    # Persist callback mappings so buttons continue working after a Colab/bot
    # restart. The in-memory cache is still used for fast access.
    con = init_db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS callback_urls ("
            "token TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO callback_urls(token, value) VALUES (?, ?)",
            (token, value),
        )
        con.commit()
    finally:
        con.close()

    return token


def dec(value):
    if value in CALLBACK_URLS:
        return CALLBACK_URLS[value]

    con = init_db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS callback_urls ("
            "token TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = con.execute(
            "SELECT value FROM callback_urls WHERE token=?",
            (value,),
        ).fetchone()
    finally:
        con.close()

    if not row:
        raise RuntimeError("This button has expired. Open the episode again.")
    CALLBACK_URLS[value] = row[0]
    return row[0]


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
    # Ensure the SQLite schema exists before any read. This is important on a
    # fresh Colab runtime where /latest may be the first database operation.
    con = init_db()
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
    series_name=None,
    series_total=None,
):
    print("[download] title={} preferred={} sources={}".format(
        ep.get("title"), preferred_quality, len(ep.get("sources") or [])
    ), flush=True)
    for item in (ep.get("sources") or []):
        print("[download] available label={} type={} url={}".format(
            item.get("label"), item.get("type"), str(item.get("url", ""))[:180]
        ), flush=True)

    source = choose_source(ep, preferred_quality)
    if not source:
        print("[download] ERROR: no source selected", flush=True)
        raise ProviderError("No downloadable video source is available")

    print("[download] selected label={} url={}".format(
        source.get("label"), str(source.get("url", ""))[:300]
    ), flush=True)

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
            ep.get("page_url"),
        )

        size = output.stat().st_size
        print("[download] completed local file size={}".format(size), flush=True)
        if size > MAX_UPLOAD_BYTES:
            raise ProviderError(
                "File is {:.2f} GB, above the 2 GB Pyrogram limit.".format(
                    size / 1073741824
                )
            )

        metadata = await asyncio.to_thread(video_metadata, output)
        print("[upload] ffprobe duration={} width={} height={}".format(
            metadata.get("duration"), metadata.get("width"), metadata.get("height")
        ), flush=True)

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
        print("[upload] waiting {}s before Telegram upload".format(TELEGRAM_UPLOAD_DELAY), flush=True)
        await asyncio.sleep(TELEGRAM_UPLOAD_DELAY)
        print("[upload] sending {} to chat {}".format(output.name, target_chat), flush=True)

        sent = None
        for upload_attempt in range(1, 4):
            try:
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
                break
            except FloodWait as exc:
                wait = max(int(getattr(exc, "value", 0) or 0), 1)
                print(
                    "[upload] Telegram FloodWait={}s attempt {}/3".format(
                        wait, upload_attempt
                    ),
                    flush=True,
                )
                if upload_attempt >= 3:
                    raise
                await asyncio.sleep(wait + 1)

        if sent is None:
            raise ProviderError("Telegram upload did not return a message")

        print("[upload] Telegram message id={}".format(sent.id), flush=True)
        if post_id is not None:
            mark_video_uploaded(post_id, sent.id)

        return sent
    except Exception as exc:
        print("[download/upload] FAILED: {}".format(exc), flush=True)
        raise
    finally:
        output.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)


@app.on_message(filters.command("ping"))
async def ping_command(_, message):
    print("[command] /ping chat_id={}".format(message.chat.id), flush=True)
    await message.reply_text("🏓 <b>WHBot is responding.</b>")


@app.on_message(filters.command("debug"))
async def debug_command(_, message):
    print("[command] /debug chat_id={}".format(message.chat.id), flush=True)
    try:
        con = init_db()
        try:
            posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            series = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        finally:
            con.close()

        await message.reply_text(
            "🧪 <b>WHBot diagnostics</b>\n\n"
            "✅ Bot handler: working\n"
            "📁 DB: <code>{}</code>\n"
            "📄 Posts: <b>{}</b>\n"
            "📚 Series: <b>{}</b>\n"
            "📥 Download channel: <code>{}</code>".format(
                DB_PATH, posts, series, DOWNLOAD_CHANNEL_ID or "NOT SET"
            )
        )
    except Exception as exc:
        print("[command] /debug FAILED: {}".format(exc), flush=True)
        await message.reply_text("❌ DEBUG FAILED: " + html.escape(str(exc)))


@app.on_message(filters.all, group=99)
async def incoming_debug(_, message):
    # Log only the message type/command, never tokens or full message text.
    try:
        text_value = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if text_value.startswith("/"):
            command_name = text_value.split()[0].split("@")[0]
            print("[incoming] command={} chat_id={}".format(
                command_name, message.chat.id
            ), flush=True)
    except Exception as exc:
        print("[incoming] log error: {}".format(exc), flush=True)


@app.on_message(filters.command("help"))
async def help_command(_, message):
    print("[command] /help chat_id={}".format(message.chat.id), flush=True)
    await message.reply_text(
        "<b>WHBot Help</b>\n\n"
        "🔎 <b>Browse</b>\n"
        "/latest — show one latest post\n"
        "/search &lt;query&gt; — search the local catalog\n"
        "/episode &lt;URL&gt; — open a specific episode\n\n"
        "⚙️ <b>Catalog</b>\n"
        "/stats — catalog statistics\n"
        "/crawl — crawl series pages into the catalog\n"
        "/download — download an entire series to the download channel\n"
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
        "/crawl - crawl series catalog\n"
        "/download - download a complete series to the download channel\n"
        "/publish - publish catalog metadata\n"
        "/auto - sequentially upload pending episodes"
    )


@app.on_message(filters.command("latest"))
async def latest(_, message):
    print("[command] /latest chat_id={}".format(message.chat.id), flush=True)
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
    print("[command] /search chat_id={}".format(message.chat.id), flush=True)
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


def series_crawl_progress_factory(status):
    state = {"last": 0.0}

    def progress(page, total_pages, processed, new_count, series):
        now = time.monotonic()
        if now - state["last"] < 5.0:
            return
        state["last"] = now
        name = series.get("name") if series else None
        episodes = series.get("total_episodes") if series else None
        current = (
            f"🎬 Current: <b>{html.escape(name)}</b>\n"
            f"📺 Episodes: <b>{episodes or 0}</b>\n"
            if name else ""
        )
        text = (
            "🕷️ <b>Series crawler running</b>\n\n"
            f"📄 Page: <b>{page}/{total_pages}</b>\n"
            f"📚 Series processed: <b>{processed}</b>\n"
            f"🆕 New series: <b>{new_count}</b>\n"
            + current + "\n"
            "🔄 Updating every 5 seconds"
        )
        asyncio.run_coroutine_threadsafe(safe_edit(status, text), app.loop)

    return progress


@app.on_message(filters.command("crawl"))
async def crawl_command(_, message):
    CRAWL_STATES[message.chat.id] = {"stage": "start"}
    await message.reply_text(
        "🕷 <b>Catalog crawler</b>\n\n"
        "Send the <b>starting series page number</b>.\n"
        "<b>1</b> = the first series page.\n"
        "<b>2</b> = the second series page.\n"
        "Example: <code>1</code>"
    )


@app.on_message(filters.text)
async def crawl_input(_, message):
    if message.text.startswith("/"):
        return

    if DOWNLOAD_STATES.get(message.chat.id):
        await start_series_download(message, message.text.strip())
        return

    state = CRAWL_STATES.get(message.chat.id)
    if not state:
        return

    try:
        value = int(message.text.strip())
        if value < 1:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "❌ Send a whole number starting from <b>1</b>, such as <b>1</b>, <b>7</b> or <b>121</b>."
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
        "🕷 <b>Crawling series pages {} → {}</b>\n"
        "1 = /series/; 2+ = /series/page/N/\n\n"
        "Each series is saved with its thumbnail, title, episode count, series URL and all episode URLs.\n"
        "New series are then published to the configured Telegram channel.".format(
            start_page, end_page
        )
    )

    try:
        with BACKGROUND_LOCK:
            progress = series_crawl_progress_factory(status)
            result = await asyncio.to_thread(
                crawl_series, start_page, end_page, 0.25, None, progress
            )

            new_series_ids = result.get("new_ids", [])
            published = 0
            if new_series_ids:
                published = await asyncio.to_thread(
                    publish_series_ids, new_series_ids
                )

        await status.edit_text(
            "✅ <b>Series crawl completed</b>\n\n"
            "Pages: <b>{} → {}</b>\n"
            "Series processed: <b>{}</b>\n"
            "New series saved: <b>{}</b>\n"
            "Published to channel: <b>{}</b>\n\n"
            "Saved data: title, thumbnail, episode count, series URL and all episode URLs.".format(
                start_page,
                end_page,
                result.get("processed", 0),
                len(result.get("new_ids", [])),
                published,
            )
        )
    except Exception as exc:
        await status.edit_text("❌ Series crawl failed: " + html.escape(str(exc)))


@app.on_message(filters.regex(r"^/download(?:@\\w+)?(?:\\s+.*)?$", flags=re.I))
async def download_command(_, message):
    print("[command] /download chat_id={}".format(message.chat.id), flush=True)
    if not DOWNLOAD_CHANNEL_ID:
        await message.reply_text("❌ DOWNLOAD_CHANNEL_ID is not configured.")
        return

    if len(message.command) >= 2:
        await start_series_download(message, " ".join(message.command[1:]).strip())
        return

    DOWNLOAD_STATES[message.chat.id] = True
    await message.reply_text(
        "📥 <b>Series downloader</b>\n\n"
        "Send the series URL.\n"
        "Example:\n"
        "<code>https://watchhentai.net/series/kuro-gal-a-la-carte-id-01/</code>"
    )


async def resolve_series(series_url):
    if not re.match(r"^https?://watchhentai\.net/series/[^\s]+/?$", series_url, re.I):
        raise ProviderError("Please send a valid WatchHentai series URL.")

    series = await asyncio.to_thread(provider.get_series, series_url)
    episode_items = await asyncio.to_thread(
        provider.series_episodes, series["series_url"]
    )
    if not episode_items:
        raise ProviderError("No episodes found in this series.")

    series["total_episodes"] = series.get("total_episodes") or len(episode_items)
    return series, episode_items


async def show_series_preview(message, series, episode_items):
    total = series.get("total_episodes") or len(episode_items)
    caption = (
        f"🎬 <b>{html.escape(series['name'])}</b>\n"
        f"📺 Episodes found: <b>{len(episode_items)}</b>\n"
        f"📚 Total Episodes: <b>{total}</b>\n\n"
        "🔗 Episode pages have been collected.\n"
        "⬇️ Press the button below to resolve each episode's actual video source and download it."
    )
    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "⬇️ Download All Episodes",
                callback_data="sd:" + enc(series["series_url"]),
            )
        ]]
    )

    if series.get("thumbnail"):
        try:
            await app.send_photo(
                message.chat.id,
                series["thumbnail"],
                caption=caption,
                reply_markup=markup,
            )
            return
        except Exception as exc:
            print("[series preview] thumbnail failed: {}".format(exc), flush=True)

    await app.send_message(
        message.chat.id,
        caption,
        reply_markup=markup,
    )


async def download_series(
    message,
    series_url,
    status=None,
    raise_on_error=False,
    acquire_lock=True,
):
    if message is not None:
        DOWNLOAD_STATES.pop(message.chat.id, None)

    if acquire_lock and AUTO_LOCK.locked():
        error = ProviderError("Another download/upload job is already running.")
        if message is not None:
            await message.reply_text("⏳ Another download/upload job is already running.")
        if raise_on_error:
            raise error
        return

    owns_lock = False
    if acquire_lock:
        await AUTO_LOCK.acquire()
        owns_lock = True

    if status is None:
        if message is None:
            raise ProviderError("A status message is required for background downloads.")
        status = await message.reply_text("🔎 Resolving series and episode list...")

    try:
        series, episode_items = await resolve_series(series_url)
        total = series.get("total_episodes") or len(episode_items)

        await safe_edit(
            status,
            "📚 <b>{}</b>\nEpisodes found: <b>{}</b>\n"
            "🔗 Resolving video sources only when each episode is downloaded...".format(
                html.escape(series["name"]), len(episode_items)
            ),
        )

        header_text = (
            f"🎬 <b>{html.escape(series['name'])}</b>\n"
            f"📚 Total Episodes: <b>{total}</b>\n"
            f'🔗 <a href="{html.escape(series["series_url"], quote=True)}">View Series</a>'
        )
        try:
            if series.get("thumbnail"):
                await app.send_photo(
                    DOWNLOAD_CHANNEL_ID,
                    series["thumbnail"],
                    caption=header_text,
                )
            else:
                await app.send_message(DOWNLOAD_CHANNEL_ID, header_text)
        except Exception as exc:
            print("[series header] {}".format(exc), flush=True)
            await app.send_message(DOWNLOAD_CHANNEL_ID, header_text)

        def episode_number(item):
            m = re.search(r"episode[-\s]+(\d+)", item["page_url"], re.I)
            return int(m.group(1)) if m else 10**9

        episode_items.sort(key=episode_number)

        for index, item in enumerate(episode_items, 1):
            print(
                "[series download] episode {}/{} url={}".format(
                    index, total, item["page_url"]
                ),
                flush=True,
            )

            # The series preview deliberately does not resolve media sources.
            # Resolve the episode page and its actual stream/download sources here,
            # immediately before that episode is downloaded.
            ep = await asyncio.to_thread(
                provider.get_episode, item["page_url"], True
            )
            sources = ep.get("sources") or []
            if not sources:
                raise ProviderError(
                    f"Episode {index}/{total} has no downloadable video source."
                )

            order = {"2160p": 4, "1440p": 3, "1080p": 2, "720p": 1}
            sources = sorted(
                sources,
                key=lambda source: order.get(
                    str(source.get("label", "")).lower(), 0
                ),
                reverse=True,
            )

            last_error = None
            uploaded = False
            for source in sources:
                label = str(source.get("label") or "Unknown")
                await safe_edit(
                    status,
                    "⬇️ <b>Episode {}/{}</b> — resolving/downloading <b>{}</b>...".format(
                        index, total, html.escape(label)
                    ),
                )
                print(
                    "[series download] trying episode {} quality={}".format(
                        index, label
                    ),
                    flush=True,
                )
                try:
                    await download_and_send(
                        ep,
                        DOWNLOAD_CHANNEL_ID,
                        preferred_quality=label,
                        status=status,
                        series_name=series["name"],
                        series_total=total,
                    )
                    uploaded = True
                    break
                except Exception as source_exc:
                    last_error = source_exc
                    print(
                        "[series download] quality {} failed: {}".format(
                            label, source_exc
                        ),
                        flush=True,
                    )

            if not uploaded:
                raise ProviderError(
                    "Episode {}/{} failed for all available sources: {}".format(
                        index, total, last_error or "unknown error"
                    )
                )

            await safe_edit(
                status,
                "✅ <b>Episode {}/{}</b> uploaded to download channel.".format(
                    index, total
                ),
            )

        await safe_edit(
            status,
            "🎉 <b>Series download complete</b>\n\n"
            f"{html.escape(series['name'])}\n"
            f"Episodes: <b>{total}</b>",
        )
    except Exception as exc:
        print(
            "[series download] FAILED: {}: {}".format(type(exc).__name__, exc),
            flush=True,
        )
        await safe_edit(
            status,
            "❌ Download failed: " + html.escape(str(exc)),
        )
        if raise_on_error:
            raise
    finally:
        if owns_lock:
            AUTO_LOCK.release()


async def start_series_download(message, series_url):
    # /download is a two-step workflow:
    # 1) scrape the series page and show a preview;
    # 2) resolve actual media sources only after the user presses Download All.
    if message is None:
        raise ProviderError("A message is required for /download preview.")

    DOWNLOAD_STATES.pop(message.chat.id, None)
    status = await message.reply_text("🔎 Scraping series page and collecting episodes...")
    try:
        series, episode_items = await resolve_series(series_url)
        await status.delete()
        await show_series_preview(message, series, episode_items)
    except Exception as exc:
        await safe_edit(status, "❌ Series lookup failed: " + html.escape(str(exc)))




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
    print("[command] /episode chat_id={}".format(message.chat.id), flush=True)
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


PROGRESS_BAR_LENGTH = 12
PROGRESS_UPDATE_INTERVAL = max(float(os.getenv("DOWNLOAD_PROGRESS_INTERVAL", "5.0")), 2.0)


def format_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def format_size(value):
    if not value:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def progress_bar(percent):
    percent = max(0.0, min(float(percent), 100.0))
    filled = int(round(PROGRESS_BAR_LENGTH * percent / 100))
    return "█" * filled + "░" * (PROGRESS_BAR_LENGTH - filled)


def progress_text(prefix, current, total, started):
    elapsed = max(time.monotonic() - started, 0.001)
    speed = current / elapsed
    percent = current * 100 / total if total else 0.0
    eta = (total - current) / speed if total and speed > 0 else 0

    bar = progress_bar(percent)
    current_size = format_size(current)
    total_size = format_size(total) if total else "?"
    speed_size = format_size(speed) + "/s"

    return (
        f"{prefix}\n"
        f"<code>[{bar}] {percent:5.1f}%</code>\n"
        f"📦 <b>{current_size}</b> / <b>{total_size}</b>\n"
        f"⚡ <b>{speed_size}</b>\n"
        f"⏱ <b>{format_time(elapsed)}</b> elapsed  •  "
        f"🕐 <b>{format_time(eta)}</b> left"
    )


def crawl_progress_factory(status):
    state = {"last": 0.0}

    def progress(page, total_pages, found, imported, phase):
        now = time.monotonic()
        if phase != "completed" and now - state["last"] < 2.0:
            return
        state["last"] = now

        percent = page * 100 / total_pages if total_pages else 0
        bar = progress_bar(percent)

        if phase == "completed":
            text = (
                "🕷️ <b>Crawl completed</b>\n"
                f"<code>[{bar}] 100.0%</code>\n"
                f"📄 Pages: <b>{total_pages}/{total_pages}</b>\n"
                f"🆕 New posts: <b>{imported}</b>"
            )
        else:
            text = (
                "🕷️ <b>Crawling catalog</b>\n"
                f"<code>[{bar}] {percent:5.1f}%</code>\n"
                f"📄 Page: <b>{page}/{total_pages}</b>\n"
                f"🔎 Found: <b>{found}</b> episodes\n"
                f"🆕 Imported: <b>{imported}</b>"
            )

        asyncio.run_coroutine_threadsafe(
            safe_edit(status, text),
            app.loop,
        )

    return progress


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
    if now - last < TELEGRAM_PROGRESS_INTERVAL and current < total:
        return
    upload_progress._last = now
    await safe_edit(
        status,
        progress_text("📤 <b>Uploading to Telegram</b>", current, total, started),
    )


def download_progress_factory(status):
    state = {"last": 0.0}

    def progress(current, total, started):
        if status is None:
            return
        now = time.monotonic()
        if now - state["last"] < PROGRESS_UPDATE_INTERVAL and (not total or current < total):
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


async def run_auto(status):
    if not DOWNLOAD_CHANNEL_ID:
        raise RuntimeError("DOWNLOAD_CHANNEL_ID is required for /auto")

    con = init_db()
    try:
        rows = con.execute(
            """SELECT id,url,name,total_episodes
               FROM series
               ORDER BY id ASC"""
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await status.edit_text(
            "✅ Series auto queue is empty. Run /crawl first."
        )
        return

    AUTO_STOP.clear()
    total = len(rows)
    completed = 0

    await AUTO_LOCK.acquire()
    try:
        for index, row in enumerate(rows, 1):
            if AUTO_STOP.is_set():
                await status.edit_text(
                    f"⏹ <b>Series auto downloader stopped.</b>\n"
                    f"Completed: <b>{completed}/{total}</b>"
                )
                return

            series_id, series_url, series_name, episode_total = row

            await safe_edit(
                status,
                f"🤖 <b>Auto series downloader</b>\n"
                f"Series: <b>{html.escape(series_name or series_url)}</b>\n"
                f"Series queue: <b>{index}/{total}</b>\n"
                f"Episodes: <b>{episode_total or '?'}</b>"
            )

            try:
                await download_series(
                    message=None,
                    series_url=series_url,
                    status=status,
                    raise_on_error=True,
                    acquire_lock=False,
                )
                completed += 1

                await safe_edit(
                    status,
                    f"✅ <b>Series {completed}/{total} completed</b>\n"
                    f"{html.escape(series_name or series_url)}"
                )
            except Exception as exc:
                await safe_edit(
                    status,
                    f"⚠️ <b>Series failed</b>\n"
                    f"{html.escape(series_name or series_url)}\n"
                    f"{html.escape(str(exc))}\n\n"
                    f"Continuing: <b>{completed}/{total}</b>"
                )
                await asyncio.sleep(1)

        await safe_edit(
            status,
            f"🎉 <b>Series auto downloader finished</b>\n"
            f"Completed: <b>{completed}/{total}</b>"
        )
    finally:
        AUTO_LOCK.release()


@app.on_message(filters.command("auto"))
async def auto_command(_, message):
    print("[command] /auto chat_id={}".format(message.chat.id), flush=True)
    args = message.command[1:]
    if args and args[0].lower() == "stop":
        AUTO_STOP.set()
        await message.reply_text(
            "⏹ Series auto downloader will stop after the current series."
        )
        return

    if args:
        await message.reply_text(
            "Usage: /auto or /auto stop\n"
            "Quality is selected automatically at the highest available level."
        )
        return

    if AUTO_LOCK.locked():
        await message.reply_text("⚠️ A download/upload job is already running.")
        return

    status = await message.reply_text("🤖 Starting series auto downloader...")

    try:
        await run_auto(status)
    except Exception as exc:
        await status.edit_text(
            "❌ Series auto downloader failed: " + html.escape(str(exc))
        )


@app.on_callback_query()
async def callback(_, query):
    print("[callback] data={}".format(query.data[:80]), flush=True)
    await query.answer()

    try:
        if query.data.startswith("sd:"):
            series_url = dec(query.data[3:])
            if AUTO_LOCK.locked():
                await query.message.reply_text(
                    "⏳ Another download/upload job is already running."
                )
                return

            status = await query.message.reply_text(
                "🔎 <b>Starting series download...</b>\n"
                "Each episode will be resolved individually."
            )
            try:
                await download_series(
                    message=None,
                    series_url=series_url,
                    status=status,
                    raise_on_error=False,
                    acquire_lock=True,
                )
            except Exception as exc:
                await safe_edit(
                    status,
                    "❌ Download failed: " + html.escape(str(exc)),
                )
            return

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
        status = await query.message.reply_text(
            f"⬇️ <b>Preparing {html.escape(label)}</b>..."
        )

        try:
            ep = await asyncio.to_thread(provider.get_episode, page_url, True)

            available = {
                str(source.get("label", "")).lower(): source
                for source in (ep.get("sources") or [])
            }
            if label.lower() not in available:
                raise ProviderError(
                    "Requested quality is not available. Available: "
                    + (", ".join(sorted(available)) or "none")
                )

            await download_and_send(
                ep,
                query.message.chat.id,
                preferred_quality=label,
                status=status,
            )
            await status.delete()
        except Exception as exc:
            await safe_edit(
                status,
                "❌ <b>Download failed</b>\n" + html.escape(str(exc)),
            )

    except Exception as exc:
        await query.message.reply_text("❌ " + html.escape(str(exc)))


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
            print("[scheduler] checking first {} page(s)...".format(CRAWL_PAGES), flush=True)
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
        print("[scheduler] disabled", flush=True)
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
