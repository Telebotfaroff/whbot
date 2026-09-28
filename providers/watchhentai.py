import base64
import html as html_lib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests


class ProviderError(Exception):
    pass


class WatchHentai:
    def __init__(self):
        self.base = os.getenv("WATCHHENTAI_BASE_URL", "https://watchhentai.net").rstrip("/")
        self.ua = (
            "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
            "Chrome/140.0.0.0 Mobile Safari/537.36"
        )

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
                out.append(
                    {
                        "label": source.get("label", "Unknown"),
                        "type": source.get("type", "video/mp4"),
                        "url": self._decode(source["file"]),
                    }
                )
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
        # Prefer a real structured description when the page provides one.
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

        # Some theme versions place the synopsis in description/desc blocks.
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
                # Avoid generic site copy when a longer post description exists.
                return max(candidates, key=len)

        return self._meta(html, "og:description") or self._meta(html, "description")

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

        return {
            "provider": "watchhentai",
            "title": title,
            "episode": int(em.group(1)) if em else None,
            "synopsis": synopsis,
            "thumbnail": self._absolute(thumb) if thumb else None,
            "page_url": page_url,
            "player_url": player,
            "navigation": self._navigation(html),
            "sources": sources,
        }

    def latest(self, page=1):
        url = self.base + "/" if page <= 1 else f"{self.base}/page/{page}/"
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

    def download(self, url, output, progress=None):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        with requests.get(
            url,
            headers=self._headers(self.base + "/"),
            stream=True,
            timeout=(30, 120),
        ) as response:
            if not response.ok:
                raise ProviderError(f"Media HTTP {response.status_code}")

            content_type = response.headers.get("content-type", "").lower()
            if "video" not in content_type and "octet-stream" not in content_type:
                raise ProviderError(f"Media response is not video: {content_type or 'unknown'}")

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
