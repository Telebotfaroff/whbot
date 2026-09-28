import asyncio
import hashlib
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from providers.watchhentai import WatchHentai

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

app = Client(
    "whbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(Path(".pyrogram")),
)
provider = WatchHentai()


CALLBACK_URLS = {}

def enc(value: str) -> str:
    token = hashlib.sha256(value.encode()).hexdigest()[:12]
    CALLBACK_URLS[token] = value
    return token

def dec(value: str) -> str:
    if value not in CALLBACK_URLS:
        raise RuntimeError("This button has expired. Open the episode again.")
    return CALLBACK_URLS[value]


def keyboard(ep):
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

    qualities = [
        InlineKeyboardButton(
            "⬇ " + source["label"],
            callback_data="dl:" + enc(ep["page_url"]) + ":" + enc(source["label"]),
        )
        for source in ep["sources"]
    ]
    if qualities:
        rows.append(qualities)

    return InlineKeyboardMarkup(rows)


def episode_text(ep):
    available = ", ".join(source["label"] for source in ep["sources"]) or "none"
    return (
        f"🎬 <b>{ep['title']}</b>\n"
        f"Episode: <b>{ep.get('episode') or '?'}</b>\n\n"
        f"Available: {available}"
    )


async def show(client, message, ep):
    caption = episode_text(ep)
    try:
        if ep.get("thumbnail"):
            await client.send_photo(
                message.chat.id,
                ep["thumbnail"],
                caption=caption,
                parse_mode="html",
                reply_markup=keyboard(ep),
            )
            return
    except Exception:
        pass

    await client.send_message(
        message.chat.id,
        caption,
        parse_mode="html",
        reply_markup=keyboard(ep),
    )


@app.on_message(filters.command("start"))
async def start(_, message):
    await message.reply_text("WHBot ready.\n\n/latest\n/episode <WatchHentai URL>")


@app.on_message(filters.command("latest"))
async def latest(_, message):
    status = await message.reply_text("🔎 Loading homepage...")
    try:
        items = await asyncio.to_thread(provider.latest, 1)
        for item in items[:10]:
            ep = await asyncio.to_thread(provider.get_episode, item["page_url"], True)
            await show(app, message, ep)
        await status.delete()
    except Exception as exc:
        await status.edit_text("❌ " + str(exc))


@app.on_message(filters.command("episode"))
async def episode(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /episode <URL>")
        return

    try:
        ep = await asyncio.to_thread(
            provider.get_episode, " ".join(message.command[1:]), True
        )
        await show(app, message, ep)
    except Exception as exc:
        await message.reply_text("❌ " + str(exc))


def progress_text(prefix, current, total, started):
    elapsed = max(time.monotonic() - started, 0.001)
    percent = current * 100 / total if total else 0
    speed = current / elapsed
    remaining = max(total - current, 0)
    eta = remaining / speed if speed else 0

    def fmt(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60}s"

    return (
        f"{prefix}\n"
        f"Progress: <b>{percent:.1f}%</b>\n"
        f"Size: <b>{current / 1073741824:.2f} / {total / 1073741824:.2f} GB</b>\n"
        f"Speed: <b>{speed / 1048576:.2f} MB/s</b>\n"
        f"ETA: <b>{fmt(eta)}</b>"
    )


async def upload_progress(current, total, status, started):
    now = time.monotonic()
    last = getattr(upload_progress, "_last", 0.0)
    if now - last < 1.0 and current < total:
        return
    upload_progress._last = now

    try:
        await status.edit_text(
            progress_text("📤 Uploading...", current, total, started),
            parse_mode="html",
        )
    except RPCError:
        pass


@app.on_callback_query()
async def callback(_, query):
    await query.answer()

    try:
        if query.data.startswith("ep:"):
            ep = await asyncio.to_thread(
                provider.get_episode, dec(query.data[3:]), True
            )
            await show(app, query.message, ep)
            return

        if not query.data.startswith("dl:"):
            return

        encoded_page, encoded_label = query.data[3:].split(":", 1)
        page_url = dec(encoded_page)
        label = dec(encoded_label)

        ep = await asyncio.to_thread(provider.get_episode, page_url, True)
        source = next((item for item in ep["sources"] if item["label"] == label), None)
        if not source:
            raise RuntimeError("Source unavailable")

        safe = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in ep["title"]
        )[:80]
        output = DOWNLOAD_DIR / f"{safe}-{label}.mp4"

        status = await query.message.reply_text(
            f"⬇️ Downloading <b>{label}</b>...",
            parse_mode="html",
        )

        await asyncio.to_thread(provider.download, source["url"], output)

        size = output.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            await status.edit_text(
                f"❌ File is {size / 1073741824:.2f} GB, above the 2 GB application limit."
            )
            output.unlink(missing_ok=True)
            return

        await status.edit_text("📤 Preparing Telegram upload...")
        started = time.monotonic()

        try:
            await app.send_video(
                chat_id=query.message.chat.id,
                video=str(output),
                caption=f"{ep['title']} — {label}",
                supports_streaming=True,
                progress=upload_progress,
                progress_args=(status, started),
            )
        finally:
            output.unlink(missing_ok=True)

        await status.delete()

    except Exception as exc:
        await query.message.reply_text("❌ " + str(exc))


if __name__ == "__main__":
    print("WHBot running with Pyrogram")
    app.run()
