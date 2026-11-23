"""Build a personal research feed: papers, recommendations, citations of your work, and postdoc job ads.

Runs daily on GitHub Actions. Reads feed/config.yml, keeps a memory of items in feed/state.json,
and writes feed/feed.xml (RSS 2.0) and feed/index.html (the reading desk).

Ranking is a hybrid of two signals:
  1. similarity: how close a paper's title and abstract are to your profile (your papers, seed papers,
     your Zotero library, your stated interests and papers you starred), using sentence embeddings
     with a TF-IDF fallback;
  2. keywords: topic words in config.yml, with personal boosts and downweights.
"""
import datetime as dt
import html
import json
import os
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
CACHE = HERE / "profile_cache.json"
LIKES = HERE / "likes.json"
OUT_XML = HERE / "feed.xml"
OUT_HTML = HERE / "index.html"
UA = "research-feed/2.0 (+https://g-jha-g.github.io/feed/)"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
S2 = "https://api.semanticscholar.org"
S2_KEY = os.environ.get("S2_API_KEY", "")


# ---------- http ----------
def http(url, data=None, timeout=40, retries=3, headers=None):
    last = None
    h = {"User-Agent": UA}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network errors should never stop the whole build
            last = e
            wait = 20 if "429" in str(e) else 3 * (attempt + 1)
            time.sleep(wait)
    raise last


def s2(path, data=None, params=None):
    url = S2 + path + ("?" + urllib.parse.urlencode(params) if params else "")
    out = json.loads(http(url, data=data, headers={"x-api-key": S2_KEY} if S2_KEY else None))
    time.sleep(1.2)  # stay under the shared rate limit
    return out


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return re.sub(r"^abstract[\s:.]*", "", text, flags=re.I)


# ---------- sources ----------
def fetch_arxiv(entry):
    q = urllib.parse.urlencode({"search_query": entry["query"], "sortBy": "submittedDate",
                                "sortOrder": "descending", "max_results": entry.get("max", 200)})
    items = []
    for e in feedparser.parse(http("https://export.arxiv.org/api/query?" + q)).entries:
        aid = re.sub(r"v\d+$", "", e.get("id", "").rsplit("/abs/", 1)[-1])
        items.append({"key": "arxiv:" + aid, "title": clean(e.get("title")), "summary": clean(e.get("summary")),
                      "link": "https://arxiv.org/abs/" + aid,
                      "authors": ", ".join(a.get("name", "") for a in e.get("authors", [])[:8]),
                      "source": "arXiv", "published": e.get("published", "")[:10]})
    return items


def fetch_crossref(journal, email, days):
    since = (NOW - dt.timedelta(days=days)).date().isoformat()
    q = urllib.parse.urlencode({"filter": f"from-index-date:{since},type:journal-article",
                                "sort": "indexed", "order": "desc", "rows": 200,
                                "select": "DOI,title,abstract,author,URL,published,container-title",
                                "mailto": email})
    data = json.loads(http(f"https://api.crossref.org/journals/{journal['issn']}/works?{q}"))
    items = []
    for w in data.get("message", {}).get("items", []):
        doi = (w.get("DOI") or "").lower()
        if not doi:
            continue
        parts = (w.get("published") or {}).get("date-parts") or [[]]
        items.append({"key": "doi:" + doi, "title": clean(" ".join(w.get("title") or [])),
                      "summary": clean(w.get("abstract")), "link": "https://doi.org/" + doi,
                      "authors": ", ".join(f"{a.get('given', '')} {a.get('family', '')}".strip()
                                           for a in (w.get("author") or [])[:8]),
                      "source": journal["name"], "published": "-".join(str(p) for p in parts[0])})
    return items


SCHOLAR_LINK = re.compile(r'<a[^>]*class="gse_alrt_title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def fetch_scholar(url):
    items = []
    for e in feedparser.parse(http(url)).entries:
        body = "".join(c.get("value", "") for c in e.get("content", []) or []) or e.get("summary", "")
        found = SCHOLAR_LINK.findall(body)
        chunks = SCHOLAR_LINK.split(body)
        for i, (href, title) in enumerate(found):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query)
            target = qs.get("url", [html.unescape(href)])[0]
            snippet = chunks[3 * i + 3] if 3 * i + 3 < len(chunks) else ""
            items.append({"key": "scholar:" + target, "title": clean(title), "summary": clean(snippet)[:600],
                          "link": target, "authors": "", "source": "Google Scholar alert",
                          "published": "", "scholar": True})
    return items


