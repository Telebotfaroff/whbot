import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import build_series_index as indexer
from providers.watchhentai import ProviderError


def listing(*slugs):
    links = []
    for slug in slugs:
        title = slug.replace("-", " ").title()
        links.append(
            '<div class="series-card"><a href="/series/{0}/">{1}</a></div>'.format(
                slug, title
            )
        )
    return "<html><body><h1>Series</h1>{}</body></html>".format("".join(links))


class FakeProvider:
    def _get(self, url, referer=None):
        if "/series/page/3/" in url or "/series/page/4/" in url:
            return "<html><body><h1>Series archive</h1><p>No entries</p></body></html>"
        if url.endswith("/series/"):
            return listing("alpha", "beta")
        if "/series/page/2/" in url:
            return listing("beta", "gamma")
        raise AssertionError("Unexpected URL: " + url)

    def series_episodes(self, series_url):
        slug = series_url.rstrip("/").rsplit("/", 1)[-1]
        if slug == "alpha":
            return [
                {"page_url": "https://watchhentai.net/videos/alpha-1/"},
                {"page_url": "https://watchhentai.net/videos/alpha-1/?ref=duplicate#player"},
            ]
        return [{"page_url": "https://watchhentai.net/videos/{}-1/".format(slug)}]


class SeriesIndexTests(unittest.TestCase):
    def test_canonical_url_strips_tracking_query_fragment_and_trailing_slash(self):
        self.assertEqual(
            indexer.canonical_url("HTTPS://WATCHHENTAI.NET/series/Alpha/?utm=x#top"),
            "https://watchhentai.net/series/Alpha",
        )

    def test_only_series_detail_urls_are_accepted(self):
        self.assertTrue(indexer.is_series_detail("https://watchhentai.net/series/alpha/"))
        self.assertFalse(indexer.is_series_detail("https://watchhentai.net/series/"))
        self.assertFalse(indexer.is_series_detail("https://watchhentai.net/series/page/2/"))
        self.assertFalse(indexer.is_series_detail("https://other.example/series/alpha/"))

    def test_crawl_deduplicates_across_pages_and_stops_after_empty_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(indexer, "OUTPUT_ROOT", root / "whdata"),
                patch.object(indexer, "LOG_PATH", root / "whdata" / "scrape-log.json"),
                patch.object(indexer, "STATE_PATH", root / "state.json"),
                patch.object(indexer, "MAX_PAGES", 8),
                patch.object(indexer, "MAX_EMPTY_PAGES", 2),
                patch.object(indexer, "PAGE_RETRIES", 1),
                patch.object(indexer, "REQUEST_DELAY", 0),
                patch.object(indexer, "WatchHentai", FakeProvider),
            ):
                self.assertEqual(indexer.main(), 0)

            records = []
            for path in (root / "whdata").glob("page */*.json"):
                records.append(json.loads(path.read_text(encoding="utf-8")))
            urls = [record["post_url"] for record in records]
            self.assertEqual(len(urls), 3)
            self.assertEqual(len(set(urls)), 3)
            self.assertIn("https://watchhentai.net/series/alpha", urls)
            self.assertIn("https://watchhentai.net/series/beta", urls)
            self.assertIn("https://watchhentai.net/series/gamma", urls)

            alpha = next(record for record in records if record["post_url"].endswith("/alpha"))
            self.assertEqual(len(alpha["episodes"]), 1)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "complete")

    def test_failed_write_does_not_delete_existing_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(indexer, "OUTPUT_ROOT", root):
                directory = root / "page 1"
                directory.mkdir()
                existing = directory / "Existing.json"
                existing.write_text('{"post_url":"https://watchhentai.net/series/existing"}', encoding="utf-8")
                with self.assertRaises(TypeError):
                    indexer.write_page(1, [{
                        "post_title": "New",
                        "invalid": object(),
                    }])
                self.assertTrue(existing.exists())
                self.assertIn("existing", existing.read_text(encoding="utf-8"))

    def test_challenge_page_is_not_treated_as_end_of_pagination(self):
        class ChallengeProvider:
            def _get(self, url, referer=None):
                return "<html><title>Just a moment...</title><body>Verify you are human</body></html>"

            def series_episodes(self, series_url):
                return []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(indexer, "OUTPUT_ROOT", root),
                patch.object(indexer, "LOG_PATH", root / "scrape-log.json"),
            ):
                with self.assertRaises(ProviderError):
                    indexer.scrape_listing(
                        ChallengeProvider(),
                        1,
                        {"posts": 0, "episodes": 0, "last_page": None, "last_post": None, "last_post_url": None},
                    )


if __name__ == "__main__":
    unittest.main()
