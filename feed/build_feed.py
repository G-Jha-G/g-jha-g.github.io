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
                                "sortOrder": "descending", "max_results": entry.get("max", 200)})
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


def score(item, topics, extra=None):
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
    if extra:
        text = item["title"] + " " + item["summary"]
        boosts = [w for w, pat in extra["boost"] if pat.search(text)]
        s += min(2 * len(boosts), 4)
        if any(pat.search(text) for _, pat in extra["down"]):
            s -= 3
        item["boosts"] = boosts
    return s, hits


def word_pats(words):
    return [(w, compile_topics({"x": [w]})["x"]) for w in (words or [])]


def watched_in(authors, people):
    low = authors.lower()
    return [p["name"] for p in people if p["name"].lower() in low]


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


def write_html(cfg, items, people):
    keep = ("key", "title", "summary", "link", "authors", "source", "published",
            "first_seen", "score", "topics", "watch", "boosts")
    data = [{k: it.get(k) for k in keep} for it in items]
    for d in data:
        d["summary"] = re.sub(r"^abstract[\s:.]*", "", d["summary"] or "", flags=re.I)[:1600]
    payload = json.dumps({"updated": NOW.isoformat(timespec="minutes"),
                          "topics": list(cfg["topics"]),
                          "people": [{"name": p["name"], "where": p.get("where", "")} for p in people],
                          "items": data}, ensure_ascii=False).replace("</", "<\\/")
    page = (HERE / "template.html").read_text(encoding="utf-8").replace("/*DATA*/null", payload)
    OUT_HTML.write_text(page, encoding="utf-8")


# ---------- main ----------
def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    topics = compile_topics(cfg["topics"])
    extra = {"boost": word_pats(cfg.get("boost")), "down": word_pats(cfg.get("downweight"))}
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
    people = cfg.get("watch_people") or []
    for p in people:
        if not p.get("arxiv"):
            continue
        try:
            got = fetch_arxiv({"query": p["arxiv"], "max": 15})
            for it in got:
                it["watch_hit"] = True
            fresh += got
            time.sleep(3)
        except Exception as e:
            errors.append(f"{p['name']}: {e}")
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
        s, hits = score(it, topics, extra)
        watch = watched_in(it["authors"], people)
        it["watch"] = watch
        if it.get("scholar"):
            hits = hits or ["Scholar alert"]  # alerts are already tailored to you, so always keep them
        elif watch and (it.get("watch_hit") or s >= 1):
            hits = hits or ["Watched author"]  # papers by people you track are kept even with weak keyword hits
            s += 4
        elif s < cfg.get("min_score", 3) or not hits:
            continue
        it.pop("watch_hit", None)
        nt = norm_title(it["title"])
        key = it["key"] if it["key"] in state else seen_titles.get(nt, it["key"])
        if key in state:  # already known: keep first-seen date, upgrade score if higher
            if s > state[key]["score"]:
                state[key].update(score=s, topics=hits)
            state[key]["watch"] = watch
            continue
        it.update(score=s, topics=hits, first_seen=NOW.isoformat(timespec="seconds"))
        it.pop("scholar", None)
        state[it["key"]] = it
        seen_titles[nt] = it["key"]
        added += 1

    # re-rank everything already stored, so edits to boost, downweight or watch_people apply at once
    for v in state.values():
        if v["source"].startswith("Google Scholar"):
            continue
        s2, hits2 = score(v, topics, extra)
        v["watch"] = watched_in(v["authors"], people)
        v["score"] = s2 + (4 if v["watch"] else 0)
        v["topics"] = hits2 or v["topics"]

    cutoff = NOW - dt.timedelta(days=cfg.get("keep_days", 30))
    state = {k: v for k, v in state.items() if dt.datetime.fromisoformat(v["first_seen"]) >= cutoff}
    items = sorted(state.values(), key=lambda v: (v["first_seen"][:10], v["score"]), reverse=True)
    items = items[: cfg.get("max_items", 250)]

    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    write_rss(cfg, items)
    write_html(cfg, items, people)
    print(f"fetched {len(fresh)}, added {added}, feed has {len(items)} items")
    for e in errors:
        print("warning:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
