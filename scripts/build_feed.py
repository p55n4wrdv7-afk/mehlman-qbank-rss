#!/usr/bin/env python3
import json
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://mehlmanmedical.com/category/free-video-qbank/"
WP_API = "https://mehlmanmedical.com/wp-json/wp/v2"
ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "posts.json"
FEED_FILE = ROOT / "docs" / "feed.xml"

USER_AGENT = "Mehlman-Qbank-RSS/3.0 (+personal RSS archive; polite crawler)"
TIMEOUT = 30
ARCHIVE_DELAY = 0.5
POST_DELAY = 0.35

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def get(url: str) -> str:
    last_exc = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def qnum(title: str):
    # Matches both "HY USMLE Q #1644" and older "Audio Qbank – Q321"
    m = re.search(r"\bQ\s*#?\s*(\d+)\b", title or "", re.I)
    return int(m.group(1)) if m else None


def archive_url(page: int) -> str:
    return BASE if page == 1 else f"{BASE}page/{page}/"


def parse_archive(html: str):
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    candidates = soup.select("article")
    if not candidates:
        candidates = soup.select("h2, h3")

    for node in candidates:
        heading = node.select_one("h1, h2, h3") if hasattr(node, "select_one") else None
        if heading is None and getattr(node, "name", None) in {"h2", "h3"}:
            heading = node
        if heading is None:
            continue

        a = heading.find("a", href=True)
        if not a:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        number = qnum(title)
        if number is None:
            continue

        url = urljoin(BASE, a["href"])
        posts.append({
            "title": title,
            "url": url,
            "qnum": number,
        })

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
        m = re.search(r"/page/(\d+)/?", a["href"])
        if m:
            nums.append(int(m.group(1)))

    for el in soup.select(".page-numbers"):
        txt = clean_text(el.get_text())
        if txt.isdigit():
            nums.append(int(txt))

    return max(nums) if nums else 1


def load_existing():
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def normalize_url(url):
    return (url or "").split("#", 1)[0].split("?", 1)[0].rstrip("/") + "/"


