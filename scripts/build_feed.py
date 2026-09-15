#!/usr/bin/env python3

import json
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

FEED_BASE = "https://mehlmanmedical.com/category/free-video-qbank/feed/"
SITE_BASE = "https://mehlmanmedical.com/category/free-video-qbank/"

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "posts.json"
FEED_FILE = ROOT / "docs" / "feed.xml"

USER_AGENT = "Mehlman-Qbank-RSS/2.0 (+personal RSS archive)"
TIMEOUT = 30
DELAY = 0.4

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def feed_url(page):
    if page == 1:
        return FEED_BASE
    return f"{FEED_BASE}?paged={page}"


def get_feed(page):
    url = feed_url(page)
    r = session.get(url, timeout=TIMEOUT)

    # Past the final WordPress feed page may return 404.
    if r.status_code == 404:
        return None

    r.raise_for_status()
    return r.text


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def qnum(title):
    m = re.search(r"#\s*(\d+)", title or "")
    return int(m.group(1)) if m else None


def child_text(node, tag, default=""):
    child = node.find(tag)
    if child is None or child.text is None:
        return default
    return child.text


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)

    channel = root.find("channel")
    if channel is None:
        return []

    posts = []

    for item in channel.findall("item"):
        title = clean_text(child_text(item, "title"))
        link = clean_text(child_text(item, "link"))

        # Keep only numbered HY USMLE Qbank posts.
        if not re.search(r"\bQ\s*#?\s*\d+\b", title, re.I):
            continue

        guid = clean_text(child_text(item, "guid")) or link
        published = clean_text(child_text(item, "pubDate"))

        # This is the important part:
        # copy WordPress's FULL RSS article content.
        content_node = item.find(f"{{{CONTENT_NS}}}encoded")
        content_html = ""

        if content_node is not None and content_node.text:
            content_html = content_node.text

        # Fallback if WordPress ever omits content:encoded.
        if not content_html:
            content_html = child_text(item, "description")

        posts.append({
            "title": title,
            "url": link,
            "guid": guid,
            "qnum": qnum(title),
            "published": published,
            "content_html": content_html,
        })

    return posts


def load_existing():
    if not DATA_FILE.exists():
        return []

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return []


def post_key(post):
    return post.get("guid") or post.get("url")


def crawl():
    existing = load_existing()
    existing_by_key = {
        post_key(p): p
        for p in existing
        if post_key(p)
    }

    # Your existing archive was created with excerpts only.
    # If any saved post lacks full HTML, do ONE full RSS crawl to upgrade it.
    needs_backfill = (
        not existing
        or any(not p.get("content_html") for p in existing)
    )

    found = []

    page = 1
    consecutive_known_pages = 0

    # Safety ceiling; actual archive should finish long before this.
    MAX_PAGES = 400

    if needs_backfill:
        print("Full-content backfill required.")
    else:
        print(f"Incremental update: {len(existing)} posts already stored.")

    previous_page_keys = None

    while page <= MAX_PAGES:
        try:
            raw = get_feed(page)
        except requests.HTTPError as exc:
            # A 404 means we went past the oldest feed page.
            if exc.response is not None and exc.response.status_code == 404:
                print(f"Reached end of archive at page {page}.")
                break
            raise

        if raw is None:
            print(f"Reached end of archive at page {page}.")
            break

        batch = parse_feed(raw)

        if not batch:
            print(f"No Qbank items on feed page {page}; stopping.")
            break

        keys = [post_key(p) for p in batch if post_key(p)]

        # Protect against WordPress ignoring ?paged= and returning page 1
        # repeatedly.
        if page > 1 and keys == previous_page_keys:
            raise RuntimeError(
                "WordPress returned the same RSS page twice. "
                "Feed pagination is not working as expected."
            )

        previous_page_keys = keys

        known_complete = sum(
            1
            for p in batch
            if post_key(p) in existing_by_key
            and existing_by_key[post_key(p)].get("content_html")
        )

        print(
            f"RSS page {page}: "
            f"{len(batch)} Qbank posts, "
            f"{known_complete} already stored with full content"
        )

        found.extend(batch)

        # After the initial full-content backfill, future runs only need
        # to scan until we've reached already-known material.
        if not needs_backfill:
            if known_complete == len(batch):
                consecutive_known_pages += 1
            else:
                consecutive_known_pages = 0

            if consecutive_known_pages >= 2:
                print("Reached known archive material; incremental crawl complete.")
                break

        page += 1
        time.sleep(DELAY)

    merged = dict(existing_by_key)

    for p in found:
        key = post_key(p)
        if not key:
            continue

        old = merged.get(key, {})

        merged[key] = {
            "title": p.get("title") or old.get("title", ""),
            "url": p.get("url") or old.get("url", ""),
            "guid": p.get("guid") or old.get("guid", ""),
            "qnum": (
                p.get("qnum")
                if p.get("qnum") is not None
                else old.get("qnum")
            ),
            "published": p.get("published") or old.get("published", ""),
            "content_html": (
                p.get("content_html")
                or old.get("content_html", "")
            ),
        }

    posts = list(merged.values())

    def sort_key(post):
        published = post.get("published", "")
        try:
            return parsedate_to_datetime(published).timestamp()
        except Exception:
            return float(post.get("qnum") or 0)

    posts.sort(key=sort_key, reverse=True)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(posts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return posts


def cdata(value):
    # Prevent embedded content from accidentally closing CDATA.
    return (value or "").replace("]]>", "]]]]><![CDATA[>")


def build_feed(posts):
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)

    items = []

    for p in posts:
        title = escape(p.get("title", ""), quote=False)
        link = escape(p.get("url", ""), quote=True)
        guid = escape(p.get("guid") or p.get("url", ""), quote=False)
        published = p.get("published") or format_datetime(now)

        html = p.get("content_html") or (
            "<p>Open the original Mehlman Medical post.</p>"
        )

        html = cdata(html)

        items.append(
            f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{escape(published, quote=False)}</pubDate>
      <description><![CDATA[{html}]]></description>
      <content:encoded><![CDATA[{html}]]></content:encoded>
    </item>"""
        )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Mehlman Medical – Complete Free Video Qbank</title>
    <link>{SITE_BASE}</link>
    <description>Complete Mehlman Medical Free Video Qbank archive with full post content.</description>
    <language>en</language>
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>
    <generator>mehlman-qbank-rss</generator>
{chr(10).join(items)}
  </channel>
</rss>
"""

    FEED_FILE.write_text(xml, encoding="utf-8")

    complete = sum(bool(p.get("content_html")) for p in posts)

    print(
        f"Wrote {len(posts)} RSS items "
        f"({complete} with full content) to {FEED_FILE}"
    )


if __name__ == "__main__":
    posts = crawl()
    build_feed(posts)