def s2_item(p, kind):
    ext = p.get("externalIds") or {}
    if ext.get("ArXiv"):
        key, link = "arxiv:" + ext["ArXiv"], "https://arxiv.org/abs/" + ext["ArXiv"]
    elif ext.get("DOI"):
        key, link = "doi:" + ext["DOI"].lower(), "https://doi.org/" + ext["DOI"].lower()
    else:
        key, link = "s2:" + p["paperId"], "https://www.semanticscholar.org/paper/" + p["paperId"]
    return {"key": key, "title": clean(p.get("title")), "summary": clean(p.get("abstract")), "link": link,
            "authors": ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:8]),
            "source": p.get("venue") or "Semantic Scholar", "published": p.get("publicationDate") or str(p.get("year") or ""),
            "kind": kind}


S2_FIELDS = "paperId,title,abstract,authors,venue,year,publicationDate,externalIds"


def fetch_jobs(url, title_pat, topic_pat):
    items = []
    f = feedparser.parse(http(url))
    src = f.feed.get("title") or urllib.parse.urlparse(url).netloc
    for e in f.entries:
        title, desc = clean(e.get("title")), clean(e.get("summary") or e.get("description"))
        if not title_pat.search(title + " " + desc[:300]) or not topic_pat.search(title + " " + desc):
            continue
        link = e.get("link", url)
        items.append({"key": "job:" + link, "title": title, "summary": desc[:900], "link": link, "authors": "",
                      "source": src,
                      "published": (e.get("published") or "")[:16], "kind": "job"})
    return items


# ---------- keyword scoring ----------
def compile_words(words):
    pats = []
    for w in words:
        w = w.lower().strip()
        star = w.endswith("*")
        core = re.escape(w.rstrip("*")).replace(r"\*", r"\w*").replace(r"\ ", r"[\s-]+")
        pats.append(r"\b" + core + (r"\w*" if star else r"\b"))
    return re.compile("|".join(pats), re.I) if pats else re.compile(r"(?!x)x")


def keyword_score(item, topics, boost, down):
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
    text = item["title"] + " " + item["summary"]
    item["boosts"] = [w.rstrip("*") for w, p in boost if p.search(text)]
    s += min(2 * len(item["boosts"]), 4)
    item["down"] = any(p.search(text) for p in down)
    if item["down"]:
        s -= 3
    return s, hits


def watched_in(authors, people):
    low = authors.lower()
    return [p["name"] for p in people if p["name"].lower() in low]


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())[:120]


# ---------- profile ----------
def parse_bib(path):
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    docs = []
    for entry in re.split(r"\n@", text):
        t = re.search(r"\btitle\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", entry, re.S | re.I)
        a = re.search(r"\babstract\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", entry, re.S | re.I)
        if t:
            title = re.sub(r"[{}]", "", t.group(1)).strip()
            docs.append({"title": title, "text": title + ". " + (re.sub(r"[{}]", "", a.group(1)) if a else ""),
                         "src": "your Zotero library"})
    return docs


