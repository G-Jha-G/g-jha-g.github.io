"""Build a filtered research feed from arXiv, Crossref journals and Google Scholar alert emails.

Runs daily on GitHub Actions. Reads feed/config.yml, keeps a memory of items in feed/state.json,
and writes feed/feed.xml (RSS 2.0) and feed/index.html (a readable list).
"""
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import yaml

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.yml"
STATE = HERE / "state.json"
OUT_XML = HERE / "feed.xml"
OUT_HTML = HERE / "index.html"
UA = "research-feed/1.0 (+https://g-jha-g.github.io/feed/)"
NOW = dt.datetime.now(dt.timezone.utc)


# ---------- fetching ----------
def http_get(url, timeout=40, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network errors should never stop the whole build
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_arxiv(entry):
    q = urllib.parse.urlencode({"search_query": entry["query"], "sortBy": "submittedDate",
                                "sortOrder": "descending", "max_results": 200})
    data = http_get("https://export.arxiv.org/api/query?" + q)
    items = []
    for e in feedparser.parse(data).entries:
        aid = e.get("id", "").rsplit("/abs/", 1)[-1]
        aid = re.sub(r"v\d+$", "", aid)
        items.append({
            "key": "arxiv:" + aid,
            "title": clean(e.get("title")),
            "summary": clean(e.get("summary")),
            "link": "https://arxiv.org/abs/" + aid,
            "authors": ", ".join(a.get("name", "") for a in e.get("authors", [])[:6]),
            "source": "arXiv",
            "published": e.get("published", ""),
        })
    return items


def fetch_crossref(journal, email, days):
    since = (NOW - dt.timedelta(days=days)).date().isoformat()
    q = urllib.parse.urlencode({"filter": f"from-index-date:{since},type:journal-article",
                                "sort": "indexed", "order": "desc", "rows": 200,
                                "select": "DOI,title,abstract,author,URL,published,container-title",
                                "mailto": email})
    data = json.loads(http_get(f"https://api.crossref.org/journals/{journal['issn']}/works?{q}"))
    items = []
    for w in data.get("message", {}).get("items", []):
        doi = (w.get("DOI") or "").lower()
        if not doi:
            continue
        parts = (w.get("published") or {}).get("date-parts") or [[]]
        items.append({
            "key": "doi:" + doi,
            "title": clean(" ".join(w.get("title") or [])),
            "summary": clean(w.get("abstract")),
            "link": "https://doi.org/" + doi,
            "authors": ", ".join(f"{a.get('given', '')} {a.get('family', '')}".strip()
                                 for a in (w.get("author") or [])[:6]),
            "source": journal["name"],
            "published": "-".join(str(p) for p in parts[0]),
        })
    return items


SCHOLAR_LINK = re.compile(r'<a[^>]*class="gse_alrt_title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def scholar_target(href):
    href = html.unescape(href)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return qs.get("url", [href])[0]


def fetch_scholar(url):
    items = []
    for e in feedparser.parse(http_get(url)).entries:
        body = ""
        for c in e.get("content", []) or []:
            body += c.get("value", "")
        body = body or e.get("summary", "")
        found = SCHOLAR_LINK.findall(body)
        if not found:  # unknown email format: keep the whole email as one item
            items.append({"key": "scholar:" + (e.get("id") or e.get("link", "")),
                          "title": clean(e.get("title")), "summary": clean(body)[:600],
                          "link": e.get("link", url), "authors": "", "source": "Google Scholar alert",
                          "published": e.get("published", ""), "scholar": True})
            continue
        chunks = SCHOLAR_LINK.split(body)
        for i, (href, title) in enumerate(found):
            snippet = chunks[3 * i + 3] if 3 * i + 3 < len(chunks) else ""
            target = scholar_target(href)
            items.append({"key": "scholar:" + target, "title": clean(title),
                          "summary": clean(snippet)[:600], "link": target, "authors": "",
                          "source": "Google Scholar alert: " + clean(e.get("title"))[:80],
                          "published": e.get("published", ""), "scholar": True})
    return items


# ---------- scoring ----------
def compile_topics(topics):
    out = {}
    for name, words in topics.items():
        pats = []
        for w in words:
            w = w.lower().strip()
            star = w.endswith("*")
            core = re.escape(w.rstrip("*")).replace(r"\*", r"\w*").replace(r"\ ", r"[\s-]+")
            pats.append(r"\b" + core + (r"\w*" if star else r"\b"))
        out[name] = re.compile("|".join(pats), re.I)
    return out


def score(item, topics):
    hits, s = [], 0
    for name, pat in topics.items():
        if pat.search(item["title"]):
            s += 3
            hits.append(name)
        elif pat.search(item["summary"]):
            s += 1
            hits.append(name)
    if "Deep Learning" in hits and len(hits) > 1:
        s += 3
    if item.get("scholar"):
        s += 5
    return s, hits


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())[:120]


# ---------- output ----------
def write_rss(cfg, items):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
             f"<title>{escape(cfg['title'])}</title>",
             f"<link>{escape(cfg['site'])}</link>",
             f'<atom:link href="{escape(cfg["site"])}feed.xml" rel="self" type="application/rss+xml"/>',
             "<description>Daily filtered papers on downscaling, deep learning, climate modelling, "
             "and weather and S2S forecasting.</description>",
             f"<lastBuildDate>{format_datetime(NOW)}</lastBuildDate>"]
    for it in items:
        first = dt.datetime.fromisoformat(it["first_seen"])
        desc = f"{it['source']}. Topics: {', '.join(it['topics'])}. Score {it['score']}."
        if it["authors"]:
            desc = it["authors"] + ". " + desc
        if it["summary"]:
            desc += " " + it["summary"][:700]
        parts.append("<item>"
                     f"<title>{escape(it['title'])}</title>"
                     f"<link>{escape(it['link'])}</link>"
                     f'<guid isPermaLink="false">{escape(it["key"])}</guid>'
                     f"<pubDate>{format_datetime(first)}</pubDate>"
                     + "".join(f"<category>{escape(t)}</category>" for t in it["topics"]) +
                     f"<description>{escape(desc)}</description>"
                     "</item>")
    parts.append("</channel></rss>")
    OUT_XML.write_text("\n".join(parts), encoding="utf-8")


