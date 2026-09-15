#!/usr/bin/env python3
import json
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://mehlmanmedical.com/category/free-video-qbank/"
ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "posts.json"
FEED_FILE = ROOT / "docs" / "feed.xml"
USER_AGENT = "Mehlman-Qbank-RSS/1.0 (+personal RSS archive; polite crawler)"
TIMEOUT = 30
DELAY = 0.6

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def get(url: str) -> str:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def qnum(title: str):
    m = re.search(r"#\s*(\d+)", title)
    return int(m.group(1)) if m else None


def archive_url(page: int) -> str:
    return BASE if page == 1 else f"{BASE}page/{page}/"


def parse_archive(html: str):
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    # Prefer article elements when available.
    candidates = soup.select("article")
    if not candidates:
        # Astra/WordPress archive fallback: locate post headings.
        candidates = soup.select("h2, h3")

    for node in candidates:
        scope = node
        heading = scope.select_one("h1, h2, h3") if getattr(scope, "select_one", None) else None
        if heading is None and getattr(scope, "name", None) in {"h2", "h3"}:
            heading = scope
        if heading is None:
            continue

        a = heading.find("a", href=True)
        if not a:
            continue
        title = clean_text(a.get_text(" ", strip=True))
        url = urljoin(BASE, a["href"])

        # This category has some older non-numbered material. Keep only actual
        # Qbank posts; current items are titled "HY USMLE Q #...".
        if not re.search(r"\bQ\s*#?\s*\d+\b", title, re.I):
            continue

        excerpt = ""
        if getattr(scope, "select_one", None):
            excerpt_node = scope.select_one(".entry-summary, .entry-content, p")
            if excerpt_node:
                excerpt = clean_text(excerpt_node.get_text(" ", strip=True))

        dt = None
        time_node = scope.find("time") if getattr(scope, "find", None) else None
        if time_node:
            raw = time_node.get("datetime") or clean_text(time_node.get_text(" ", strip=True))
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                dt = None

        posts.append({
            "title": title,
            "url": url,
            "excerpt": excerpt,
            "qnum": qnum(title),
            "published": dt.isoformat() if dt else None,
        })

    # De-duplicate links while preserving order.
    seen = set()
    out = []
    for p in posts:
        if p["url"] not in seen:
            seen.add(p["url"])
            out.append(p)
    return out


def last_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/page/(\d+)/?", href)
        if m:
            nums.append(int(m.group(1)))
    # Also inspect visible pagination text.
    for el in soup.select(".page-numbers"):
        txt = clean_text(el.get_text())
        if txt.isdigit():
            nums.append(int(txt))
    return max(nums) if nums else 1


def load_existing():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def assign_dates(posts):
    """Ensure every item has a stable RSS date.

    Real WordPress dates are used when visible in archive markup. If a date is
    absent, synthetic dates preserve Qbank order and remain stable because they
    are saved in posts.json.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # Newest first. Use qnum where available.
    posts.sort(key=lambda p: (p.get("qnum") is not None, p.get("qnum") or -1), reverse=True)
    for i, p in enumerate(posts):
        if not p.get("published"):
            p["published"] = (now - timedelta(minutes=i)).isoformat()
    return posts


def crawl():
    existing = load_existing()
    existing_by_url = {p["url"]: p for p in existing}

    first_html = get(BASE)
    first_posts = parse_archive(first_html)
    if not first_posts:
        raise RuntimeError("No Qbank posts found on first page; refusing to publish an incomplete feed")

    if not existing:
        total_pages = last_page(first_html)
        print(f"Initial crawl: {total_pages} archive pages")
        found = []
        for page in range(1, total_pages + 1):
            html = first_html if page == 1 else get(archive_url(page))
            batch = parse_archive(html)
            print(f"page {page}/{total_pages}: {len(batch)} Qbank posts")
            found.extend(batch)
            if page != total_pages:
                time.sleep(DELAY)
    else:
        print(f"Incremental crawl: {len(existing)} saved posts")
        found = []
        page = 1
        consecutive_known_pages = 0
        max_pages = last_page(first_html)
        while page <= max_pages:
            html = first_html if page == 1 else get(archive_url(page))
            batch = parse_archive(html)
            found.extend(batch)
            urls = [p["url"] for p in batch]
            known = sum(1 for u in urls if u in existing_by_url)
            print(f"page {page}: {len(batch)} posts, {known} already known")
            if batch and known == len(batch):
                consecutive_known_pages += 1
            else:
                consecutive_known_pages = 0
            if consecutive_known_pages >= 2:
                break
            page += 1
            time.sleep(DELAY)

    # Merge old metadata with newly scraped metadata.
    merged = dict(existing_by_url)
    for p in found:
        old = merged.get(p["url"], {})
        merged[p["url"]] = {
            "title": p.get("title") or old.get("title", ""),
            "url": p["url"],
            "excerpt": p.get("excerpt") or old.get("excerpt", ""),
            "qnum": p.get("qnum") if p.get("qnum") is not None else old.get("qnum"),
            "published": p.get("published") or old.get("published"),
        }

    posts = assign_dates(list(merged.values()))
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(posts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return posts


def xml_text(s):
    return escape(s or "", quote=False)


def build_feed(posts):
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    build_dt = datetime.now(timezone.utc)
    items = []
    for p in posts:
        try:
            dt = datetime.fromisoformat(p["published"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = build_dt
        title = xml_text(p["title"])
        link = escape(p["url"], quote=True)
        desc = xml_text(p.get("excerpt") or "Open the Mehlman Medical Qbank post.")
        items.append(f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink=\"true\">{link}</guid>
      <pubDate>{format_datetime(dt)}</pubDate>
      <description>{desc}</description>
    </item>""")

    xml = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\">
  <channel>
    <title>Mehlman Medical – Complete Free Video Qbank</title>
    <link>{BASE}</link>
    <description>Complete historical archive of numbered Mehlman Medical Free Video Qbank posts, automatically updated.</description>
    <language>en</language>
    <lastBuildDate>{format_datetime(build_dt)}</lastBuildDate>
    <generator>mehlman-qbank-rss</generator>
{chr(10).join(items)}
  </channel>
</rss>
"""
    FEED_FILE.write_text(xml, encoding="utf-8")
    print(f"Wrote {len(posts)} items to {FEED_FILE}")


if __name__ == "__main__":
    posts = crawl()
    build_feed(posts)