def fetch_wp_publication_dates():
    """
    Fetch exact WordPress publication dates for every post in the
    Free Video Qbank category. This avoids inventing dates for old items.
    """
    try:
        r = session.get(
            f"{WP_API}/categories",
            params={"slug": "free-video-qbank", "per_page": 100, "_fields": "id,slug"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        categories = r.json()
        if not categories:
            raise RuntimeError("Free Video Qbank category not found in WordPress REST API")

        category_id = categories[0]["id"]
        dates = {}
        page = 1

        while True:
            r = session.get(
                f"{WP_API}/posts",
                params={
                    "categories": category_id,
                    "per_page": 100,
                    "page": page,
                    "_fields": "link,date,date_gmt,slug",
                },
                timeout=TIMEOUT,
            )

            if r.status_code == 400:
                break

            r.raise_for_status()
            batch = r.json()
            if not batch:
                break

            for item in batch:
                link = normalize_url(item.get("link"))
                raw = item.get("date_gmt") or item.get("date")
                if not link or not raw:
                    continue

                # WordPress date_gmt is returned without a timezone suffix.
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dates[link] = dt.isoformat()
                except Exception:
                    continue

            total_pages = r.headers.get("X-WP-TotalPages")
            if total_pages:
                if page >= int(total_pages):
                    break
            elif len(batch) < 100:
                break

            page += 1
            time.sleep(0.15)

        print(f"WordPress REST: loaded {len(dates)} exact publication dates")
        return dates

    except Exception as exc:
        print(f"WARNING: could not load WordPress publication dates: {exc}")
        return {}


def repair_publication_dates(posts):
    date_map = fetch_wp_publication_dates()

    if not date_map:
        print("No exact date map available; existing dates will be kept.")
        return posts

    matched = 0
    missing = 0

    for post in posts:
        key = normalize_url(post.get("url"))
        exact = date_map.get(key)

        if exact:
            post["published"] = exact
            matched += 1
        else:
            # Do not manufacture a current-time date for items the API
            # could not match. Better no date than a false date.
            post["published"] = None
            missing += 1

    print(f"Publication dates repaired: {matched} exact, {missing} unmatched")
    return posts


def absolutize_content(content, page_url):
    # Turn relative links/media into absolute URLs so they work inside NetNewsWire.
    attrs = (
        ("a", "href"),
        ("img", "src"),
        ("iframe", "src"),
        ("source", "src"),
        ("video", "src"),
    )

    for selector, attr in attrs:
        for tag in content.find_all(selector):
            value = tag.get(attr)

            # Some embeds/images lazy-load from data-src.
            if not value and tag.get("data-src"):
                value = tag.get("data-src")
                tag[attr] = value

            if value:
                tag[attr] = urljoin(page_url, value)

    for img in content.find_all("img"):
        if img.get("srcset"):
            parts = []
            for part in img["srcset"].split(","):
                bits = part.strip().split()
                if bits:
                    bits[0] = urljoin(page_url, bits[0])
                    parts.append(" ".join(bits))
            img["srcset"] = ", ".join(parts)


def extract_original_date(soup):
    # Prefer the date shown for the post itself.
    selectors = [
        "time.entry-date[datetime]",
        "time.published[datetime]",
        "time[datetime]",
        "[itemprop='datePublished'][datetime]",
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.get("datetime"):
            raw = node["datetime"].strip()
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                pass

    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        raw = meta["content"].strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass

    return None


def extract_full_post(post):
    html = get(post["url"])
    soup = BeautifulSoup(html, "html.parser")

    # Astra/WordPress normally places the actual article body here.
    content = (
        soup.select_one("article .entry-content")
        or soup.select_one(".entry-content")
        or soup.select_one("article .post-content")
        or soup.select_one(".post-content")
    )

    if content is None:
        raise RuntimeError(f"Could not find article body: {post['url']}")

    # Remove things that should not be syndicated, while KEEPING iframe/video.
    for bad in content.select(
        "script, style, noscript, form, "
        ".sharedaddy, .jp-relatedposts, .post-navigation, "
        ".navigation, .comments-area"
    ):
        bad.decompose()

    absolutize_content(content, post["url"])

    # Remove empty paragraphs but keep media wrappers.
    for p in list(content.find_all("p")):
        if not p.get_text(strip=True) and not p.find(["img", "iframe", "video", "audio"]):
            p.decompose()

    content_html = content.decode_contents(formatter="html").strip()
    if not content_html:
        raise RuntimeError(f"Article body was empty: {post['url']}")

    # Refresh title from the individual page when possible.
    title_node = soup.select_one("article h1.entry-title, h1.entry-title, article h1")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else post.get("title", "")

    return {
        **post,
        "title": title or post.get("title", ""),
        "qnum": qnum(title) or post.get("qnum"),
        "published": extract_original_date(soup) or post.get("published"),
        "content_html": content_html,
    }


def discover_posts(existing):
    existing_by_url = {p["url"]: p for p in existing if p.get("url")}

    first_html = get(BASE)

    if not existing:
        total_pages = last_page(first_html)
        print(f"Initial archive discovery: {total_pages} pages")
        found = []

        for page in range(1, total_pages + 1):
            html = first_html if page == 1 else get(archive_url(page))
            batch = parse_archive(html)
            print(f"archive page {page}/{total_pages}: {len(batch)} numbered Qbank posts")
            found.extend(batch)
            if page != total_pages:
                time.sleep(ARCHIVE_DELAY)
    else:
        print(f"Incremental archive discovery: {len(existing)} saved posts")
        found = []
        page = 1
        consecutive_known_pages = 0
        max_pages = 20

        while page <= max_pages:
            html = first_html if page == 1 else get(archive_url(page))
            batch = parse_archive(html)
            found.extend(batch)

            urls = [p["url"] for p in batch]
            known = sum(1 for u in urls if u in existing_by_url)
            print(f"archive page {page}: {len(batch)} posts, {known} already known")

            if batch and known == len(batch):
                consecutive_known_pages += 1
            else:
                consecutive_known_pages = 0

            if consecutive_known_pages >= 2:
                break

            page += 1
            time.sleep(ARCHIVE_DELAY)

    merged = dict(existing_by_url)

    for p in found:
        old = merged.get(p["url"], {})
        merged[p["url"]] = {
            **old,
            "title": p.get("title") or old.get("title", ""),
            "url": p["url"],
            "qnum": p.get("qnum") if p.get("qnum") is not None else old.get("qnum"),
        }

    return list(merged.values())


def backfill_full_content(posts):
    pending = [
        p for p in posts
        if not p.get("content_html")
        or "Open the original Mehlman Medical post." in p.get("content_html", "")
    ]

    print(f"{len(pending)} posts need full article content")

    by_url = {p["url"]: p for p in posts if p.get("url")}

    for i, p in enumerate(pending, start=1):
        try:
            full = extract_full_post(p)
            by_url[p["url"]] = full
            print(f"[{i}/{len(pending)}] OK {full.get('title', p['url'])}")
        except Exception as exc:
            # Keep the post in the archive, but do NOT pretend this is complete.
            old = by_url[p["url"]]
            old["fetch_error"] = str(exc)
            print(f"[{i}/{len(pending)}] ERROR {p['url']}: {exc}")

        # Save progress continuously. If GitHub ever interrupts a long first run,
        # the next run can continue rather than starting over after a commit.
        if i % 25 == 0:
            save_posts(list(by_url.values()))

        time.sleep(POST_DELAY)

    return list(by_url.values())


def save_posts(posts):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(posts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def to_datetime(raw):
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def cdata(value):
    return (value or "").replace("]]>", "]]]]><![CDATA[>")


def build_feed(posts):
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    build_dt = datetime.now(timezone.utc)

    # Newest Q number first, matching the Qbank sequence.
    posts.sort(key=lambda p: p.get("qnum") or -1, reverse=True)

    items = []
    full_count = 0

    for p in posts:
        title = escape(p.get("title", ""), quote=False)
        link = escape(p.get("url", ""), quote=True)
        dt = to_datetime(p.get("published"))
        pub_date_xml = (
            f"      <pubDate>{format_datetime(dt)}</pubDate>\n"
            if dt is not None
            else ""
        )

        html = p.get("content_html", "").strip()
        if html:
            full_count += 1
        else:
            html = (
                f'<p><a href="{link}">'
                "Open the original Mehlman Medical post."
                "</a></p>"
            )

        html = cdata(html)

        items.append(f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
{pub_date_xml}      <description><![CDATA[{html}]]></description>
      <content:encoded><![CDATA[{html}]]></content:encoded>
    </item>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Mehlman Medical – Complete Free Video Qbank</title>
    <link>{BASE}</link>
    <description>Complete historical archive of numbered Mehlman Medical Free Video Qbank posts with full article content, automatically updated.</description>
    <language>en</language>
    <lastBuildDate>{format_datetime(build_dt)}</lastBuildDate>
    <generator>mehlman-qbank-rss</generator>
{chr(10).join(items)}
  </channel>
</rss>
"""

    FEED_FILE.write_text(xml, encoding="utf-8")
    print(f"Wrote {len(posts)} items; {full_count} contain full article HTML")


def main():
    existing = load_existing()
    posts = discover_posts(existing)
    posts = backfill_full_content(posts)
    posts = repair_publication_dates(posts)
    save_posts(posts)
    build_feed(posts)


if __name__ == "__main__":
    main()
