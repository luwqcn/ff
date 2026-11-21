#!/usr/bin/env python3
"""ff 生成器：SQLite -> Atom/RSS + JSON + 静态阅读站。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from jinja2 import Environment, FileSystemLoader, select_autoescape

ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM)
CAT_SLUGS = {
    "科技": "tech", "资讯": "news", "国际": "world", "人文": "human",
    "博客聚集": "blog", "独立博客": "indie", "未分类": "misc",
}
SITE_TITLE = "ff · 阅读"
SITE_DESC = "用 Org 管理订阅源的个人阅读站。每 4 小时更新，有变化才发布。"
# 只展示近两天的文章；可用 FF_FRESH_HOURS 覆盖（例如本地调试放宽到 72）
FRESH_HOURS = max(1, int(os.environ.get("FF_FRESH_HOURS", "48")))


def fresh_enough(published: str) -> bool:
    """发布日期在最近 FRESH_HOURS 小时内才算新鲜；解析不了视为过期。"""
    if not published:
        return False
    try:
        dt = datetime.fromisoformat(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return dt >= datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)


def cat_slug(name: str) -> str:
    return CAT_SLUGS.get(name, "cat-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:6])


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def minutes(text: str) -> int:
    return max(1, round(len(plain(text)) / 300))


def load_articles(db_path: Path, limit: int = 800) -> tuple[list[dict], list[dict]]:
    import sqlite3
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT a.*, f.status AS feed_status FROM articles a
        JOIN feeds f ON f.slug = a.feed_slug AND f.enabled = 1
        ORDER BY a.published DESC LIMIT ?
    """, (limit,)).fetchall()
    feeds = [dict(r) for r in db.execute(
        "SELECT slug,title,url,link,category,description,tags,enabled,status,last_fetch FROM feeds WHERE enabled=1 ORDER BY category,title")]
    db.close()
    for f in feeds:
        try:
            f["tags"] = json.loads(f.get("tags") or "[]")
        except (TypeError, ValueError):
            f["tags"] = []
    articles = []
    for r in rows:
        if not fresh_enough(r["published"]):
            continue
        item = dict(r)
        item["read_time"] = minutes(item.get("summary"))
        item["category_slug"] = cat_slug(item.get("category") or "未分类")
        articles.append(item)
    return articles, feeds


