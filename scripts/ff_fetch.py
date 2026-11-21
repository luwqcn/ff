#!/usr/bin/env python3
"""ff 抓取器：读 feeds.json，增量拉取订阅源，去重后写入 SQLite。

只使用 feed 自带的内容，不额外抓取原文全文。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx

UA = "ff-reader/0.1 (+RSS reader; local preview)"
ALLOWED_TAGS = {
    "p", "br", "a", "strong", "em", "b", "i", "u", "ul", "ol", "li",
    "blockquote", "code", "pre", "div", "span", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td",
}
DROP_TAGS = {"script", "style", "iframe", "object", "embed", "form", "input", "svg", "video", "audio"}


class Sanitizer(HTMLParser):
    """白名单清洗：只保留 feed 自带摘要里的安全标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_TAGS:
            self.depth += 1
            return
        if self.depth or tag not in ALLOWED_TAGS:
            return
        clean = []
        for key, value in attrs:
            key = (key or "").lower()
            if value is None or key not in {"href", "src", "alt", "title"}:
                continue
            if key in {"href", "src"}:
                parts = urlsplit(value.strip())
                if parts.scheme.lower() not in {"http", "https", "mailto"}:
                    continue
                value = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            clean.append(f'{key}="{html.escape(value, quote=True)}"')
        suffix = " /" if tag in {"br", "hr"} else ""
        self.out.append(f"<{tag}{(' ' + ' '.join(clean)) if clean else ''}{suffix}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in ALLOWED_TAGS and tag.lower() not in {"br", "hr"}:
            self.out.append(f"</{tag.lower()}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_TAGS:
            self.depth = max(0, self.depth - 1)
            return
        if not self.depth and tag in ALLOWED_TAGS and tag not in {"br", "hr", "img"}:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.depth:
            self.out.append(html.escape(data))

    def result(self) -> str:
        text = "".join(self.out).strip()
        return re.sub(r"\s*\n\s*", "\n", text)[:8000]


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    s = Sanitizer()
    try:
        s.feed(raw)
        s.close()
        return s.result()
    except Exception:
        return html.escape(re.sub(r"<[^>]+>", "", raw))[:4000]


def normalize_link(link: str) -> str:
    if not link:
        return ""
    parts = urlsplit(link.strip())
    query = "&".join(
        p for p in parts.query.split("&")
        if p.split("=", 1)[0].lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def iso_time(value) -> str:
    if value:
        try:
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS feeds (
            slug TEXT PRIMARY KEY, title TEXT, url TEXT, link TEXT,
            category TEXT, description TEXT DEFAULT '', tags TEXT DEFAULT '[]',
            enabled INTEGER, interval INTEGER,
            last_fetch TEXT, etag TEXT, last_modified TEXT,
            status TEXT, error TEXT
        )
    """)
    cols = {r[1] for r in db.execute("PRAGMA table_info(feeds)")}
    if "description" not in cols:
        db.execute("ALTER TABLE feeds ADD COLUMN description TEXT DEFAULT ''")
    if "link" not in cols:
        db.execute("ALTER TABLE feeds ADD COLUMN link TEXT DEFAULT ''")
    if "tags" not in cols:
        db.execute("ALTER TABLE feeds ADD COLUMN tags TEXT DEFAULT '[]'")
    db.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            guid TEXT PRIMARY KEY, feed_slug TEXT, feed_title TEXT, category TEXT,
            title TEXT, link TEXT, author TEXT, published TEXT,
            summary TEXT, fetched_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_articles_feed ON articles(feed_slug)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            ts TEXT, slug TEXT, status TEXT, added INTEGER, error TEXT
        )
    """)
    return db


def sync_feeds(db: sqlite3.Connection, feeds: list[dict]) -> None:
    known = {row[0] for row in db.execute("SELECT slug FROM feeds")}
    incoming = {f["slug"] for f in feeds}
    for f in feeds:
        db.execute("""
            INSERT INTO feeds (slug,title,url,link,category,description,tags,enabled,interval,status)
            VALUES (:slug,:title,:url,:link,:category,:description,:tags,:enabled,:interval,'new')
            ON CONFLICT(slug) DO UPDATE SET
              title=excluded.title, url=excluded.url, link=excluded.link,
              category=excluded.category,
              description=excluded.description, tags=excluded.tags,
              enabled=excluded.enabled, interval=excluded.interval
        """, {**f, "enabled": int(f["enabled"]), "link": f.get("link") or f["url"],
              "tags": json.dumps(f.get("tags") or [], ensure_ascii=False)})
    for slug in known - incoming:
        # 源从 ff.org 删掉后，连同已抓的文章一起清掉；
        # 只标记 enabled=0 会让旧文章继续出现在阅读器和订阅里。
        db.execute("DELETE FROM articles WHERE feed_slug=?", (slug,))
        db.execute("DELETE FROM fetch_log WHERE slug=?", (slug,))
        db.execute("DELETE FROM feeds WHERE slug=?", (slug,))


def due_feeds(db: sqlite3.Connection, feeds: list[dict], force: bool) -> list[dict]:
    rows = {r[0]: r for r in db.execute(
        "SELECT slug,last_fetch,etag,last_modified FROM feeds WHERE enabled=1")}
    now = datetime.now(timezone.utc)
    out = []
    for f in feeds:
        if not f["enabled"]:
            continue
        row = rows.get(f["slug"])
        if not row:
            out.append(f)
            continue
        last = datetime.fromisoformat(row[1]) if row[1] else datetime.min.replace(tzinfo=timezone.utc)
        if force or now - last >= timedelta(seconds=f["interval"]):
            out.append({**f, "etag": row[2], "last_modified": row[3]})
    return out


def fetch_one(feed: dict) -> dict:
    headers = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
    if feed.get("etag"):
        headers["If-None-Match"] = feed["etag"]
    if feed.get("last_modified"):
        headers["If-Modified-Since"] = feed["last_modified"]
    try:
        with httpx.Client(follow_redirects=True, timeout=18, headers=headers) as client:
            for attempt in range(2):
                try:
                    resp = client.get(feed["url"])
                    break
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt:
                        raise
        if resp.status_code == 304:
            return {"slug": feed["slug"], "status": "304", "added": 0}
        resp.raise_for_status()
        if not resp.content or len(resp.content) > 12_000_000:
            return {"slug": feed["slug"], "status": "error", "added": 0, "error": "feed 内容为空或过大"}
        return {"slug": feed["slug"], "status": str(resp.status_code), "added": 0,
                "text": resp.content, "etag": resp.headers.get("etag", ""),
                "last_modified": resp.headers.get("last-modified", "")}
    except Exception as exc:
        return {"slug": feed["slug"], "status": "error", "added": 0, "error": f"{type(exc).__name__}: {exc}"}


def store_feed(db: sqlite3.Connection, feed: dict, result: dict, now: str) -> None:
    if result["status"] == "error":
        db.execute("UPDATE feeds SET last_fetch=?,status='error',error=? WHERE slug=?",
                   (now, result.get("error", "")[:500], feed["slug"]))
        db.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                   (now, feed["slug"], "error", 0, result.get("error", "")[:500]))
        return
    if result["status"] == "304":
        db.execute("UPDATE feeds SET last_fetch=?,status='ok',error='' WHERE slug=?", (now, feed["slug"]))
        db.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)", (now, feed["slug"], "304", 0, ""))
        return

    parsed = feedparser.parse(result["text"])
    added = 0
    for entry in parsed.entries:
        link = normalize_link(entry.get("link", ""))
        guid = entry.get("id") or entry.get("guid") or link or (entry.get("title", "") + now)
        title = html.unescape(re.sub(r"<[^>]+>", "", entry.get("title", "(无标题)"))).strip()
        if not title:
            continue
        raw = entry.get("summary") or entry.get("description") or ""
        if not raw and entry.get("content"):
            raw = entry["content"][0].get("value", "")
        author = entry.get("author") or ""
        if not author and entry.get("authors"):
            author = entry["authors"][0].get("name", "")
        published = iso_time(entry.get("published_parsed") or entry.get("updated_parsed"))
        cur = db.execute("""
            INSERT OR IGNORE INTO articles
              (guid,feed_slug,feed_title,category,title,link,author,published,summary,fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (guid, feed["slug"], feed["title"], feed["category"], title,
              link, author, published, clean_html(raw), now))
        added += cur.rowcount
    db.execute("""
        UPDATE feeds SET last_fetch=?,etag=?,last_modified=?,status='ok',error=''
        WHERE slug=?
    """, (now, result.get("etag", ""), result.get("last_modified", ""), feed["slug"]))
    db.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)", (now, feed["slug"], "ok", added, ""))
    result["added"] = added
    if parsed.bozo and not parsed.entries:
        db.execute("UPDATE feeds SET status='parse_error',error=? WHERE slug=?",
                   (str(getattr(parsed, "bozo_exception", "bozo"))[:500], feed["slug"]))


def prune(db: sqlite3.Connection) -> int:
    before = db.total_changes
    db.execute("DELETE FROM articles WHERE fetched_at < date('now','-90 days')")
    db.execute("""
        DELETE FROM articles WHERE rowid IN (
          SELECT rowid FROM (
            SELECT rowid, ROW_NUMBER() OVER (PARTITION BY feed_slug ORDER BY published DESC) AS rn
            FROM articles
          ) WHERE rn > 200
        )
    """)
    db.execute("DELETE FROM fetch_log WHERE ts < date('now','-30 days')")
    return db.total_changes - before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="ff 项目根目录")
    ap.add_argument("--force", action="store_true", help="忽略 INTERVAL，强制抓全部启用源")
    args = ap.parse_args()

    root = Path(args.root)
    feeds_file = root / "data" / "feeds.json"
    if not feeds_file.exists():
        print("找不到 data/feeds.json，请先跑解析脚本", file=sys.stderr)
        return 1
    feeds = json.loads(feeds_file.read_text(encoding="utf-8"))["feeds"]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    db = connect(root / "data" / "state.db")
    sync_feeds(db, feeds)
    due = due_feeds(db, feeds, args.force)
    print(f"==> 到期源 {len(due)}/{sum(1 for f in feeds if f['enabled'])}")

    results: list[tuple[dict, dict]] = []
    if due:
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_one, f): f for f in due}
            for future in cf.as_completed(futures):
                results.append((futures[future], future.result()))

    total = 0
    for feed, result in results:
        store_feed(db, feed, result, now)
        total += result["added"]
        print(f"  [{result['status']}] {feed['title']}: +{result['added']}"
              + (f" ({result['error'][:80]})" if result.get("error") else ""))

    removed = prune(db)
    counts = dict(db.execute("SELECT status, COUNT(*) FROM feeds GROUP BY status").fetchall())
    db.commit()
    stats = {"fetched_at": now, "due": len(due), "added": total,
             "pruned": removed, "feeds": counts}
    (root / "data" / "fetch-stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"==> 新增 {total} 篇，清理 {removed} 条，统计已写入 data/fetch-stats.json")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
