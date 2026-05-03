#!/usr/bin/env python3
"""
Read RSS/Atom feed URLs from an OPML file, keep entries matching perovskite-related
keywords, and generate a new RSS file that can be subscribed to in Zotero/Inoreader.
"""

import argparse
import hashlib
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator

DEFAULT_OPML = "Inoreader Feeds 20260502.opml"
DEFAULT_OUTPUT = "filtered_perovskite.xml"

PV_HINTS = [
    "solar", "photovoltaic", "photovoltaics", "cell", "cells", "module", "modules",
    "tandem", "silicon", "interface", "passivation", "stability", "carrier",
    "hole", "electron", "transport", "film", "films", "crystal", "crystals",
    "light-emitting", "led", "optoelectronic", "optoelectronics", "semiconductor",
]


def read_word_list(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    words = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


def clean_text(value) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_text(entry) -> str:
    parts = [
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("description", ""),
        entry.get("subtitle", ""),
    ]

    if entry.get("content"):
        for c in entry.get("content", []):
            if isinstance(c, dict):
                parts.append(c.get("value", ""))
            else:
                parts.append(str(c))

    if entry.get("authors"):
        for a in entry.get("authors", []):
            if isinstance(a, dict):
                parts.append(a.get("name", ""))
            else:
                parts.append(str(a))

    if entry.get("tags"):
        for t in entry.get("tags", []):
            if isinstance(t, dict):
                parts.append(t.get("term", ""))
            else:
                parts.append(str(t))

    return clean_text(" ".join(parts))


def contains_keyword(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def is_relevant(text: str, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    lower = text.lower()
    if not contains_keyword(lower, include_keywords):
        return False

    # Do not over-filter photovoltaics/optoelectronics papers that mention phrases like oxide perovskite.
    has_pv_hint = any(h in lower for h in PV_HINTS)
    has_exclusion = contains_keyword(lower, exclude_keywords)
    if has_exclusion and not has_pv_hint:
        return False
    return True


def extract_rss_urls_from_opml(opml_file: str) -> list[dict]:
    tree = ET.parse(opml_file)
    root = tree.getroot()
    feeds = []
    seen = set()
    for outline in root.iter("outline"):
        xml_url = outline.attrib.get("xmlUrl")
        if not xml_url or xml_url in seen:
            continue
        seen.add(xml_url)
        feeds.append({
            "title": outline.attrib.get("title") or outline.attrib.get("text") or xml_url,
            "xmlUrl": xml_url,
            "htmlUrl": outline.attrib.get("htmlUrl", ""),
        })
    return feeds


def fetch_feed(url: str, timeout: int = 25):
    headers = {
        "User-Agent": "perovskite-rss-filter/1.0 (+https://github.com/)"
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return feedparser.parse(response.content)


def parse_entry_date(entry) -> datetime:
    for key in ["published", "updated", "created"]:
        if entry.get(key):
            try:
                dt = date_parser.parse(entry.get(key))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    for key in ["published_parsed", "updated_parsed", "created_parsed"]:
        if entry.get(key):
            try:
                return datetime(*entry.get(key)[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def stable_guid(entry, link: str, title: str) -> str:
    raw = entry.get("id") or entry.get("guid") or link or title
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--opml", default=DEFAULT_OPML)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--include", default="include_keywords.txt")
    parser.add_argument("--exclude", default="exclude_keywords.txt")
    parser.add_argument("--max-items", type=int, default=500)
    parser.add_argument("--site-url", default="https://example.com/filtered_perovskite.xml")
    parser.add_argument("--sleep", type=float, default=0.5, help="seconds between feed requests")
    args = parser.parse_args()

    include_keywords = read_word_list(args.include)
    exclude_keywords = read_word_list(args.exclude)
    if not include_keywords:
        print("No include keywords found.", file=sys.stderr)
        sys.exit(1)

    feeds = extract_rss_urls_from_opml(args.opml)
    print(f"Found {len(feeds)} feeds in OPML.")

    matched = []
    seen = set()
    failed = []

    for i, feed_info in enumerate(feeds, 1):
        url = feed_info["xmlUrl"]
        try:
            parsed = fetch_feed(url)
            source_title = clean_text(parsed.feed.get("title")) or feed_info["title"]
            print(f"[{i}/{len(feeds)}] {source_title}: {len(parsed.entries)} entries")
        except Exception as exc:
            print(f"[{i}/{len(feeds)}] FAILED: {url} -- {exc}")
            failed.append((url, str(exc)))
            continue

        for entry in parsed.entries:
            title = clean_text(entry.get("title", "Untitled")) or "Untitled"
            link = entry.get("link", "")
            text = entry_text(entry)
            if not is_relevant(text, include_keywords, exclude_keywords):
                continue

            guid = stable_guid(entry, link, title)
            dedup_key = link or guid
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            summary = clean_text(entry.get("summary") or entry.get("description") or "")
            matched.append({
                "title": title,
                "link": link,
                "summary": summary,
                "date": parse_entry_date(entry),
                "source": source_title,
                "guid": guid,
            })
        time.sleep(args.sleep)

    matched.sort(key=lambda x: x["date"], reverse=True)
    matched = matched[: args.max_items]

    fg = FeedGenerator()
    fg.id(args.site_url)
    fg.title("Perovskite-filtered Journal RSS")
    fg.link(href=args.site_url, rel="self")
    fg.link(href=args.site_url.rsplit("/", 1)[0] + "/", rel="alternate")
    fg.description("Articles filtered from the imported journal RSS feeds by perovskite-related keywords.")
    fg.language("en")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for item in matched:
        fe = fg.add_entry()
        fe.id(item["guid"])
        fe.title(item["title"])
        if item["link"]:
            fe.link(href=item["link"])
        desc = f"Source: {item['source']}"
        if item["summary"]:
            desc += f"<br><br>{html.escape(item['summary'])}"
        fe.description(desc)
        fe.pubDate(item["date"])

    fg.rss_file(args.output, pretty=True)
    print(f"Matched {len(matched)} entries. Wrote {args.output}.")
    if failed:
        print(f"Failed feeds: {len(failed)}")
        Path("failed_feeds.txt").write_text("\n".join(f"{u}\t{e}" for u, e in failed), encoding="utf-8")


if __name__ == "__main__":
    main()
