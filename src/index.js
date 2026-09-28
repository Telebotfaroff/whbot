import "dotenv/config";
import { mkdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { Telegraf } from "telegraf";
import { watchHentai } from "./providers/watchhentai.js";

const token = process.env.BOT_TOKEN;
if (!token) throw new Error("BOT_TOKEN is required");

const bot = new Telegraf(token);
const downloadDir = process.env.DOWNLOAD_DIR || "./downloads";
const maxUploadMb = Number(process.env.MAX_UPLOAD_MB || 50);

await mkdir(downloadDir, { recursive: true });

const b64 = value => Buffer.from(value).toString("base64url");
const unb64 = value => Buffer.from(value, "base64url").toString();

function esc(value = "") {
  return value.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]);
}

function keyboard(ep) {
  const nav = [];
  if (ep.navigation.previous) nav.push({ text: "⬅ Previous", callback_data: "ep:" + b64(ep.navigation.previous) });
  if (ep.navigation.series) nav.push({ text: "📋 All Episodes", url: ep.navigation.series });
  if (ep.navigation.next) nav.push({ text: "Next ➡", callback_data: "ep:" + b64(ep.navigation.next) });

  const rows = nav.length ? [nav] : [];
  const qualities = ep.sources.map(s => ({
    text: "⬇ " + s.label,
    callback_data: "dl:" + b64(ep.pageUrl) + ":" + b64(s.label)
  }));
  if (qualities.length) rows.push(qualities);
  return { inline_keyboard: rows };
}

async function showEpisode(ctx, ep) {
  const text =
    "🎬 <b>" + esc(ep.title) + "</b>\n" +
    "Episode: <b>" + (ep.episode ?? "?") + "</b>\n\n" +
    (ep.sources.length ? "Available: " + ep.sources.map(s => s.label).join(", ") : "Choose an action.");

  const extra = { parse_mode: "HTML", reply_markup: keyboard(ep) };

  if (ep.thumbnail) {
    try {
      await ctx.replyWithPhoto(ep.thumbnail, { caption: text, ...extra });
      return;
    } catch {}
  }
  await ctx.reply(text, extra);
}

bot.start(ctx => ctx.reply("WHBot ready.\n\n/latest — latest homepage episodes\n/episode <URL> — inspect an episode"));

bot.command("latest", async ctx => {
  try {
    await ctx.reply("🔎 Loading homepage...");
    const items = await watchHentai.latest(1);
    if (!items.length) return ctx.reply("No episodes found.");

    for (const item of items.slice(0, 10)) {
      const ep = await watchHentai.getEpisode(item.pageUrl, { resolveSources: true });
      await showEpisode(ctx, ep);
    }
  } catch (error) {
    console.error(error);
    await ctx.reply("❌ " + error.message);
  }
});

bot.command("episode", async ctx => {
  const url = ctx.message.text.split(/\s+/).slice(1).join(" ").trim();
  if (!url) return ctx.reply("Usage: /episode <WatchHentai episode URL>");

  try {
    await ctx.reply("🔎 Resolving...");
    await showEpisode(ctx, await watchHentai.getEpisode(url, { resolveSources: true }));
  } catch (error) {
    console.error(error);
    await ctx.reply("❌ " + error.message);
  }
});

bot.on("callback_query", async ctx => {
  const data = ctx.callbackQuery.data || "";
  await ctx.answerCbQuery();

  try {
    if (data.startsWith("ep:")) {
      const ep = await watchHentai.getEpisode(unb64(data.slice(3)), { resolveSources: true });
      await showEpisode(ctx, ep);
      return;
    }

    if (data.startsWith("dl:")) {
      const split = data.slice(3).split(":");
      const pageUrl = unb64(split[0]);
      const label = unb64(split.slice(1).join(":"));

      const ep = await watchHentai.getEpisode(pageUrl, { resolveSources: true });
      const source = ep.sources.find(s => s.label === label);
      if (!source) throw new Error(label + " source is unavailable");

      const safe = ep.title.replace(/[^a-z0-9._-]+/gi, "_").slice(0, 80);
      const output = join(downloadDir, safe + "-" + label + ".mp4");
      const status = await ctx.reply("⬇️ Downloading " + label + "...");

      let last = -5;
      await watchHentai.download(source.url, output, async p => {
        if (!p.total || p.percent - last < 5) return;
        last = p.percent;
        await ctx.telegram.editMessageText(
          ctx.chat.id,
          status.message_id,
          undefined,
          "⬇️ " + label + " — " + p.percent.toFixed(1) + "% (" +
          (p.downloaded / 1048576).toFixed(1) + "/" +
          (p.total / 1048576).toFixed(1) + " MB)"
        ).catch(() => {});
      });

      const size = (await stat(output)).size / 1048576;

      if (size > maxUploadMb) {
        await ctx.telegram.editMessageText(
          ctx.chat.id,
          status.message_id,
          undefined,
          "✅ Downloaded " + size.toFixed(1) + " MB.\n⚠️ Exceeds MAX_UPLOAD_MB=" + maxUploadMb + "."
        );
        return;
      }

      await ctx.telegram.editMessageText(ctx.chat.id, status.message_id, undefined, "📤 Uploading...");
      await ctx.replyWithVideo({ source: output }, { caption: ep.title + " — " + label });
      await ctx.telegram.deleteMessage(ctx.chat.id, status.message_id).catch(() => {});
    }
  } catch (error) {
    console.error(error);
    await ctx.reply("❌ " + error.message);
  }
});

bot.catch((error) => console.error("Bot error:", error));
await bot.launch();
console.log("WHBot running");

process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));
