#!/usr/bin/env python3
"""Build a permanent RSS archive. Run with --full to rescan every archive page."""
import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BASE = "https://mehlmanmedical.com/category/free-video-qbank/"
PODCAST_RSS = "https://anchor.fm/s/2447ab5c/podcast/rss"
ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "posts.json"
FEED_FILE = ROOT / "docs" / "feed.xml"
TIMEOUT = 30
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("content", CONTENT_NS)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mehlman-Qbank-RSS/4.0 (+personal RSS archive; polite crawler)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def now():
    return datetime.now(timezone.utc)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def qnum(title):
    match = re.search(r"\bQ\s*#?\s*(\d+)\b", title or "", re.I)
    return int(match.group(1)) if match else None


def date(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    for parser in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
                   parsedate_to_datetime):
        try:
            value = parser(raw.strip())
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def get(url):
    for attempt in range(3):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if attempt == 2 or (status and status != 429 and status < 500):
                raise
            time.sleep(2 ** (attempt + 1))


def url_key(url):
    parts = urlsplit(url)
    if parts.hostname in {"mehlmanmedical.com", "www.mehlmanmedical.com"}:
        return urlunsplit(("https", "mehlmanmedical.com",
                          parts.path.rstrip("/") + "/", "", ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save(posts):
    atomic_write(DATA_FILE, json.dumps(posts, ensure_ascii=False, indent=2) + "\n")


def load():
    if not DATA_FILE.exists():
        return []
    posts = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if not isinstance(posts, list) or any(
        not isinstance(p, dict) or not p.get("url") or not p.get("title")
        for p in posts
    ):
        raise ValueError("Invalid posts.json; refusing to overwrite the saved archive")
    merged = {}
    for post in posts:
        key = url_key(post["url"])
        if key in merged:
            # Keep the first saved URL/GUID so existing subscriptions stay stable.
            original_url = merged[key]["url"]
            merged[key].update({k: v for k, v in post.items() if v is not None and v != ""})
            merged[key]["url"] = original_url
        else:
            merged[key] = dict(post)
    return list(merged.values())


def archive(html):
    soup = BeautifulSoup(html, "html.parser")
    posts = {}
    for node in soup.select("article") or soup.select("h2, h3"):
        heading = node if node.name in {"h2", "h3"} else node.select_one("h1, h2, h3")
        anchor = heading.find("a", href=True) if heading else None
        if not anchor:
            continue
        title = clean(anchor.get_text(" ", strip=True))
        number = qnum(title)
        if number is not None:
            url = url_key(urljoin(BASE, anchor["href"]))
            posts[url] = {"url": url, "title": title, "qnum": number}
    pages = [1]
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"^" + re.escape(BASE) + r"page/(\d+)/?$",
                          urljoin(BASE, anchor["href"]))
        if match:
            pages.append(int(match.group(1)))
    return list(posts.values()), max(pages), bool(soup.select("article"))


def discover(existing, full=False):
    merged = {url_key(p["url"]): dict(p) for p in existing}
    known = set(merged)
    first = get(BASE)
    first_posts, total, _ = archive(first)
    if not first_posts:
        raise RuntimeError("No Qbank entries on page 1; refusing to publish")
    full = full or not existing
    seen = set()
    known_pages = 0
    for page in range(1, total + 1):
        html = first if page == 1 else get(f"{BASE}page/{page}/")
        batch, _, has_articles = archive(html)
        if not batch and not has_articles:
            raise RuntimeError(f"Archive page {page} has no recognizable posts")
        keys = {url_key(p["url"]) for p in batch}
        if keys and keys.issubset(seen):
            raise RuntimeError(f"Archive page {page} repeats earlier entries; check pagination")
        seen.update(keys)
        for post in batch:
            key = url_key(post["url"])
            old = merged.get(key, {})
            merged[key] = {**old, **post, "url": old.get("url", post["url"])}
        print(f"Archive {page}/{total}: {len(batch)} numbered posts", flush=True)
        known_pages = known_pages + 1 if keys and keys.issubset(known) else 0
        if not full and known_pages >= 2:
            break
        if page < total:
            time.sleep(0.5)
    print(f"Discovered {len(seen)} unique URLs; preserving {len(merged)} total posts")
    return list(merged.values())


def original_date(soup):
    # Generic <time> elements on this site belong to related articles.
    # Dates are obtained from the URL-matched WordPress record in repair_dates.
    return None


def full_post(post):
    soup = BeautifulSoup(get(post["url"]), "html.parser")
    body = soup.select_one("article .entry-content, .entry-content, article .post-content, .post-content")
    if body is None:
        raise RuntimeError("Article body not found")
    for node in body.select("script, style, noscript, form, .sharedaddy, .jp-relatedposts, "
                            ".post-navigation, .navigation, .comments-area"):
        node.decompose()
    for tag, attribute in (("a", "href"), ("img", "src"), ("iframe", "src"),
                           ("source", "src"), ("video", "src"), ("audio", "src"),
                           ("video", "poster")):
        for node in body.find_all(tag):
            value = node.get(attribute) or (node.get("data-src") if attribute == "src" else None)
            if value:
                node[attribute] = urljoin(post["url"], value)
    for node in body.select("img[srcset], source[srcset]"):
        # src remains the reliable fallback; avoid malformed relative srcset URLs.
        del node["srcset"]
    content = body.decode_contents(formatter="html").strip()
    if not content:
        raise RuntimeError("Article body is empty")
    result = {**post, "content_html": content, "content_fetched_at": now().isoformat()}
    title = soup.select_one("article h1.entry-title, h1.entry-title, article h1")
    if title and qnum(clean(title.get_text(" ", strip=True))) is not None:
        result["title"] = clean(title.get_text(" ", strip=True))
        result["qnum"] = qnum(result["title"])
    website_date = original_date(soup)
    if website_date:
        result["website_published"] = website_date
        if not date(result.get("published")):
            result.update(published=website_date, date_source="website")
    result.pop("fetch_error", None)
    return result


def backfill(posts, refresh_days):
    cutoff = now() - timedelta(days=refresh_days) if refresh_days else None
    failures = 0
    for index, post in enumerate(posts):
        fetched = date(post.get("content_fetched_at"))
        missing = not post.get("content_html", "").strip() or "Open the original Mehlman Medical post." in post.get("content_html", "")
        stale = cutoff is not None and (fetched is None or fetched < cutoff)
        if not missing and not stale:
            continue
        try:
            posts[index] = full_post(post)
        except (requests.RequestException, RuntimeError) as exc:
            post["fetch_error"] = str(exc)
            failures += 1
            print(f"WARNING: {post['url']}: {exc}", flush=True)
        if (index + 1) % 25 == 0:
            save(posts)  # Local checkpoint; workflow must commit it to survive a new runner.
        time.sleep(0.35)
    print(f"Content fetch failures: {failures}")
    return posts


def normalized(title):
    title = unescape(title or "").lower()
    title = re.sub(r"[–—−]", "-", title).replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"#\s+", "#", clean(title))


