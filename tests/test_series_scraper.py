"""Scraper-only test for WatchHentai series extraction.

This test intentionally stops before episode/source resolution.
It does not call get_episode(), download(), GoFile, or Telegram.
"""

import os
import re
import sys
from pathlib import Path

# Make the repository root importable when this script is run as tests/test_series_scraper.py.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers.watchhentai import ProviderError, WatchHentai


def main():
    series_url = os.environ.get("SERIES_URL", "").strip()
    if not series_url:
        print("ERROR: SERIES_URL is not set.", file=sys.stderr)
        return 2

    if not re.match(r"^https?://watchhentai\.net/series/[^\s]+/?$", series_url, re.I):
        print("ERROR: SERIES_URL must be a WatchHentai series URL.", file=sys.stderr)
        return 2

    provider = WatchHentai()

    print("=== WHBot Series Scraper Test ===")
    print(f"Series URL: {series_url}")
    print()
    print("[1/2] Fetching series metadata...")
    series = provider.get_series(series_url)

    canonical_url = series.get("series_url") or series_url
    print(f"Series: {series.get('name') or 'Unknown'}")
    print(f"Thumbnail: {series.get('thumbnail') or 'None'}")
    print(f"Metadata episode count: {series.get('total_episodes', 0)}")
    print()
    print("[2/2] Extracting episode page URLs...")
    episodes = provider.series_episodes(canonical_url)

    if not episodes:
        raise ProviderError("No episode URLs were extracted from the series page.")

    print(f"Extracted episode count: {len(episodes)}")
    print()
    print("Episode URLs:")
    for index, episode in enumerate(episodes, 1):
        print(f"{index}. {episode['page_url']}")

    result_file = os.environ.get("SERIES_RESULT_FILE", "series-result.txt")
    with open(result_file, "w", encoding="utf-8") as output:
        output.write("=== WHBot Series Scraper Result ===\\n")
        output.write(f"Series URL: {series_url}\\n")
        output.write(f"Canonical URL: {canonical_url}\\n")
        output.write(f"Series: {series.get('name') or 'Unknown'}\\n")
        output.write(f"Thumbnail: {series.get('thumbnail') or 'None'}\\n")
        output.write(f"Metadata episode count: {series.get('total_episodes', 0)}\\n")
        output.write(f"Extracted episode count: {len(episodes)}\\n\\n")
        output.write("Episode URLs:\\n")
        for index, episode in enumerate(episodes, 1):
            output.write(f"{index}. {episode['page_url']}\\n")
        output.write("\\n=== Scraper-only test passed ===\\n")
        output.write("No episode source resolution was performed.\\n")
        output.write("No video was downloaded.\\n")
        output.write("No GoFile upload was performed.\\n")
        output.write("No Telegram action was performed.\\n")
    print(f"Result saved to: {result_file}")

