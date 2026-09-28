import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";

const BASE = process.env.WATCHHENTAI_BASE_URL || "https://watchhentai.net";
const UA = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/140.0.0.0 Mobile Safari/537.36";

function reqHeaders(referer = BASE + "/") {
  return {
    "User-Agent": UA,
    "Referer": referer,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
  };
}

async function getText(url, referer = BASE + "/") {
  const res = await fetch(url, { headers: reqHeaders(referer) });
  if (!res.ok) throw new Error("HTTP " + res.status + " for " + url);
  return res.text();
}

function absolute(url) {
  return new URL(url, BASE).href;
}

function clean(value) {
  return value ? value.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim() : null;
}

function decodeMediaUrl(value) {
  value = value.replace(/-/g, "+").replace(/_/g, "/");
  while (value.length % 4) value += "=";
  const decoded = Buffer.from(value, "base64");
  const transformed = Buffer.alloc(decoded.length);

  for (let i = 0; i < decoded.length; i++) {
    transformed[i] = decoded[i] ^ ((13 + (i % 17)) & 255);
  }

  transformed.reverse();
  return Buffer.from(transformed.toString(), "base64").toString("utf8");
}

function extractSources(playerHtml) {
  const match = playerHtml.match(/var\s+whJwSources\s*=\s*(\[[\s\S]*?\]);/);
  if (!match) return [];

  const encoded = JSON.parse(match[1]);
  return encoded.map(source => ({
    label: source.label || "Unknown",
    type: source.type || "video/mp4",
    url: decodeMediaUrl(source.file)
  })).filter(source => source.url);
}

function extractNavigation(html) {
  const result = { previous: null, next: null, series: null };

  const prev = html.match(/class=['"]item item-prev['"][\s\S]*?<a[^>]+href=["']([^"']+)["']/i);
  const next = html.match(/class=['"]item item-next['"][\s\S]*?<a[^>]+href=["']([^"']+)["'][^>]*>/i);
  const series = html.match(/class=['"]item item-all['"][\s\S]*?<a[^>]+href=["']([^"']+)["']/i);

  if (prev) result.previous = absolute(prev[1]);

  if (next && next[1] !== "#") {
    const block = next[0];
    if (!/nonex/i.test(block) && !/aria-disabled=["']true/i.test(block)) {
      result.next = absolute(next[1]);
    }
  }

  if (series) result.series = absolute(series[1]);
  return result;
}

function episodeNumber(title, url) {
  const match = (title + " " + url).match(/episode[-\s]+(\d+)/i);
  return match ? Number(match[1]) : null;
}

async function getEpisode(pageUrl, options = {}) {
  const resolveSources = options.resolveSources !== false;

  if (!/watchhentai\.net\/videos\//i.test(pageUrl)) {
    throw new Error("Not a WatchHentai episode URL");
  }

  const html = await getText(pageUrl);

  const title =
    clean(html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)/i)?.[1]) ||
    clean(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]) ||
    pageUrl;

  const thumbnail =
    html.match(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)/i)?.[1] || null;

  const player =
    html.match(/data-primary-player-url=["']([^"']+)/i)?.[1] ||
    html.match(/<meta[^>]+itemprop=["']contentUrl["'][^>]+content=["']([^"']+)/i)?.[1];

  if (!player) throw new Error("Primary player URL not found");

  let sources = [];
  if (resolveSources) {
    const playerUrl = absolute(player);
    const playerHtml = await getText(playerUrl, pageUrl);
    sources = extractSources(playerHtml);
  }

  return {
    provider: "watchhentai",
    title,
    episode: episodeNumber(title, pageUrl),
    thumbnail: thumbnail ? absolute(thumbnail) : null,
    pageUrl,
    playerUrl: absolute(player),
    navigation: extractNavigation(html),
    sources
  };
}

async function latest(page = 1) {
  const url = page <= 1 ? BASE + "/" : BASE + "/page/" + page + "/";
  const html = await getText(url);
  const found = new Set();
  const result = [];
  const re = /href=["']([^"']*\/videos\/[^"']+)["']/gi;
  let match;

  while ((match = re.exec(html))) {
    const pageUrl = absolute(match[1]).split("#")[0];
    if (pageUrl.replace(/\/$/, "") === BASE + "/videos") continue;
    if (found.has(pageUrl)) continue;
    found.add(pageUrl);
    result.push({ provider: "watchhentai", pageUrl });
  }

  return result;
}

async function download(url, output, onProgress = async () => {}) {
  await mkdir(dirname(output), { recursive: true });

  const res = await fetch(url, {
    headers: { "User-Agent": UA, "Referer": BASE + "/" }
  });

  if (!res.ok) throw new Error("Media HTTP " + res.status);
  if (!res.body) throw new Error("Media response has no body");

  const total = Number(res.headers.get("content-length") || 0);
  const reader = res.body.getReader();
  const file = createWriteStream(output);
  let downloaded = 0;
  const started = Date.now();

  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      file.write(Buffer.from(part.value));
      downloaded += part.value.byteLength;

      await onProgress({
        downloaded,
        total,
        percent: total ? downloaded * 100 / total : 0,
        speed: downloaded / Math.max(1, (Date.now() - started) / 1000)
      });
    }
  } finally {
    file.end();
    await new Promise(resolve => file.once("close", resolve));
  }
}

export const watchHentai = { latest, getEpisode, download };
