import asyncio
import html
import os
import time
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from providers.watchhentai import WatchHentai, ProviderError
from crawler.catalog import DB_PATH

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CALLBACK_URLS = {}

app = Client(
    "whbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(Path(".pyrogram")),
)
provider = WatchHentai()


def enc(value):
    import hashlib
    token = hashlib.sha256(value.encode()).hexdigest()[:12]
    CALLBACK_URLS[token] = value
    return token


def dec(value):
    if value not in CALLBACK_URLS:
        raise RuntimeError("This button has expired. Open the episode again.")
    return CALLBACK_URLS[value]


def episode_keyboard(ep):
    rows = []
    nav = []
    navigation = ep["navigation"]

    if navigation.get("previous"):
        nav.append(InlineKeyboardButton("⬅ Previous", callback_data="ep:" + enc(navigation["previous"])))
    if navigation.get("series"):
        nav.append(InlineKeyboardButton("📋 All Episodes", url=navigation["series"]))
    if navigation.get("next"):
        nav.append(InlineKeyboardButton("Next ➡", callback_data="ep:" + enc(navigation["next"])))
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
    # Telegram captions have a finite size; keep the useful description readable.
    if len(synopsis) > 700:
        synopsis = synopsis[:697] + "..."

    lines = [
        f"🎬 <b>{title}</b>",
        f"📺 Episode: <b>{ep.get('episode') or '?'}</b>",
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
                parse_mode="html",
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    await app.send_message(
        message.chat.id,
        caption,
        parse_mode="html",
        reply_markup=markup,
    )


@app.on_message(filters.command("start"))
async def start(_, message):
    await message.reply_text(
        "WHBot ready.\n\n"
        "/latest - latest catalog posts\n"
        "/search <query> - search local catalog\n"
        "/episode <URL> - open an episode\n"
        "/stats - catalog statistics\n"
        "/crawl - import catalog pages 1-121"
    )


def db_rows(query, params=()):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


@app.on_message(filters.command("latest"))
async def latest(_, message):
    status = await message.reply_text("🔎 Loading latest catalog posts...")
    try:
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT url FROM posts ORDER BY id DESC LIMIT 10",
        )
        if not rows:
            await status.edit_text("Catalog is empty. Run /crawl first.")
            return
        await status.delete()
        for (url,) in rows:
            try:
                ep = await asyncio.to_thread(provider.get_episode, url, True)
                await show(message, ep)
            except Exception as exc:
                await message.reply_text("⚠️ Could not load one episode: " + str(exc))
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("search"))
async def search(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /search <title>")
        return

    query = " ".join(message.command[1:]).strip()
    status = await message.reply_text(
        f"🔎 Searching local catalog for <b>{html.escape(query)}</b>...",
        parse_mode="html",
    )
    try:
        like = "%" + query.replace("%", "\%").replace("_", "\_") + "%"
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT url FROM posts WHERE title LIKE ? ESCAPE '\\' "
            "OR synopsis LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 20",
            (like, like),
        )
        if not rows:
            await status.edit_text("No matching posts in local catalog.")
            return
        await status.edit_text(f"Found {len(rows)} result(s). Loading...")
        for (url,) in rows[:10]:
            try:
                ep = await asyncio.to_thread(provider.get_episode, url, True)
                await show(message, ep)
            except Exception as exc:
                await message.reply_text("⚠️ Could not load result: " + str(exc))
        await status.delete()
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("stats"))
async def stats(_, message):
    try:
        row = await asyncio.to_thread(
            db_rows,
            "SELECT COUNT(*), SUM(CASE WHEN published=1 THEN 1 ELSE 0 END) FROM posts",
        )
        total, published = row[0]
        await message.reply_text(
            f"📊 <b>Catalog</b>\n\n"
            f"Posts: <b>{total or 0}</b>\n"
            f"Published: <b>{published or 0}</b>",
            parse_mode="html",
        )
    except Exception as exc:
        await message.reply_text("❌ " + str(exc))


@app.on_message(filters.command("crawl"))
async def crawl_command(_, message):
    status = await message.reply_text("🕷 Starting catalog import: pages 1-121...")
    try:
        from crawler.catalog import crawl
        await asyncio.to_thread(crawl, 121, 0.25)
        await status.edit_text("✅ Catalog import completed.")
    except Exception as exc:
        await status.edit_text("❌ Crawl failed: " + str(exc))


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
    try:
        await status.edit_text(text, parse_mode="html")
    except RPCError:
        pass


async def upload_progress(current, total, status, started):
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
        now = time.monotonic()
        if now - state["last"] < 1.0 and (not total or current < total):
            return
        state["last"] = now

        text = progress_text("⬇️ Downloading...", current, total, started)

        # Provider runs in a worker thread. Schedule the Telegram edit on
        # the main asyncio loop.
        asyncio.run_coroutine_threadsafe(
            safe_edit(status, text),
            app.loop,
        )

    return progress


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

        ep = await asyncio.to_thread(
            provider.get_episode, page_url, True
        )

        source = next(
            (item for item in ep["sources"] if item["label"] == label),
            None,
        )
        if not source:
            raise ProviderError("Source unavailable")

        safe = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in ep["title"]
        )[:80]

        output = DOWNLOAD_DIR / f"{safe}-{label}.mp4"

        status = await query.message.reply_text(
            f"⬇️ Downloading <b>{html.escape(label)}</b>...",
            parse_mode="html",
        )

        try:
            await asyncio.to_thread(
                provider.download,
                source["url"],
                output,
                download_progress_factory(status),
            )

            size = output.stat().st_size

            if size > MAX_UPLOAD_BYTES:
                await status.edit_text(
                    f"❌ File is {size / 1073741824:.2f} GB, above the 2 GB application limit."
                )
                output.unlink(missing_ok=True)
                return

            await status.edit_text("📤 Preparing Telegram upload...")
            started = time.monotonic()

            # Successful return means Telegram accepted the upload.
            await app.send_video(
                chat_id=query.message.chat.id,
                video=str(output),
                caption=f"{ep['title']} — {label}",
                supports_streaming=True,
                progress=upload_progress,
                progress_args=(status, started),
            )

            # Delete ONLY after successful Telegram upload.
            output.unlink(missing_ok=True)
            await status.delete()

        except Exception:
            # Keep the local file if an upload fails so it can be inspected/retried.
            raise

    except Exception as exc:
        await query.message.reply_text("❌ " + str(exc))


if __name__ == "__main__":
    print("WHBot running with Pyrogram")
    app.run()
