import base64
import html as html_lib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from PIL import Image


class ProviderError(Exception):
    pass


class WatchHentai:
    def __init__(self):
        self.base = os.getenv("WATCHHENTAI_BASE_URL", "https://watchhentai.net").rstrip("/")
        self.ua = (
            "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
            "Chrome/140.0.0.0 Mobile Safari/537.36"
        )
        self._series_cache = {}

    def _headers(self, referer=None):
        return {
            "User-Agent": self.ua,
            "Referer": referer or self.base + "/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _get(self, url, referer=None):
        r = requests.get(url, headers=self._headers(referer), timeout=(20, 40))
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code} for {url}")
        return r.text

    def _absolute(self, url):
        return urljoin(self.base + "/", url)

    @staticmethod
    def _clean(value):
        if not value:
            return None
        value = re.sub(r"<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>", " ", value, flags=re.I)
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        value = html_lib.unescape(value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _meta(html, name):
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)',
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(name)}["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.I)
            if m:
                return WatchHentai._clean(m.group(1))
        return None

    @staticmethod
    def _decode(value):
        value = value.replace("-", "+").replace("_", "/")
        value += "=" * (-len(value) % 4)
        decoded = base64.b64decode(value)
        decoded = bytes(
            byte ^ ((13 + (i % 17)) & 255)
            for i, byte in enumerate(decoded)
        )[::-1]
        return base64.b64decode(decoded).decode("utf-8")

    def _sources(self, html):
        m = re.search(r"var\s+whJwSources\s*=\s*(\[[\s\S]*?\]);", html)
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        out = []
        for source in data:
            try:
                out.append({
                    "label": source.get("label", "Unknown"),
                    "type": source.get("type", "video/mp4"),
                    "url": self._decode(source["file"]),
                })
            except Exception:
                continue
        return out

    def _navigation(self, html):
        out = {"previous": None, "next": None, "series": None}
        patterns = {
            "previous": r'class=["\'][^"\']*item-prev[^"\']*["\'][\s\S]*?<a[^>]+href=["\']([^"\']+)',
            "series": r'class=["\'][^"\']*item-all[^"\']*["\'][\s\S]*?<a[^>]+href=["\']([^"\']+)',
            "next": r'class=["\'][^"\']*item-next[^"\']*["\'][\s\S]*?<a[^>]+href=["\']([^"\']+)',
        }

        for key, pattern in patterns.items():
            m = re.search(pattern, html, re.I)
            if not m:
                continue
            href = m.group(1)
            if href == "#" or (key == "next" and "nonex" in m.group(0).lower()):
                continue
            out[key] = self._absolute(href)

        return out

    def _synopsis(self, html):
        for marker in (
            'itemprop="description"',
            "itemprop='description'",
        ):
            m = re.search(
                rf'<[^>]+{re.escape(marker)}[^>]*>([\s\S]*?)</',
                html,
                re.I,
            )
            if m:
                text = self._clean(m.group(1))
                if text and len(text) > 30:
                    return text

        for cls in ("description", "desc", "entry-content", "post-content"):
            pattern = (
                rf'<(?:div|p|section)[^>]+class=["\'][^"\']*\b{cls}\b[^"\']*["\'][^>]*>'
                rf'([\s\S]*?)</(?:div|p|section)>'
            )
            candidates = []
            for m in re.finditer(pattern, html, re.I):
                text = self._clean(m.group(1))
                if text and len(text) > 30:
                    candidates.append(text)
            if candidates:
                return max(candidates, key=len)

        return self._meta(html, "og:description") or self._meta(html, "description")

    def _episode_links(self, html):
        seen = set()
        out = []
        for m in re.finditer(r'href=["\']([^"\']*/videos/[^"\']+)["\']', html, re.I):
            url = self._absolute(m.group(1)).split("#")[0].rstrip("/")
            if url == self.base + "/videos" or url in seen:
                continue
            seen.add(url)
            out.append({"provider": "watchhentai", "page_url": url})
        return out

    def _series_links(self, html):
        seen = set()
        out = []
        for m in re.finditer(r'href=["\']([^"\']*/series/[^"\']+)["\']', html, re.I):
            url = self._absolute(m.group(1)).split("#")[0].rstrip("/")
            if (
                url.rstrip("/") == self.base + "/series"
                or re.search(r"/series/page/\d+/?$", url, re.I)
                or url in seen
            ):
                continue
            seen.add(url)
            out.append({"provider": "watchhentai", "series_url": url})
        return out

    def _series_page(self, page):
        url = self._absolute(page)
        html = self._get(url)

        title = self._meta(html, "og:title")
        if title:
            title = re.sub(r"\s*-\s*Watch Hentai.*$", "", title, flags=re.I).strip()
        if not title:
            m = re.search(r'<h1[^>]*>([^<]+)</h1>', html, re.I)
            title = self._clean(m.group(1)) if m else url

        thumb = self._meta(html, "og:image")

        # The series page exposes the episode list and an explicit episode
        # count in the page metadata. Prefer that count, then fall back to
        # distinct episode links if the metadata is unavailable.
        total = None
        m = re.search(r'([0-9]+)\s+Episodes', html, re.I)
        if m:
            total = int(m.group(1))
        episode_links = self._episode_links(html)
        if total is None:
            total = len(episode_links)

        return {
            "provider": "watchhentai",
            "series_url": url,
            "name": title,
            "thumbnail": self._absolute(thumb) if thumb else None,
            "total_episodes": total,
        }

    def series_latest(self, page=1):
        # Series pagination is 1-based:
        # 1 -> /series/
        # 2 -> /series/page/2/
        # 3 -> /series/page/3/
        if page < 1:
            raise ProviderError("Series page number must be 1 or greater")
        url = self.base + "/series/" if page == 1 else f"{self.base}/series/page/{page}/"
        html = self._get(url)
        return self._series_links(html)

    def get_series(self, series_url):
        return self._series_page(series_url)

    def series_episodes(self, series_url):
        if not series_url:
            return []
        series_url = series_url.rstrip("/")
        if series_url in self._series_cache:
            return self._series_cache[series_url]

        html = self._get(series_url, self.base + "/")
        links = self._episode_links(html)
        self._series_cache[series_url] = links
        return links

    def series_total(self, series_url):
        try:
            return len(self.series_episodes(series_url))
        except Exception:
            return 0

    def get_episode(self, page_url, resolve_sources=True):
        if not re.search(r"watchhentai\.net/videos/", page_url, re.I):
            raise ProviderError("Not a WatchHentai episode URL")

        html = self._get(page_url)

        title = self._meta(html, "og:title")
        if not title:
            m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
            title = self._clean(m.group(1)) if m else page_url

        thumb = self._meta(html, "og:image")
        synopsis = self._synopsis(html)

        m = re.search(r'data-primary-player-url=["\']([^"\']+)', html, re.I)
        if not m:
            raise ProviderError("Primary player URL not found")

        player = self._absolute(m.group(1))
        sources = self._sources(self._get(player, page_url)) if resolve_sources else []

        em = re.search(r"episode[-\s]+(\d+)", title + " " + page_url, re.I)
        navigation = self._navigation(html)

        return {
            "provider": "watchhentai",
            "title": title,
            "episode": int(em.group(1)) if em else None,
            "synopsis": synopsis,
            "thumbnail": self._absolute(thumb) if thumb else None,
            "page_url": page_url,
            "player_url": player,
            "navigation": navigation,
            "series_total": self.series_total(navigation.get("series")),
            "sources": sources,
        }

    def latest(self, page=0):
        # 0 is /videos/; every other number maps directly to /videos/page/N/.
        url = self.base + "/videos/" if page == 0 else f"{self.base}/videos/page/{page}/"
        html = self._get(url)
        return self._episode_links(html)

    def search(self, query, pages=3):
        needle = query.lower().strip()
        if not needle:
            return []

        found = []
        seen = set()
        for page in range(1, pages + 1):
            for item in self.latest(page):
                slug = item["page_url"].rstrip("/").rsplit("/", 1)[-1]
                text = re.sub(r"[-_]+", " ", slug).lower()
                if needle in text and item["page_url"] not in seen:
                    seen.add(item["page_url"])
                    found.append(item)

        return found[:20]

    def download_thumbnail(self, url, output):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        response = requests.get(
            url,
            headers=self._headers(),
            timeout=(20, 40),
        )
        if not response.ok:
            raise ProviderError("Thumbnail HTTP {}".format(response.status_code))

        raw = output.with_suffix(".source")
        raw.write_bytes(response.content)
        try:
            with Image.open(raw) as image:
                image = image.convert("RGB")
                image.thumbnail((320, 320), Image.Resampling.LANCZOS)
                image.save(output, "JPEG", quality=85, optimize=True)
        finally:
            raw.unlink(missing_ok=True)

        if output.stat().st_size >= 200 * 1024:
            with Image.open(output) as image:
                image.save(output, "JPEG", quality=70, optimize=True)

        if output.stat().st_size >= 200 * 1024:
            raise ProviderError("Thumbnail could not be reduced below Telegram's 200 KB limit")

        return output

    def download(self, url, output, progress=None, referer=None):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        headers = self._headers(referer or self.base + "/")
        headers["Accept"] = "video/mp4,video/*;q=0.9,*/*;q=0.8"
        with requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(30, 120),
        ) as response:
            if not response.ok:
                raise ProviderError(f"Media HTTP {response.status_code}")

            content_type = response.headers.get("content-type", "").lower()
            if "video" not in content_type and "octet-stream" not in content_type:
                raise ProviderError(
                    f"Media response is not video: {content_type or 'unknown'}"
                )

            total = int(response.headers.get("content-length") or 0)
            current = 0

            try:
                with output.open("wb") as file:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        file.write(chunk)
                        current += len(chunk)
                        if progress:
                            progress(current, total, started)
            except Exception:
                output.unlink(missing_ok=True)
                raise