def build_profile(cfg, errors):
    """Collect texts that describe your taste. Semantic Scholar lookups are cached for a week."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    ids = [("mine", i) for i in cfg.get("my_papers") or []] + [("seed", i) for i in cfg.get("seed_papers") or []]
    stale = not cache.get("date") or (NOW - dt.datetime.fromisoformat(cache["date"])).days >= 7
    if ids and (stale or set(cache.get("ids", [])) != {i for _, i in ids}):
        try:
            got = s2("/graph/v1/paper/batch", data={"ids": [i for _, i in ids]}, params={"fields": S2_FIELDS})
            cache = {"date": NOW.isoformat(), "ids": [i for _, i in ids], "papers": []}
            for (role, i), p in zip(ids, got):
                if p:
                    cache["papers"].append({"role": role, "id": p["paperId"], "title": p.get("title") or "",
                                            "abstract": p.get("abstract") or ""})
                else:
                    errors.append(f"Semantic Scholar does not know {i}")
            CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            errors.append(f"profile lookup: {e}")
    pos, neg = [], []
    for p in cache.get("papers", []):
        pos.append({"title": p["title"], "text": p["title"] + ". " + p["abstract"],
                    "src": "your paper" if p["role"] == "mine" else "a seed paper"})
    for t in cfg.get("interests") or []:
        pos.append({"title": t["name"], "text": t["name"] + ". " + t["text"], "src": "your interests"})
    pos += parse_bib(HERE / (cfg.get("zotero_bib") or "library.bib"))
    if LIKES.exists():
        try:
            likes = json.loads(LIKES.read_text(encoding="utf-8"))
            for it in (likes.get("star") or {}).values():
                pos.append({"title": it["title"], "text": it["title"] + ". " + (it.get("summary") or ""),
                            "src": "a paper you starred"})
            for it in (likes.get("no") or {}).values():
                neg.append({"title": it["title"], "text": it["title"] + ". " + (it.get("summary") or "")})
        except Exception as e:
            errors.append(f"likes.json: {e}")
    my_ids = [p["id"] for p in cache.get("papers", []) if p["role"] == "mine"]
    seed_ids = [p["id"] for p in cache.get("papers", [])]
    titles = {p["id"]: p["title"] for p in cache.get("papers", [])}
    return pos, neg, my_ids, seed_ids, titles


# ---------- similarity ----------
def embed(texts, model_name):
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name)
        return model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False), "embeddings"
    except Exception as e:
        print("embeddings unavailable, using TF-IDF:", e, file=sys.stderr)
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english", min_df=1)
        return vec.fit_transform(texts), "tfidf"


def similarity(items, pos, neg, model_name):
    """Mean of the 3 closest profile texts, minus half the closest disliked text. Also returns the nearest."""
    import numpy as np
    if not pos or not items:
        return
    texts = [d["text"][:2000] for d in pos] + [d["text"][:2000] for d in neg] + \
            [(it["title"] + ". " + it["summary"])[:2000] for it in items]
    m, method = embed(texts, model_name)
    P, N, X = m[:len(pos)], m[len(pos):len(pos) + len(neg)], m[len(pos) + len(neg):]
    sp = X @ P.T
    sp = sp.toarray() if hasattr(sp, "toarray") else np.asarray(sp)
    k = min(3, sp.shape[1])
    top = np.sort(sp, axis=1)[:, -k:].mean(axis=1)
    nearest = sp.argmax(axis=1)
    if len(neg):
        sn = X @ N.T
        sn = sn.toarray() if hasattr(sn, "toarray") else np.asarray(sn)
        top = top - 0.5 * np.clip(sn.max(axis=1) - top, 0, None)
    for it, s, j in zip(items, top, nearest):
        it["sim"] = round(float(s), 4)
        it["why"] = {"title": pos[j]["title"][:140], "src": pos[j]["src"]}
    print(f"similarity by {method} for {len(items)} items against {len(pos)} profile texts")


def finalize(items, cfg):
    """Turn similarity and keywords into one 0-100 score, normalised over everything collected this run."""
    import numpy as np
    w = float((cfg.get("similarity") or {}).get("weight", 0.6))
    sims = np.array([it.get("sim", 0.0) for it in items if "sim" in it])
    lo, hi = (np.percentile(sims, 50), np.percentile(sims, 98)) if len(sims) > 20 else (0.0, 1.0)
    for it in items:
        sim_n = float(np.clip((it.get("sim", lo) - lo) / max(hi - lo, 1e-6), 0, 1)) if "sim" in it else 0.0
        kw_n = float(np.clip(it.get("kw", 0) / 15, 0, 1))
        base = (w * sim_n + (1 - w) * kw_n) if "sim" in it else kw_n
        sc = 100 * base + (10 if it.get("watch") else 0) + (10 if it.get("scholar") else 0)
        if it.get("down"):
            sc *= 0.7
        it["sim_n"] = round(sim_n, 3)
        it["score"] = int(round(min(sc, 100)))


# ---------- output ----------
def write_rss(cfg, items):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
             f"<title>{escape(cfg['title'])}</title>", f"<link>{escape(cfg['site'])}</link>",
             f'<atom:link href="{escape(cfg["site"])}feed.xml" rel="self" type="application/rss+xml"/>',
             "<description>Daily ranked papers on downscaling, deep learning, climate modelling, "
             "and weather and S2S forecasting.</description>",
             f"<lastBuildDate>{format_datetime(NOW)}</lastBuildDate>"]
    for it in items:
        if it.get("kind", "paper") != "paper" or not it.get("pick"):
            continue
        desc = f"{it['source']}. Score {it['score']}. Topics: {', '.join(it['topics'])}."
        if it.get("why"):
            desc += f" Close to {it['why']['src']}: {it['why']['title']}."
        if it["authors"]:
            desc = it["authors"] + ". " + desc
        desc += " " + it["summary"][:700]
        parts.append("<item>" f"<title>{escape(it['title'])}</title><link>{escape(it['link'])}</link>"
                     f'<guid isPermaLink="false">{escape(it["key"])}</guid>'
                     f"<pubDate>{format_datetime(dt.datetime.fromisoformat(it['first_seen']))}</pubDate>"
                     + "".join(f"<category>{escape(t)}</category>" for t in it["topics"]) +
                     f"<description>{escape(desc)}</description></item>")
    parts.append("</channel></rss>")
    OUT_XML.write_text("\n".join(parts), encoding="utf-8")


def write_html(cfg, items, people, profile_size):
    keep = ("key", "title", "summary", "link", "authors", "source", "published", "first_seen", "score",
            "topics", "watch", "boosts", "why", "kind", "pick", "cites")
    data = [{k: it.get(k) for k in keep} for it in items]
    for d in data:
        d["summary"] = (d["summary"] or "")[:1600]
    deadlines = []
    for d in cfg.get("deadlines") or []:
        try:
            if dt.date.fromisoformat(str(d["date"])) >= NOW.date():
                deadlines.append({k: str(v) for k, v in d.items()})
        except Exception:
            pass
    deadlines.sort(key=lambda d: d["date"])
    payload = json.dumps({"updated": NOW.isoformat(timespec="minutes"), "topics": list(cfg["topics"]),
                          "people": [{"name": p["name"], "where": p.get("where", "")} for p in people],
                          "deadlines": deadlines, "profile": profile_size, "items": data},
                         ensure_ascii=False).replace("</", "<\\/")
    page = (HERE / "template.html").read_text(encoding="utf-8").replace("/*DATA*/null", payload)
    OUT_HTML.write_text(page, encoding="utf-8")


# ---------- main ----------
def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    topics = {n: compile_words(w) for n, w in cfg["topics"].items()}
    boost = [(w, compile_words([w])) for w in cfg.get("boost") or []]
    down = [compile_words([w]) for w in cfg.get("downweight") or []]
    people = cfg.get("watch_people") or []
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    errors, fresh = [], []

    pos, neg, my_ids, seed_ids, my_titles = build_profile(cfg, errors)

    for a in cfg.get("arxiv") or []:
        try:
            fresh += fetch_arxiv(a)
            time.sleep(3)  # arXiv asks for a pause between calls
        except Exception as e:
            errors.append(f"{a['name']}: {e}")
    for p in people:
        if p.get("arxiv"):
            try:
                for it in fetch_arxiv({"query": p["arxiv"], "max": 15}):
                    it["watch_hit"] = True
                    fresh.append(it)
                time.sleep(3)
            except Exception as e:
                errors.append(f"{p['name']}: {e}")
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

    rec_cfg, cite_cfg = cfg.get("recommendations") or {}, cfg.get("citations") or {}
    if rec_cfg.get("enabled", True) and seed_ids:
        try:
            got = s2("/recommendations/v1/papers/", data={"positivePaperIds": seed_ids[:50]},
                     params={"fields": S2_FIELDS, "limit": rec_cfg.get("limit", 100)})
            fresh += [s2_item(p, "rec") for p in got.get("recommendedPapers", []) if p.get("title")]
        except Exception as e:
            errors.append(f"recommendations: {e}")
    if cite_cfg.get("enabled", True):
        for pid in my_ids:
            try:
                got = s2(f"/graph/v1/paper/{pid}/citations", params={"fields": S2_FIELDS, "limit": 100})
                for c in got.get("data", []):
                    p = c.get("citingPaper") or {}
                    if p.get("title"):
                        it = s2_item(p, "cite")
                        it["cites"] = [my_titles.get(pid, "")]
                        fresh.append(it)
            except Exception as e:
                errors.append(f"citations: {e}")

    jobs_cfg = cfg.get("jobs") or {}
    if jobs_cfg.get("feeds"):
        tp, kp = compile_words(jobs_cfg.get("title_words") or ["postdoc*"]), compile_words(jobs_cfg.get("topic_words") or [])
        for url in jobs_cfg["feeds"]:
            try:
                fresh += fetch_jobs(url, tp, kp)
            except Exception as e:
                errors.append(f"jobs {url}: {e}")

    # keyword pass and de-duplication against memory
    seen_titles = {norm_title(v["title"]): k for k, v in state.items()}
    cands = []
    for it in fresh:
        if not it["title"]:
            continue
        it.setdefault("kind", "paper")
        nt = norm_title(it["title"])
        key = it["key"] if it["key"] in state else seen_titles.get(nt, it["key"])
        if key in state:
            if it["kind"] == "cite" and state[key].get("kind") != "cite":  # a known paper turns out to cite you
                state[key].update(kind="cite", cites=it.get("cites"))
            continue
        if any(c is not it and norm_title(c["title"]) == nt for c in cands):
            continue
        it["kw"], it["topics"] = keyword_score(it, topics, boost, down)
        it["watch"] = watched_in(it["authors"], people)
        cands.append(it)

    # similarity for new candidates and everything remembered, so ranking stays consistent
    stored = list(state.values())
    pool = [c for c in cands if c["kind"] in ("paper", "rec", "cite")] + [s for s in stored if s.get("kind", "paper") != "job"]
    for s in stored:
        s.setdefault("kind", "paper")
        if s["kind"] != "job":
            s["kw"], kw_topics = keyword_score(s, topics, boost, down)
            s["topics"] = kw_topics or s.get("topics") or []
            s["watch"] = watched_in(s["authors"], people)
    similarity(pool, pos, neg, (cfg.get("similarity") or {}).get("model", "sentence-transformers/all-MiniLM-L6-v2"))
    finalize(pool, cfg)

    # admission rules
    min_kw = cfg.get("min_score", 3)
    sem_gate = float((cfg.get("similarity") or {}).get("admit_without_keywords", 0.75))
    added = []
    for it in cands:
        k = it["kind"]
        if k == "paper":
            ok = it.get("scholar") or (it["watch"] and (it.get("watch_hit") or it["kw"] >= 1)) \
                 or (it["kw"] >= min_kw and it["topics"]) or it.get("sim_n", 0) >= sem_gate
        elif k == "rec":
            ok = it.get("sim_n", 0) >= 0.3
        else:  # citations and jobs are always kept
            ok = True
        if not ok:
            continue
        if not it["topics"]:
            it["topics"] = ["Scholar alert"] if it.get("scholar") else (["Watched author"] if it["watch"] else ["Close to your work"]) \
                if k == "paper" else []
        it["first_seen"] = NOW.isoformat(timespec="seconds")
        for x in ("watch_hit", "scholar"):
            it.pop(x, None)
        added.append(it)
    # keep only the best recommendations per run so they do not flood the desk
    recs = sorted([a for a in added if a["kind"] == "rec"], key=lambda a: -a["score"])
    drop = {id(a) for a in recs[rec_cfg.get("keep", 15):]}
    added = [a for a in added if id(a) not in drop]
    for it in added:
        state[it["key"]] = it

    # daily picks: the best new papers today, plus anything by people you watch
    n_pick = int(cfg.get("daily_picks", 10))
    by_day = {}
    for v in state.values():
        if v.get("kind", "paper") == "paper":
            by_day.setdefault(v["first_seen"][:10], []).append(v)
    for day in by_day.values():
        day.sort(key=lambda v: -v["score"])
        for i, v in enumerate(day):
            v["pick"] = i < n_pick or bool(v.get("watch"))

    # expiry: papers and recommendations after keep_days, jobs after jobs.keep_days, citations never
    def alive(v):
        age = (NOW - dt.datetime.fromisoformat(v["first_seen"])).days
        kind = v.get("kind", "paper")
        return kind == "cite" or age <= (jobs_cfg.get("keep_days", 45) if kind == "job" else cfg.get("keep_days", 30))
    state = {k: v for k, v in state.items() if alive(v)}
    for v in state.values():
        for x in ("kw", "sim_n", "down"):
            v.pop(x, None)
    items = sorted(state.values(), key=lambda v: (v["first_seen"][:10], v.get("score", 0)), reverse=True)
    papers = [v for v in items if v.get("kind", "paper") == "paper"][: cfg.get("max_items", 300)]
    items = papers + [v for v in items if v.get("kind", "paper") != "paper"]

    STATE.write_text(json.dumps({v["key"]: v for v in items}, indent=1, ensure_ascii=False), encoding="utf-8")
    write_rss(cfg, items)
    write_html(cfg, items, people, len(pos))
    kinds = {}
    for a in added:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print(f"fetched {len(fresh)}, added {kinds}, desk has {len(items)} items, profile {len(pos)} texts")
    for e in errors:
        print("warning:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
