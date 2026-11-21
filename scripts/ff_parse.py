#!/usr/bin/env python3
"""把 ff.org 解析成 feeds.json（纯 Python，无 Emacs 依赖）。

用法（由 ff_run.sh 调用，环境变量传参）：
    FF_SOURCE=/path/to/ff.org FF_OUT=/path/to/feeds.json python ff_parse.py

解析规则（层级自适应）：
    订阅源   -> 标题里带 Org 链接、且属性抽屉里有 :URL: 的标题（落在第几级都行）
                也兼容 "- [[博客主页][源名]]" 列表项写法
    博客主页 -> 标题里的 Org 链接优先，其次 :LINK: 属性（旧格式），都没有才回退到 feed 地址
    属性抽屉 -> 写在本标题节点上，不再依赖单一 current 指针（避免层级变化时串位）
    源下面紧跟的正文 -> 源简介

    分类判定：
      多个一级标题 -> 分类只看一级标题
      只有一个一级标题 -> 该一级标题是分类；某个二级标题下若有 >=2 个
                          三级订阅源，该二级标题也算分类，否则不算
      没有一级标题   -> 降级：顶层节点下含 >=2 个直接子源的算分类

    分类简介 -> 分类标题下、第一个子标题之前的正文（照抄 ff.org，不自行编文案）
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

HEAD_RE = re.compile(r"^(\*+)\s+(.*)$")
LIST_RE = re.compile(r"^\s*[-+]\s+(.*)$")            # Org 无序列表项 = 订阅源
PROP_RE = re.compile(r"^\s*:([A-Za-z0-9_]+):\s*(.*?)\s*$")
ORG_LINK_TITLE_RE = re.compile(r"\[\[([^\]]+)\]\[([^\]]+)\]\]")
ORG_LINK_PLAIN_RE = re.compile(r"\[\[([^\]]+)\]\]")
ORG_LINK_ANY_RE = re.compile(r"\[\[([^\]]+)\](?:\[([^\]]+)\])?\]")
# 行尾 Org 标签：零宽断言才能正确切出 :80后:技术:摄影: 里的每一个（普通 findall 会漏中间的）
ORG_TAGS_RE = re.compile(r"(?<=:)([\w\u4e00-\u9fff@#%]+)(?=:)")
TRAILING_TAGS_RE = re.compile(r"\s+:(?:[\w@#%\u4e00-\u9fff-]+:)+$")
ALLOWED_URL_SCHEMES = ("mailto:", "http://", "https://")
FALSE_VALUES = {"no", "false", "0", "off", "disabled"}


def slug(url: str) -> str:
    """与 Emacs 版一致：host 去非字母数字取前 12 位 + URL 的 md5 前 6 位。"""
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        host = ""
    clean = re.sub(r"[^a-z0-9]", "", host.lower())
    if len(clean) > 12:
        clean = clean[:12]
    if not clean:
        clean = "feed"
    return f"{clean}-{hashlib.md5(url.encode('utf-8')).hexdigest()[:6]}"


def bool_value(value: str | None) -> bool:
    return (value or "yes").strip().lower() not in FALSE_VALUES


def plain_title(raw: str) -> str:
    """去掉 Org 行内标记，对齐 org-element 的 :raw-value。"""
    s = re.sub(r"\[\[[^\]]+\]\[([^\]]+)\]\]", r"\1", raw)  # [[url][text]]
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)               # [[url]]
    s = re.sub(r"(\*|/|_|\+)(.+?)\1", r"\2", s)             # 粗体/斜体/下划线/删除线
    s = re.sub(r"==(.+?)==", r"\1", s)                      # 高亮
    s = re.sub(r"\\(.)", r"\1", s)                          # 转义
    s = TRAILING_TAGS_RE.sub("", s)                          # 行尾 Org 标签 :80后:
    return s.strip()


def strip_markup(text: str) -> str:
    """简介里的 Org 标记转纯文本，对齐 org-element-interpret-data。"""
    return plain_title(text)


def extract_tags(raw: str) -> list[str]:
    """从标题原始文本里取行尾 Org 标签 :80后:技术:；只认行尾，避免误抓 URL 里的冒号。"""
    m = TRAILING_TAGS_RE.search(raw)
    if not m:
        return []
    return list(dict.fromkeys(ORG_TAGS_RE.findall(m.group(0))))  # 去重且保序


def markup_to_html(text: str) -> str:
    """把 Org 行内标记转成安全 HTML：链接保留为 <a>，其余标记剥掉并做 HTML 转义。

    只允许 mailto:/http:/https: 协议，避免 javascript: 之类的注入。
    """
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for m in ORG_LINK_ANY_RE.finditer(text):
        out.append(html.escape(plain_title(text[pos:m.start()])))
        target = m.group(1).strip()
        label = (m.group(2) or "").strip() or target
        if target.lower().startswith(ALLOWED_URL_SCHEMES):
            rel = "" if target.lower().startswith("mailto:") else ' target="_blank" rel="noopener noreferrer"'
            out.append(f'<a href="{html.escape(target, quote=True)}"{rel}>{html.escape(label)}</a>')
        else:
            out.append(html.escape(label))
        pos = m.end()
    out.append(html.escape(plain_title(text[pos:])))
    return "".join(out)


def split_org_link(raw: str) -> tuple[str, str]:
    """从标题/列表项里拆出 (标题, 链接)。链接取第一个 Org [[...]]，标题去掉所有标记。"""
    title = plain_title(raw)
    m = ORG_LINK_TITLE_RE.search(raw)
    if m:
        return title, m.group(1).strip()
    m = ORG_LINK_PLAIN_RE.search(raw)
    if m:
        return title, m.group(1).strip()
    return title, ""


def make_node(level: int, raw_title: str, kind: str = "head") -> dict:
    """一个标题节点；属性与正文都挂在自己身上，避免串到别的标题。"""
    title, link = split_org_link(raw_title)
    return {
        "level": level,
        "kind": kind,
        "title": title,
        "link": link,
        "tags": extract_tags(raw_title),
        "props": {},
        "desc": [],
        "children": [],
        "parent": None,
        "maybe_feed": False,
        "is_category": False,
    }


def walk(nodes: list[dict]):
    """按文档顺序遍历标题树（含列表项节点）。"""
    for node in nodes:
        yield node
        yield from walk(node["children"])


def parse(text: str, source: str) -> dict:
    roots: list[dict] = []
    stack: list[dict] = []          # 当前标题路径
    current: dict | None = None     # 属性/正文归属，可能是标题或列表项
    in_props = False

    for line in text.splitlines():
        head = HEAD_RE.match(line)
        if head:
            level = len(head.group(1))
            node = make_node(level, head.group(2))
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            if stack:
                node["parent"] = stack[-1]
                stack[-1]["children"].append(node)
            else:
                roots.append(node)
            stack.append(node)
            current = node
            in_props = False
            continue

        item = LIST_RE.match(line)
        if item and not in_props:
            level = stack[-1]["level"] + 1 if stack else 1
            node = make_node(level, item.group(1), kind="list")
            if stack:
                node["parent"] = stack[-1]
                stack[-1]["children"].append(node)
            else:
                roots.append(node)
            current = node       # 属性写列表项；列表项不进 stack（下面不会再挂标题）
            in_props = False
            continue

        prop = PROP_RE.match(line)
        if prop and prop.group(1).upper() == "PROPERTIES":
            in_props = True
            continue
        if prop and prop.group(1).upper() == "END" and in_props:
            in_props = False
            continue
        if in_props:
            if prop and current is not None:
                current["props"][prop.group(1).upper()] = prop.group(2)
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#+") or stripped.startswith(": "):
            continue
        if current is not None:
            current["desc"].append(stripped)

    nodes = list(walk(roots))

    # 订阅源按特征判定，与标题级别无关：有 Org 链接 + 有 :URL:
    for node in nodes:
        url = (node["props"].get("URL") or "").strip()
        node["maybe_feed"] = bool(node["link"] and url)

    # 分类判定：级别只在这里起作用
    level1 = [n for n in roots if n["kind"] == "head" and n["level"] == 1]
    if len(level1) > 1:
        category_nodes = level1
    elif len(level1) == 1:
        top = level1[0]
        category_nodes = [top]
        for child in top["children"]:
            if child["kind"] == "head" and child["level"] == 2:
                if sum(1 for c in child["children"] if c["maybe_feed"]) >= 2:
                    category_nodes.append(child)
    else:
        category_nodes = [n for n in roots
                          if sum(1 for c in n["children"] if c["maybe_feed"]) >= 2]
        if not category_nodes:
            category_nodes = [n for n in roots
                              if any(c["maybe_feed"] for c in n["children"])]

    for node in category_nodes:
        node["is_category"] = True

    def nearest_category(node: dict) -> dict | None:
        parent = node["parent"]
        while parent is not None:
            if parent["is_category"]:
                return parent
            parent = parent["parent"]
        return None

    # 输出：源归到最近的分类祖先；分类本身不作为源
    feeds = []
    for node in nodes:
        if not node["maybe_feed"] or node["is_category"]:
            continue
        url = node["props"]["URL"].strip()
        link = node["link"] or (node["props"].get("LINK") or "").strip() or url
        interval_raw = (node["props"].get("INTERVAL") or "").strip()
        try:
            interval = max(600, int(interval_raw)) if interval_raw else 14400
        except ValueError:
            interval = 14400
        cat = nearest_category(node)
        feeds.append({
            "slug": slug(url),
            "title": node["title"],
            "url": url,
            "link": link,
            "category": cat["title"] if cat else "未分类",
            "description": markup_to_html("\n".join(node["desc"]).strip()),
            "tags": node["tags"],
            "enabled": bool_value(node["props"].get("ENABLED")),
            "interval": interval,
        })

    categories = [{
        "name": node["title"],
        "description": markup_to_html("\n".join(node["desc"]).strip()),
    } for node in category_nodes]

    return {
        "source": source,
        "parsed_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "categories": categories,
        "feeds": feeds,
    }


def main() -> int:
    src = os.environ.get("FF_SOURCE")
    out = os.environ.get("FF_OUT")
    if not src or not out:
        print("需要设置 FF_SOURCE 和 FF_OUT 环境变量", file=sys.stderr)
        return 1
    text = Path(src).read_text(encoding="utf-8")
    payload = parse(text, src)
    Path(out).write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"ff-parse(py): 已解析 {src} -> {len(payload['feeds'])} 个源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
