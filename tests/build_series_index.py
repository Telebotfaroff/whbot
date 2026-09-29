"""Build the WatchHentai series index from listing pages only.

This crawler first discovers series posts from listing pages, then opens each
series post to extract its individual episode page URLs. It does NOT resolve
media sources and does NOT download episodes.
"""

import json
import os
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers.watchhentai import WatchHentai, ProviderError


BASE_URL = os.getenv("WATCHHENTAI_BASE_URL", "https://watchhentai.net").rstrip("/")
OUTPUT_ROOT = ROOT / "whdata"
MAX_PAGES = int(os.getenv("SERIES_INDEX_PAGES", "42"))


def page_url(page):
    return f"{BASE_URL}/series/" if page == 1 else f"{BASE_URL}/series/page/{page}/"


def clean_text(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def safe_filename(title, fallback_index):
    title = clean_text(title)
    title = re.sub(r'[<>:"/\\|?*]', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    return title[:180] or f"post-{fallback_index}"


def is_series_detail(url):
    parsed = urlsplit(url)
    base = urlsplit(BASE_URL)
    return (
        parsed.netloc.lower() == base.netloc.lower()
        and bool(re.fullmatch(r"/series/[^/]+", parsed.path.rstrip("/"), re.I))
    )


def find_card(anchor):
    current = anchor
    for _ in range(6):
        current = current.parent
        if current is None:
            return anchor
        name = current.name or ""
        classes = " ".join(current.get("class", []))
        marker = f"{name} {classes}".lower()
        if name in {"article", "li"} or any(
            word in marker for word in ("series", "post", "item", "card", "archive")
        ):
            return current
    return anchor.parent or anchor


def extract_total_episodes(text):
    text = clean_text(text)
    patterns = [
        r"(\d+)\s*(?:episodes?|eps?)(?:\b|$)",
        r"(?:episodes?|eps?)\s*[:\-]?\s*(\d+)",
        r"(\d+)\s*episode\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return 0


def extract_title(anchor, card):
    for attr in ("title", "aria-label"):
        value = clean_text(anchor.get(attr))
        if value:
            return value

    image = card.find("img")
    if image:
        for attr in ("alt", "title"):
            value = clean_text(image.get(attr))
            if value:
                return value

    heading = card.find(re.compile(r"^h[1-6]$"))
    if heading:
        value = clean_text(heading.get_text(" ", strip=True))
        if value:
            return value

    return clean_text(anchor.get_text(" ", strip=True))


def extract_thumbnail(card, anchor):
    image = card.find("img")
    if not image:
        return None

    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        value = clean_text(image.get(attr))
        if value:
            return urljoin(BASE_URL + "/", value)
    return None


def scrape_listing(provider, page):
    source_page = page_url(page)
    print(f"[index] page {page}: {source_page}", flush=True)
    html = provider._get(source_page, BASE_URL + "/")
    soup = BeautifulSoup(html, "html.parser")

    records = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = urljoin(BASE_URL + "/", anchor["href"].split("#", 1)[0])
        parsed = urlsplit(href)
        detail_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if not is_series_detail(detail_url):
            continue
        if detail_url in seen:
            continue

        card = find_card(anchor)
        title = extract_title(anchor, card)
        thumbnail = extract_thumbnail(card, anchor)
        # Open the series post and extract its individual episode page URLs.
        # This does not resolve media sources or download any episode.
        episodes = provider.series_episodes(detail_url)
        episode_records = [
            {
                "episode": index,
                "url": episode["page_url"],
            }
            for index, episode in enumerate(episodes, 1)
        ]
        total_episodes = len(episode_records) or extract_total_episodes(
            card.get_text(" ", strip=True)
        )

        seen.add(detail_url)
        records.append({
            "post_title": title,
            "post_url": detail_url,
            "link": detail_url,
            "thumbnail": thumbnail,
            "total_episodes": total_episodes,
            "episodes": episode_records,
            "source_page": source_page,
            "page": page,
        })

    return records


def write_page(page, records):
    directory = OUTPUT_ROOT / f"page {page}"
    directory.mkdir(parents=True, exist_ok=True)

    # Remove stale JSON files for this page before rewriting it.
    for old_file in directory.glob("*.json"):
        old_file.unlink()

    used = {}
    for index, record in enumerate(records, 1):
        base = safe_filename(record["post_title"], index)
        used[base] = used.get(base, 0) + 1
        filename = base if used[base] == 1 else f"{base}-{used[base]}"
        path = directory / f"{filename}.json"
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    provider = WatchHentai()

    total_records = 0
    for page in range(1, MAX_PAGES + 1):
        try:
            records = scrape_listing(provider, page)
            write_page(page, records)
            total_records += len(records)
            print(f"[index] page {page}: {len(records)} series posts", flush=True)
        except Exception as exc:
            raise ProviderError(f"Failed to scrape listing page {page}: {exc}") from exc

    print(f"[index] complete: {total_records} records across {MAX_PAGES} pages", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