def repair_dates(posts):
    """Use each article's own WordPress publication date, never related-post dates."""
    api = "https://mehlmanmedical.com/wp-json/wp/v2/"
    categories = json.loads(get(api + "categories?slug=free-video-qbank"))
    if len(categories) != 1:
        raise RuntimeError("Cannot identify the Qbank category for date verification")
    category_id = int(categories[0]["id"])
    records = {}
    page = 1
    while True:
        url = (api + f"posts?categories={category_id}&per_page=100&page={page}"
               + "&_fields=id,link,date_gmt")
        batch = json.loads(get(url))
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected WordPress date response")
        for record in batch:
            records[url_key(record["link"])] = record
        if len(batch) < 100 or page * 100 >= int(categories[0]["count"]):
            break
        page += 1
        time.sleep(0.5)
    matched = 0
    for post in posts:
        record = records.get(url_key(post["url"]))
        raw = record.get("date_gmt") if record else None
        if not raw or raw.startswith("0000-"):
            continue
        published = date(raw + "Z")
        if not published:
            continue
        if post.get("published") and "previous_published" not in post:
            post["previous_published"] = post["published"]
        post.update(published=published.isoformat(), date_source="wordpress-api",
                    date_source_url=api + "posts/" + str(record["id"]))
        matched += 1
    if not matched:
        raise RuntimeError("No WordPress publication dates matched; refusing to publish")
    print(f"WordPress dates matched by post URL: {matched}/{len(posts)}; "
          "unmatched saved dates preserved")


def xml_safe(value):
    return "".join(c for c in str(value or "") if c in "\t\n\r" or
                   0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                   0x10000 <= ord(c) <= 0x10FFFF)


def build_feed(posts):
    if not posts or len({url_key(p["url"]) for p in posts}) != len(posts):
        raise ValueError("Refusing to publish an empty feed or duplicate URLs")
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    def add(parent, name, value):
        ET.SubElement(parent, name).text = xml_safe(value)
    add(channel, "title", "Mehlman Medical – Complete Free Video Qbank")
    add(channel, "link", BASE)
    add(channel, "description", "Numbered Mehlman Qbank archive with full content where available. Dates use URL-matched WordPress publication records, retaining saved dates for posts no longer in the category.")
    add(channel, "language", "en")
    add(channel, "lastBuildDate", format_datetime(now()))
    add(channel, "generator", "mehlman-qbank-rss")
    full_count = 0
    for post in sorted(posts, key=lambda p: qnum(p["title"]) or -1, reverse=True):
        item = ET.SubElement(channel, "item")
        add(item, "title", post["title"])
        add(item, "link", post["url"])
        ET.SubElement(item, "guid", isPermaLink="true").text = xml_safe(post["url"])
        published = date(post.get("published"))
        if published:
            add(item, "pubDate", format_datetime(published))
        html = post.get("content_html", "").strip()
        if html:
            full_count += 1
        else:
            html = "Open the original Mehlman Medical post using the article link."
        add(item, "description", html)
        add(item, f"{{{CONTENT_NS}}}encoded", html)
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml)  # Validate before replacing the published file.
    atomic_write(FEED_FILE, xml.decode("utf-8") + "\n")
    print(f"RSS verified: {len(posts)} unique items; {full_count} with article HTML")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Rescan all archive pages, preserving saved posts")
    parser.add_argument("--refresh-days", type=int, default=0,
                        help="Refresh content older than N days; 0 only fetches missing content")
    args = parser.parse_args()
    if args.refresh_days < 0:
        parser.error("--refresh-days must be zero or positive")
    posts = discover(load(), full=args.full)
    posts = backfill(posts, args.refresh_days)
    repair_dates(posts)
    save(posts)
    build_feed(posts)


if __name__ == "__main__":
    main()
