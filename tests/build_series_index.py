"""Build a durable, deduplicated WatchHentai series index.

The crawler indexes series metadata and episode page URLs only. It does not
resolve media sources or download episodes.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers.watchhentai import WatchHentai, ProviderError


BASE_URL = os.getenv("WATCHHENTAI_BASE_URL", "https://watchhentai.net").rstrip("/")
OUTPUT_ROOT = Path(os.getenv("SERIES_INDEX_OUTPUT_ROOT", str(ROOT / "whdata")))
LOG_PATH = OUTPUT_ROOT / "scrape-log.json"
STATE_PATH = Path(os.getenv("SERIES_INDEX_STATE_PATH", str(OUTPUT_ROOT / ".scrape-state.json")))
MAX_PAGES = max(1, int(os.getenv("SERIES_INDEX_PAGES", "42")))
MAX_EMPTY_PAGES = max(1, int(os.getenv("SERIES_INDEX_MAX_EMPTY_PAGES", "2")))
PAGE_RETRIES = max(1, int(os.getenv("SERIES_INDEX_PAGE_RETRIES", "3")))
REQUEST_DELAY = max(0.0, float(os.getenv("SERIES_INDEX_DELAY", "0.25")))


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url, base_url=BASE_URL):
    """Normalize an internal URL so equivalent links share one dedupe key."""
    if not url:
        return None
    absolute = urljoin(base_url.rstrip("/") + "/", str(url).strip())
    parsed = urlsplit(absolute)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host if port is None or (parsed.scheme.lower(), port) in {
        ("http", 80), ("https", 443)
    } else f"{host}:{port}"
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        path = "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def is_series_detail(url):
    normalized = canonical_url(url)
    if not normalized:
        return False
    parsed = urlsplit(normalized)
    base = urlsplit(BASE_URL)
    return (
        parsed.hostname == (base.hostname or "").lower()
        and bool(re.fullmatch(r"/series/[^/]+", parsed.path, re.I))
    )


def clean_text(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def safe_filename(title, fallback_index):
    title = clean_text(title)
    title = re.sub(r'[<>:"/\\|?*]', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    return title[:180] or f"post-{fallback_index}"


def write_scrape_log(
    *,
    status,
    page=None,
    post_title=None,
    post_url=None,
    posts_scraped=0,
    episodes_scraped=0,
    error=None,
):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "updated_at": now_utc(),
        "last_scraped_page": page,
        "last_scraped_post": post_title,
        "last_scraped_post_url": post_url,
        "posts_scraped": posts_scraped,
        "episodes_scraped": episodes_scraped,
        "pages_requested": MAX_PAGES,
        "max_consecutive_empty_pages": MAX_EMPTY_PAGES,
        "error": error,
    }
    temporary = LOG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(LOG_PATH)


def read_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(*, status, next_page, completed_pages, error=None, consecutive_empty_pages=0):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "next_page": next_page,
        "completed_pages": sorted(set(completed_pages)),
        "consecutive_empty_pages": consecutive_empty_pages,
        "updated_at": now_utc(),
        "error": error,
    }
    temporary = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def page_url(page):
    return f"{BASE_URL}/series/" if page == 1 else f"{BASE_URL}/series/page/{page}/"


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
    for pattern in (
        r"(\d+)\s*(?:episodes?|eps?)(?:\b|$)",
        r"(?:episodes?|eps?)\s*[:\-]?\s*(\d+)",
    ):
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


def extract_thumbnail(card):
    image = card.find("img")
    if not image:
        return None
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        value = clean_text(image.get(attr))
        if value:
            return urljoin(BASE_URL + "/", value)
    return None


def _looks_like_block_page(html):
    sample = (html or "").lower()
    markers = (
        "cf-chl-",
        "challenge-platform",
        "verify you are human",
        "checking your browser",
        "captcha",
        "access denied",
        "just a moment...",
    )
    return any(marker in sample for marker in markers)


def scrape_listing(provider, page, progress):
    source_page = page_url(page)
    print(f"[index] page {page}: {source_page}", flush=True)
    write_scrape_log(
        status="running",
        page=page,
        posts_scraped=progress["posts"],
        episodes_scraped=progress["episodes"],
    )

    html = provider._get(source_page, BASE_URL + "/")
    soup = BeautifulSoup(html, "html.parser")
    records = []
    seen_on_page = set()

    for anchor in soup.find_all("a", href=True):
        detail_url = canonical_url(anchor["href"], BASE_URL)
        if not is_series_detail(detail_url) or detail_url in seen_on_page:
            continue

        card = find_card(anchor)
        title = extract_title(anchor, card) or detail_url.rsplit("/", 1)[-1]
        thumbnail = extract_thumbnail(card)
        print(f"[index] page {page}: {title} -> {detail_url}", flush=True)

        episodes = provider.series_episodes(detail_url)
        episode_records = []
        seen_episodes = set()
        for episode in episodes:
            episode_url = canonical_url(episode.get("page_url"), BASE_URL)
            if not episode_url or episode_url in seen_episodes:
                continue
            seen_episodes.add(episode_url)
            episode_records.append({"episode": len(episode_records) + 1, "url": episode_url})

        total_episodes = len(episode_records) or extract_total_episodes(
            card.get_text(" ", strip=True)
        )
        seen_on_page.add(detail_url)
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

    if not records and _looks_like_block_page(html):
        raise ProviderError(f"Page {page} returned a challenge/block page; not treating it as end-of-pagination")

    # Update counters only after the entire listing page succeeds. A retry after
    # a partial page failure must not inflate the progress totals.
    progress["posts"] += len(records)
    progress["episodes"] += sum(len(record["episodes"]) for record in records)
    if records:
        progress["last_page"] = page
        progress["last_post"] = records[-1]["post_title"]
        progress["last_post_url"] = records[-1]["post_url"]
    write_scrape_log(
        status="running",
        page=page,
        post_title=progress["last_post"],
        post_url=progress["last_post_url"],
        posts_scraped=progress["posts"],
        episodes_scraped=progress["episodes"],
    )
    return records


def existing_urls_before(start_page):
    """Load URLs from already completed earlier pages when resuming a crawl."""
    found = set()
    for directory in OUTPUT_ROOT.glob("page *"):
        match = re.fullmatch(r"page\s+(\d+)", directory.name)
        if not match or int(match.group(1)) >= start_page:
            continue
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                url = canonical_url(record.get("post_url") or record.get("link"))
                if url:
                    found.add(url)
            except (OSError, json.JSONDecodeError):
                print(f"[index] warning: ignoring invalid existing JSON: {path}", flush=True)
    return found


def write_page(page, records):
    """Replace a page only after every new JSON file has been written successfully."""
    directory = OUTPUT_ROOT / f"page {page}"
    directory.mkdir(parents=True, exist_ok=True)

    used = {}
    staged = []
    try:
        for index, record in enumerate(records, 1):
            base = safe_filename(record.get("post_title"), index)
            used[base] = used.get(base, 0) + 1
            filename = base if used[base] == 1 else f"{base}-{used[base]}"
            target = directory / f"{filename}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            staged.append((temporary, target))

        new_names = {target.name for _, target in staged}
        for temporary, target in staged:
            temporary.replace(target)

        for old_file in directory.glob("*.json"):
            if old_file.name not in new_names:
                old_file.unlink()
    except Exception:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise


def deduplicate_existing_index():
    """Remove legacy duplicate JSON entries left by older page-scoped crawls."""
    seen = set()
    removed = 0
    directories = []
    for directory in OUTPUT_ROOT.glob("page *"):
        match = re.fullmatch(r"page\s+(\d+)", directory.name)
        if match:
            directories.append((int(match.group(1)), directory))
    for _, directory in sorted(directories):
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Keep malformed files for manual inspection; do not delete data
                # that cannot be safely identified.
                continue
            key = canonical_url(record.get("post_url") or record.get("link"))
            if not key:
                continue
            if key in seen:
                path.unlink()
                removed += 1
            else:
                seen.add(key)
    if removed:
        print(f"[index] removed {removed} legacy duplicate JSON record(s)", flush=True)
    return removed


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    provider = WatchHentai()
    progress = {
        "posts": 0,
        "episodes": 0,
        "last_page": None,
        "last_post": None,
        "last_post_url": None,
    }
    state = read_state()
    resume = os.getenv("SERIES_INDEX_RESUME", "1").strip().lower() not in {"0", "false", "no"}
    start_page = 1
    if resume and state.get("status") in {"running", "failed"}:
        candidate = state.get("next_page")
        if isinstance(candidate, int) and 1 <= candidate <= MAX_PAGES:
            start_page = candidate
            print(f"[index] resuming at page {start_page}", flush=True)

    completed_pages = set(
        p for p in state.get("completed_pages", [])
        if isinstance(p, int) and 1 <= p < start_page
    )
    seen_urls = existing_urls_before(start_page)
    empty_pages = state.get("consecutive_empty_pages", 0) if start_page > 1 else 0
    if not isinstance(empty_pages, int) or empty_pages < 0:
        empty_pages = 0
    write_state(
        status="running", next_page=start_page, completed_pages=completed_pages,
        consecutive_empty_pages=empty_pages,
    )
    write_scrape_log(status="started", page=start_page)

    page = start_page
    try:
        while page <= MAX_PAGES:
            records = None
            last_error = None
            for attempt in range(1, PAGE_RETRIES + 1):
                try:
                    records = scrape_listing(provider, page, progress)
                    break
                except Exception as exc:
                    last_error = exc
                    print(f"[index] page {page} attempt {attempt}/{PAGE_RETRIES} failed: {exc}", flush=True)
                    if attempt < PAGE_RETRIES:
                        time.sleep(min(2 ** attempt, 10))
            if records is None:
                raise ProviderError(f"Page {page} failed after {PAGE_RETRIES} attempts: {last_error}")

            if not records:
                empty_pages += 1
                print(f"[index] page {page}: valid empty listing ({empty_pages}/{MAX_EMPTY_PAGES}); existing files preserved", flush=True)
                completed_pages.add(page)
                write_state(
                    status="running", next_page=page + 1, completed_pages=completed_pages,
                    consecutive_empty_pages=empty_pages,
                )
                if empty_pages >= MAX_EMPTY_PAGES:
                    break
                page += 1
                continue

            empty_pages = 0
            unique_records = []
            for record in records:
                key = canonical_url(record["post_url"])
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                unique_records.append(record)

            write_page(page, unique_records)
            completed_pages.add(page)
            write_state(
                status="running", next_page=page + 1, completed_pages=completed_pages,
                consecutive_empty_pages=0,
            )
            print(
                f"[index] page {page}: {len(records)} discovered, "
                f"{len(unique_records)} unique records",
                flush=True,
            )
            if REQUEST_DELAY:
                time.sleep(REQUEST_DELAY)
            page += 1

        deduplicate_existing_index()
        write_state(status="complete", next_page=1, completed_pages=[], consecutive_empty_pages=0)
        write_scrape_log(
            status="complete",
            page=progress["last_page"],
            post_title=progress["last_post"],
            post_url=progress["last_post_url"],
            posts_scraped=progress["posts"],
            episodes_scraped=progress["episodes"],
        )
        print(
            f"[index] complete: {progress['posts']} discovered records, "
            f"{progress['episodes']} episodes indexed",
            flush=True,
        )
        return 0
    except Exception as exc:
        write_state(
            status="failed", next_page=page, completed_pages=completed_pages,
            error=str(exc), consecutive_empty_pages=empty_pages,
        )
        write_scrape_log(
            status="failed",
            page=progress["last_page"] or page,
            post_title=progress["last_post"],
            post_url=progress["last_post_url"],
            posts_scraped=progress["posts"],
            episodes_scraped=progress["episodes"],
            error=str(exc),
        )
        raise ProviderError(f"Series index build failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