def write_html(cfg, items):
    rows = []
    for it in items:
        tags = "".join(f'<span class="t">{html.escape(t)}</span>' for t in it["topics"])
        rows.append(
            f'<li><a href="{html.escape(it["link"])}">{html.escape(it["title"])}</a>'
            f'<div class="m">{html.escape(it["source"])}, {it["first_seen"][:10]}{tags}</div>'
            + (f'<div class="a">{html.escape(it["authors"])}</div>' if it["authors"] else "")
            + "</li>")
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research feed</title><meta name="robots" content="noindex">
<link rel="alternate" type="application/rss+xml" href="feed.xml" title="Research feed">
<style>
body{{margin:0;font:16px/1.5 Georgia,serif;color:#101b2d;background:#fff}}
main{{max-width:860px;margin:0 auto;padding:32px 20px 60px}}
h1{{font:700 26px/1.2 Helvetica,Arial,sans-serif;margin:0 0 6px}}
p.s{{font:14px/1.5 Helvetica,Arial,sans-serif;color:#566375;margin:0 0 24px}}
ul{{list-style:none;margin:0;padding:0}} li{{padding:14px 0;border-top:1px solid #dde4ec}}
li a{{font:600 17px/1.35 Helvetica,Arial,sans-serif;color:#101b2d;text-decoration:none}} li a:hover{{text-decoration:underline}}
.m,.a{{font:13px/1.5 Helvetica,Arial,sans-serif;color:#566375;margin-top:4px}}
.t{{display:inline-block;margin-left:8px;padding:1px 6px;border:1px solid #dde4ec;border-radius:3px;font-size:12px}}
</style></head><body><main>
<h1>Research feed</h1>
<p class="s">{len(items)} papers on downscaling, deep learning, climate modelling, and weather and S2S forecasting.
Updated {NOW:%d %b %Y}. Subscribe in any reader: <a href="feed.xml">feed.xml</a>.</p>
<ul>{''.join(rows)}</ul></main></body></html>"""
    OUT_HTML.write_text(page, encoding="utf-8")


# ---------- main ----------
def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    topics = compile_topics(cfg["topics"])
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    fresh, errors = [], []

    for a in cfg.get("arxiv") or []:
        try:
            fresh += fetch_arxiv(a)
            time.sleep(3)  # arXiv asks for a pause between calls
        except Exception as e:
            errors.append(f"{a['name']}: {e}")
    for j in cfg.get("journals") or []:
        try:
            fresh += fetch_crossref(j, cfg.get("contact_email", ""), cfg.get("keep_days", 30))
        except Exception as e:
            errors.append(f"{j['name']}: {e}")
    for url in cfg.get("scholar_feeds") or []:
        try:
            fresh += fetch_scholar(url)
        except Exception as e:
            errors.append(f"Scholar feed: {e}")

    seen_titles = {norm_title(v["title"]): k for k, v in state.items()}
    added = 0
    for it in fresh:
        if not it["title"]:
            continue
        s, hits = score(it, topics)
        if it.get("scholar"):
            hits = hits or ["Scholar alert"]  # alerts are already tailored to you, so always keep them
        elif s < cfg.get("min_score", 3) or not hits:
            continue
        nt = norm_title(it["title"])
        key = it["key"] if it["key"] in state else seen_titles.get(nt, it["key"])
        if key in state:  # already known: keep first-seen date, upgrade score if higher
            if s > state[key]["score"]:
                state[key].update(score=s, topics=hits)
            continue
        it.update(score=s, topics=hits, first_seen=NOW.isoformat(timespec="seconds"))
        it.pop("scholar", None)
        state[it["key"]] = it
        seen_titles[nt] = it["key"]
        added += 1

    cutoff = NOW - dt.timedelta(days=cfg.get("keep_days", 30))
    state = {k: v for k, v in state.items() if dt.datetime.fromisoformat(v["first_seen"]) >= cutoff}
    items = sorted(state.values(), key=lambda v: (v["first_seen"][:10], v["score"]), reverse=True)
    items = items[: cfg.get("max_items", 250)]

    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    write_rss(cfg, items)
    write_html(cfg, items)
    print(f"fetched {len(fresh)}, added {added}, feed has {len(items)} items")
    for e in errors:
        print("warning:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
