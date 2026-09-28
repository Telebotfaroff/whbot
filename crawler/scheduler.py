import argparse
import time

from crawler.catalog import init_db, save_episode
from publisher import publish_pending
from providers.watchhentai import WatchHentai

def check_once(pages=1):
    provider = WatchHentai()
    con = init_db()
    added = 0
    try:
        for page in range(1, pages + 1):
            for item in provider.latest(page):
                if con.execute("SELECT 1 FROM posts WHERE url=?", (item["page_url"],)).fetchone():
                    continue
                ep = provider.get_episode(item["page_url"], True)
                save_episode(con, ep)
                con.commit()
                added += 1
                print("[new] {}".format(ep["title"]))
    finally:
        con.close()
    return added

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=1800)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--publish-limit", type=int, default=20)
    args = parser.parse_args()
    while True:
        try:
            print("Checking first {} page(s)...".format(args.pages))
            print("Added {} new post(s).".format(check_once(args.pages)))
            print("Published {} pending post(s).".format(publish_pending(args.publish_limit)))
        except Exception as exc:
            print("Check failed: {}".format(exc))
        time.sleep(max(args.interval, 60))