def atom_xml(articles: list[dict], title: str, site_url: str, built: str, feed_path: str) -> str:
    root = ET.Element(f"{{{ATOM}}}feed")
    ET.SubElement(root, f"{{{ATOM}}}title").text = title
    ET.SubElement(root, f"{{{ATOM}}}subtitle").text = SITE_DESC
    ET.SubElement(root, f"{{{ATOM}}}id").text = site_url + feed_path
    ET.SubElement(root, f"{{{ATOM}}}updated").text = built
    link = ET.SubElement(root, f"{{{ATOM}}}link")
    link.set("href", site_url + feed_path)
    ET.SubElement(root, f"{{{ATOM}}}generator").text = "ff-reader 0.1"

    for a in articles:
        entry = ET.SubElement(root, f"{{{ATOM}}}entry")
        ET.SubElement(entry, f"{{{ATOM}}}title").text = a["title"]
        elink = ET.SubElement(entry, f"{{{ATOM}}}link")
        elink.set("href", a.get("link") or a["guid"])
        ET.SubElement(entry, f"{{{ATOM}}}id").text = a["guid"]
        ET.SubElement(entry, f"{{{ATOM}}}updated").text = a["published"]
        ET.SubElement(entry, f"{{{ATOM}}}published").text = a["published"]
        summary = ET.SubElement(entry, f"{{{ATOM}}}summary")
        summary.set("type", "html")
        summary.text = a.get("summary") or ""
        author = ET.SubElement(entry, f"{{{ATOM}}}author")
        ET.SubElement(author, f"{{{ATOM}}}name").text = a.get("author") or a.get("feed_title") or "ff"
        cat = ET.SubElement(entry, f"{{{ATOM}}}category")
        cat.set("term", a.get("category") or "未分类")
        src = ET.SubElement(entry, f"{{{ATOM}}}source")
        src.set("href", site_url)
        ET.SubElement(src, f"{{{ATOM}}}title").text = a.get("feed_title") or ""
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def rss_xml(articles: list[dict], site_url: str, built: str) -> str:
    root = ET.Element("rss")
    root.set("version", "2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = SITE_TITLE
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = SITE_DESC
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.fromisoformat(built))
    for a in articles:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = a["title"]
        ET.SubElement(item, "link").text = a.get("link") or a["guid"]
        ET.SubElement(item, "guid").text = a["guid"]
        try:
            pub = format_datetime(datetime.fromisoformat(a["published"]))
        except Exception:
            pub = format_datetime(datetime.fromisoformat(built))
        ET.SubElement(item, "pubDate").text = pub
        ET.SubElement(item, "description").text = a.get("summary") or ""
        ET.SubElement(item, "category").text = a.get("category") or "未分类"
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def org_keyword(path: str, key: str) -> str:
    """从 ff.org 里读 #+KEYWORD: 的值，页面标题和题注跟着源文件走。"""
    if not path:
        return ""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            m = re.match(r"#\+" + re.escape(key) + r":\s*(.*)", line)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def content_fingerprint(boot: dict, root: Path) -> str:
    """内容指纹：只含文章/源/分类/样式/模板，剔除 built_at/fetched_at 时间戳。

    CI 每轮都是全新环境、时间戳必变，若用整目录哈希会导致「永远有变化」。
    """
    h = hashlib.sha256()
    payload = {
        "articles": boot["articles"],
        "feeds": boot["feeds"],
        "categories": boot["categories"],
    }
    h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
    for name in ("templates/index.html", "static/app.css", "static/app.js", "static/favicon.svg"):
        h.update((root / name).read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()
    root = Path(args.root)
    dist = root / "dist"
    # 全量重建：源清单改动后，旧的分类订阅目录不能留在产物里
    if dist.exists():
        for child in dist.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    dist.mkdir(parents=True, exist_ok=True)
    site_url = os.environ.get("FF_SITE_URL", "https://ff.luwq.cn").rstrip("/")
    built = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    articles, feeds = load_articles(root / "data" / "state.db")
    feeds_file = root / "data" / "feeds.json"
    feeds_meta = json.loads(feeds_file.read_text(encoding="utf-8")) if feeds_file.exists() else {}
    org_title = org_keyword(feeds_meta.get("source", ""), "TITLE")
    org_desc = org_keyword(feeds_meta.get("source", ""), "DESCRIPTION")
    stats_path = root / "data" / "fetch-stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}

    categories: list[dict] = []
    counts: dict[str, int] = {}
    for a in articles:
        counts[a["category"] or "未分类"] = counts.get(a["category"] or "未分类", 0) + 1
    # 分类描述照抄 ff.org（解析器写在 feeds.json 的 categories 里），前端不再写死文案
    cat_notes = {c["name"]: c.get("description", "") for c in feeds_meta.get("categories", [])}
    for name, count in counts.items():
        categories.append({"name": name, "slug": cat_slug(name), "count": count,
                           "description": cat_notes.get(name, "")})
    categories.sort(key=lambda c: (-c["count"], c["name"]))

    public = [{k: a[k] for k in (
        "guid", "title", "link", "author", "published", "summary",
        "feed_slug", "feed_title", "category", "category_slug", "read_time")}
        for a in articles]
    boot = {
        "meta": {"title": org_title or SITE_TITLE, "description": org_desc or SITE_DESC,
                 "site_url": site_url, "built_at": built,
                 "fetched_at": stats.get("fetched_at"), "total": len(public)},
        "categories": categories,
        "feeds": [{"slug": f["slug"], "title": f["title"], "category": f["category"],
                   "description": f.get("description") or "", "tags": f.get("tags") or [],
                   "url": f["url"], "link": f.get("link") or f["url"],
                   "status": f["status"], "enabled": f["enabled"]} for f in feeds],
        "articles": public,
    }

    env = Environment(loader=FileSystemLoader(root / "templates"),
                      autoescape=select_autoescape(["html", "xml"]))
    tpl = env.get_template("index.html")
    write_text(dist / "index.html", tpl.render(
        meta=boot["meta"], categories=categories, feeds=boot["feeds"],
        boot_json=json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")))

    write_text(dist / "data" / "articles.json", json.dumps(boot, ensure_ascii=False, indent=2))
    # ff 只做订阅内容的展示，自身不生产内容，因此不生成任何 Atom/RSS 订阅文件

    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("app.css", "app.js"):
        shutil.copy2(root / "static" / name, assets / name)
    shutil.copy2(root / "static" / "favicon.svg", dist / "favicon.svg")

    digest = content_fingerprint(boot, root)
    hash_file = root / "data" / "last-build-hash"
    changed = not hash_file.exists() or hash_file.read_text().strip() != digest
    hash_file.write_text(digest + "\n", encoding="utf-8")
    print(f"==> 生成 {len(public)} 篇文章 / {len(categories)} 个分组 / {len(feeds)} 个源")
    print(f"==> 产物变化: {'是（可发布）' if changed else '否（跳过发布）'}")
    print(f"==> 输出目录: {dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
