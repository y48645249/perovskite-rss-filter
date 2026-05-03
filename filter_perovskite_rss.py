#!/usr/bin/env python3
"""
Read RSS/Atom feed URLs from an OPML file, keep entries matching keywords,
and generate a filtered RSS file while preserving as much original metadata as possible.

Designed for Zotero/Inoreader subscriptions.
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

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator

DEFAULT_OPML = "Inoreader Feeds 20260502.xml"
DEFAULT_OUTPUT = "filtered_perovskite.xml"

PV_HINTS = [
    "solar", "photovoltaic", "photovoltaics", "cell", "cells", "module", "modules",
    "tandem", "silicon", "interface", "passivation", "stability", "carrier",
    "hole", "electron", "transport", "film", "films", "crystal", "crystals",
    "light-emitting", "led", "optoelectronic", "optoelectronics", "semiconductor",
]

# Fields worth preserving in the generated RSS description when present.
PRESERVE_FIELDS = [
    "id", "guid", "link", "title", "subtitle",
    "published", "updated", "created",
    "author", "creator", "dc_creator",
    "doi", "prism_doi", "dc_identifier", "dc_identifier_uri", "arxiv_doi",
    "journal", "journal_title", "publication", "publicationname",
    "prism_publicationname", "prism_publicationName", "dc_source", "source",
    "prism_volume", "volume", "prism_number", "issue", "prism_startingpage", "prism_endingpage",
    "rights", "license", "publisher", "publisher_detail",
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
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean_text(v) for v in value)
    elif isinstance(value, dict):
        # Prefer common readable values.
        value = value.get("name") or value.get("title") or value.get("term") or value.get("value") or value.get("href") or str(value)
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def html_text(value) -> str:
    """Return HTML-safe text for descriptions."""
    return html.escape(clean_text(value))


def entry_text(entry) -> str:
    parts = []

    for key in ["title", "summary", "description", "subtitle"]:
        if entry.get(key):
            parts.append(clean_text(entry.get(key)))

    if entry.get("content"):
        for c in entry.get("content", []):
            if isinstance(c, dict):
                parts.append(clean_text(c.get("value", "")))
            else:
                parts.append(clean_text(c))

    for a in extract_authors(entry):
        parts.append(a)

    for t in extract_tags(entry):
        parts.append(t)

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
        "User-Agent": "perovskite-rss-filter/1.2 (+https://github.com/)"
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
    return hashlib.sha256(str(raw).encode("utf-8", errors="ignore")).hexdigest()


def extract_authors(entry) -> list[str]:
    """Extract author names from common RSS/Atom fields and keep order."""
    authors = []

    def add_author(value):
        if not value:
            return
        if isinstance(value, dict):
            value = value.get("name") or value.get("email") or value.get("href") or value.get("title") or ""
        value = clean_text(value)
        if not value:
            return
        # Split semicolon/and-separated author strings, but keep 'Last, First' intact.
        pieces = [p.strip() for p in re.split(r"\s*;\s*|\s+ and \s+", value) if p.strip()]
        for piece in pieces or [value]:
            if piece and piece not in authors:
                authors.append(piece)

    if entry.get("authors"):
        for a in entry.get("authors", []):
            add_author(a)

    for key in ["author", "dc_creator", "creator", "prism_authors", "authors_detail"]:
        value = entry.get(key)
        if not value:
            continue
        if isinstance(value, list):
            for v in value:
                add_author(v)
        else:
            add_author(value)

    return authors


def extract_tags(entry) -> list[str]:
    tags = []
    for t in entry.get("tags", []) or []:
        if isinstance(t, dict):
            term = clean_text(t.get("term") or t.get("label") or t.get("scheme") or "")
        else:
            term = clean_text(t)
        if term and term not in tags:
            tags.append(term)
    for key in ["category", "categories", "dc_subject", "subject"]:
        value = entry.get(key)
        if not value:
            continue
        if isinstance(value, list):
            vals = value
        else:
            vals = re.split(r"\s*;\s*|\s*,\s*", str(value))
        for v in vals:
            term = clean_text(v)
            if term and term not in tags:
                tags.append(term)
    return tags


def extract_doi_from_text(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", str(text), flags=re.I)
    if not match:
        return ""
    return match.group(0).rstrip(".);,]}")


def extract_doi(entry, combined_text: str = "") -> str:
    candidate_keys = [
        "doi", "prism_doi", "dc_identifier", "dc_identifier_uri", "citation_doi",
        "arxiv_doi", "id", "guid", "link",
    ]
    for key in candidate_keys:
        value = entry.get(key)
        if not value:
            continue
        doi = extract_doi_from_text(value)
        if doi:
            return doi
    return extract_doi_from_text(combined_text)


def extract_journal(entry, fallback_source: str = "") -> str:
    candidate_keys = [
        "prism_publicationname", "prism_publicationName", "publicationname", "publication",
        "journal", "journal_title", "dc_source", "source",
    ]
    for key in candidate_keys:
        value = entry.get(key)
        if not value:
            continue
        if isinstance(value, dict):
            value = value.get("title") or value.get("href") or value.get("name") or ""
        value = clean_text(value)
        if value:
            return value
    return clean_text(fallback_source)


def extract_summary(entry) -> str:
    for key in ["summary", "description", "subtitle"]:
        if entry.get(key):
            return clean_text(entry.get(key))
    if entry.get("content"):
        chunks = []
        for c in entry.get("content", []):
            if isinstance(c, dict):
                chunks.append(clean_text(c.get("value", "")))
            else:
                chunks.append(clean_text(c))
        return clean_text(" ".join(chunks))
    return ""


def simple_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        preferred = []
        for k in ["name", "title", "term", "value", "href", "email", "label"]:
            if value.get(k):
                preferred.append(clean_text(value.get(k)))
        return "; ".join(v for v in preferred if v) or clean_text(str(value))
    if isinstance(value, list):
        return "; ".join(simple_value(v) for v in value if simple_value(v))
    return clean_text(value)


def preserved_metadata_lines(entry, feed_info, source_title, journal, doi, authors, tags, date) -> list[str]:
    lines = []
    if authors:
        lines.append(("Authors", "; ".join(authors)))
    if journal:
        lines.append(("Journal", journal))
    if source_title and source_title != journal:
        lines.append(("Feed source", source_title))
    if feed_info.get("xmlUrl"):
        lines.append(("Original feed", feed_info["xmlUrl"]))
    if doi:
        lines.append(("DOI", doi))
    if date:
        lines.append(("Published", date.strftime("%Y-%m-%d")))
    if tags:
        lines.append(("Categories", "; ".join(tags)))

    # Preserve additional raw metadata not already represented above.
    already = {"title", "summary", "description", "content", "authors", "tags"}
    for key in PRESERVE_FIELDS:
        if key in already:
            continue
        value = entry.get(key)
        value = simple_value(value)
        if not value:
            continue
        # Avoid repeating obvious fields.
        if key in {"doi", "prism_doi"} and doi and doi in value:
            continue
        if key in {"source", "dc_source"} and journal and value == journal:
            continue
        label = key.replace("_", ":") if key.startswith(("dc_", "prism_")) else key
        lines.append((label, value))

    # Deduplicate by label+value while preserving order.
    out = []
    seen = set()
    for label, value in lines:
        key = (label.lower(), value)
        if key not in seen:
            seen.add(key)
            out.append((label, value))
    return out


def build_description(item) -> str:
    parts = []
    for label, value in item["metadata_lines"]:
        if label == "DOI":
            doi = html.escape(value)
            parts.append(f'<b>DOI:</b> <a href="https://doi.org/{doi}">{doi}</a>')
        elif str(value).startswith("http://") or str(value).startswith("https://"):
            safe = html.escape(value)
            parts.append(f'<b>{html.escape(label)}:</b> <a href="{safe}">{safe}</a>')
        else:
            parts.append(f"<b>{html.escape(label)}:</b> {html.escape(value)}")

    if item.get("summary"):
        parts.append(f"<br><b>Summary / Abstract:</b><br>{html.escape(item['summary'])}")

    return "<br>".join(parts)


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

            date = parse_entry_date(entry)
            doi = extract_doi(entry, text)
            journal = extract_journal(entry, source_title)
            authors = extract_authors(entry)
            tags = extract_tags(entry)
            summary = extract_summary(entry)

            # Use DOI as best deduplication key when available.
            guid = stable_guid(entry, link or (f"https://doi.org/{doi}" if doi else ""), title)
            dedup_key = doi.lower() if doi else (link or guid)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            metadata_lines = preserved_metadata_lines(
                entry=entry,
                feed_info=feed_info,
                source_title=source_title,
                journal=journal,
                doi=doi,
                authors=authors,
                tags=tags,
                date=date,
            )

            matched.append({
                "title": title,
                "link": link,
                "summary": summary,
                "date": date,
                "source": source_title,
                "journal": journal,
                "doi": doi,
                "authors": authors,
                "tags": tags,
                "guid": guid,
                "metadata_lines": metadata_lines,
            })
        time.sleep(args.sleep)

    matched.sort(key=lambda x: x["date"], reverse=True)
    matched = matched[: args.max_items]

    fg = FeedGenerator()
    fg.id(args.site_url)
    fg.title("Perovskite-filtered Journal RSS")
    fg.link(href=args.site_url, rel="self")
    fg.link(href=args.site_url.rsplit("/", 1)[0] + "/", rel="alternate")
    fg.description("Articles filtered from imported journal RSS feeds by keyword while preserving original metadata.")
    fg.language("en")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for item in matched:
        fe = fg.add_entry()
        fe.id(item["guid"])
        fe.title(item["title"])

        # Preserve author field for RSS readers that can show it.
        if item.get("authors"):
            try:
                fe.author(name="; ".join(item["authors"]))
            except Exception:
                pass

        # Preserve categories/tags when feedgen supports RSS category output.
        for tag in item.get("tags", [])[:20]:
            try:
                fe.category(term=tag)
            except Exception:
                pass

        if item["link"]:
            fe.link(href=item["link"])
        elif item.get("doi"):
            fe.link(href=f"https://doi.org/{item['doi']}")

        fe.description(build_description(item))
        fe.pubDate(item["date"])

    fg.rss_file(args.output, pretty=True)
    print(f"Matched {len(matched)} entries. Wrote {args.output}.")
    if failed:
        print(f"Failed feeds: {len(failed)}")
        Path("failed_feeds.txt").write_text("\n".join(f"{u}\t{e}" for u, e in failed), encoding="utf-8")


if __name__ == "__main__":
    main()
