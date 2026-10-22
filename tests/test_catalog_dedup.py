import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler import catalog


class FakeSeriesProvider:
    def __init__(self):
        self.series_calls = []

    def series_latest(self, page):
        pages = {
            1: [
                {"series_url": "https://watchhentai.net/series/alpha"},
                {"series_url": "https://watchhentai.net/series/beta"},
            ],
            2: [
                {"series_url": "https://watchhentai.net/series/alpha"},
                {"series_url": "https://watchhentai.net/series/gamma"},
            ],
        }
        return pages.get(page, [])

    def get_series(self, url):
        self.series_calls.append(url)
        return {
            "series_url": url,
            "name": url.rstrip("/").rsplit("/", 1)[-1],
            "total_episodes": 0,
        }

    def series_episodes(self, url):
        return []


class FakeEpisodeProvider:
    def __init__(self):
        self.episode_calls = []

    def latest(self, page):
        if page in (0, 1):
            return [{"page_url": "https://watchhentai.net/videos/alpha-1"}]
        return []

    def get_episode(self, url, resolve_sources=False):
        self.episode_calls.append(url)
        return {
            "page_url": url,
            "title": "Episode 1",
            "episode": 1,
            "synopsis": None,
            "thumbnail": None,
            "navigation": {},
            "player_url": None,
            "sources": [],
        }


class CatalogDedupTests(unittest.TestCase):
    def test_series_urls_repeated_on_later_pages_are_processed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "catalog.sqlite"
            connection = catalog.init_db(db_path)
            provider = FakeSeriesProvider()
            with (
                patch.object(catalog, "init_db", return_value=connection),
                patch.object(catalog, "WatchHentai", return_value=provider),
            ):
                result = catalog.crawl_series(1, 2, delay=0)

            self.assertEqual(result["processed"], 3)
            self.assertEqual(len(provider.series_calls), 3)
            check = sqlite3.connect(db_path)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM series").fetchone()[0], 3)
            finally:
                check.close()

    def test_episode_urls_repeated_on_later_pages_are_processed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "catalog.sqlite"
            connection = catalog.init_db(db_path)
            provider = FakeEpisodeProvider()
            with (
                patch.object(catalog, "init_db", return_value=connection),
                patch.object(catalog, "WatchHentai", return_value=provider),
            ):
                result = catalog.crawl(0, 1, delay=0)

            self.assertEqual(result["processed"], 1)
            self.assertEqual(provider.episode_calls, ["https://watchhentai.net/videos/alpha-1"])
            check = sqlite3.connect(db_path)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
