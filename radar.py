#!/usr/bin/env python3
"""Problem Radar: find problems people post about on Reddit (read-only).

Pipeline: collect (Reddit public RSS) -> enrich (engagement + top comments)
-> detect problems (stdlib heuristics, optional LLM refine) -> cluster -> data/problems.json

Stdlib only. Never posts, votes or replies anywhere.
"""
import html, json, math, os, re, sys, time, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
ATOM = {"a": "http://www.w3.org/2005/Atom"}
CFG = json.load(open(os.path.join(ROOT, "config.json")))
UA = CFG.get("user_agent", "problem-radar/0.1")
DELAY = float(CFG.get("request_delay_seconds", 2.5))
_last = [0.0]
LOG = []


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch(url, tries=3):
    """Polite GET: fixed delay between requests, backoff on 429/5xx."""
    for attempt in range(tries):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(8 * (attempt + 1))
                continue
            return code, ""
        except Exception as e:  # network error
            if attempt < tries - 1:
                time.sleep(4)
                continue
            return 0, str(e)
    return 0, ""


def clean_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<!-- SC_(ON|OFF) -->", " ", s)
    s = re.sub(r"submitted by.*$", " ", s, flags=re.S)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_feed(text, source):
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for e in root.findall("a:entry", ATOM):
        eid = (e.findtext("a:id", "", ATOM) or "").strip()
        if not eid.startswith("t3_"):
            continue
        link = e.find("a:link", ATOM)
        href = link.get("href") if link is not None else ""
        cat = e.find("a:category", ATOM)
        sub = cat.get("term") if cat is not None else ""
        author = (e.findtext("a:author/a:name", "", ATOM) or "").replace("/u/", "")
        out.append({
            "id": eid[3:],
            "title": html.unescape(e.findtext("a:title", "", ATOM) or "").strip(),
            "body": clean_html(e.findtext("a:content", "", ATOM)),
            "subreddit": sub,
            "author": author,
            "url": href,
            "created_utc": parse_ts(e.findtext("a:published", "", ATOM) or e.findtext("a:updated", "", ATOM) or ""),
            "sources": [source],
        })
    return out


# ---------------------------------------------------------------- collect
def collect():
    posts, stats = {}, {"requests": 0, "ok": 0, "failed": []}
    jobs = []
    for s in CFG["subreddits"]:
        for kind in CFG.get("listings", ["new"]):
            if kind == "new":
                jobs.append((f"r/{s} new", f"https://www.reddit.com/r/{s}/new/.rss?limit=50"))
            elif kind == "top_week":
                jobs.append((f"r/{s} top week", f"https://www.reddit.com/r/{s}/top/.rss?t=week&limit=50"))
    for q in CFG.get("searches", []):
        qs = urllib.parse.urlencode({"q": q, "sort": "new", "t": "month", "limit": 50})
        jobs.append((f"search: {q}", f"https://www.reddit.com/search.rss?{qs}"))
    for label, url in jobs:
        stats["requests"] += 1
        code, text = fetch(url)
        items = parse_feed(text, label) if code == 200 else []
        if code == 200:
            stats["ok"] += 1
        else:
            stats["failed"].append({"source": label, "status": code})
        log(f"[{code}] {label}: {len(items)}")
        for p in items:
            if p["id"] in posts:
                posts[p["id"]]["sources"] = sorted(set(posts[p["id"]]["sources"] + p["sources"]))
            else:
                posts[p["id"]] = p
    cutoff = time.time() - 86400 * CFG.get("max_age_days", 30)
    kept = [p for p in posts.values() if (p["created_utc"] or time.time()) >= cutoff]
    stats["unique_posts"] = len(posts)
    stats["kept_recent"] = len(kept)
    return kept, stats


def enrich_engagement(posts):
    """Score / comment counts from the public Arctic Shift archive (snapshot, may lag)."""
    ids = [p["id"] for p in posts]
    got = {}
    def grab(chunk_ids):
        url = "https://arctic-shift.photon-reddit.com/api/posts/ids?ids=" + ",".join(chunk_ids) + "&fields=id,score,num_comments"
        code, text = fetch(url)
        try:
            for d in json.loads(text).get("data") or []:
                got[d["id"]] = d
            return True
        except Exception:
            log(f"engagement chunk failed [{code}] size={len(chunk_ids)}")
            return False
    for i in range(0, len(ids), 100):
        chunk = [x for x in ids[i:i + 100] if re.fullmatch(r"[a-z0-9]{4,10}", x)]
        if chunk and not grab(chunk) and len(chunk) > 10:
            for j in range(0, len(chunk), 10):
                grab(chunk[j:j + 10])
    for p in posts:
        d = got.get(p["id"])
        p["score"] = d.get("score") if d else None
        p["num_comments"] = d.get("num_comments") if d else None
    return len(got)


METOO = re.compile(r"\b(same here|me too|same problem|same issue|following|\+1|also looking|i'?d pay|commenting to follow|did you find|any luck)\b", re.I)


def enrich_comments(post, n=8):
    url = post["url"].rstrip("/") + "/.rss?" + urllib.parse.urlencode({"limit": n, "sort": "top"})
    code, text = fetch(url)
    if code != 200:
        return False
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False
    comments = []
    for e in root.findall("a:entry", ATOM):
        if (e.findtext("a:id", "", ATOM) or "").startswith("t1_"):
            body = clean_html(e.findtext("a:content", "", ATOM))
            if body:
                comments.append({"author": (e.findtext("a:author/a:name", "", ATOM) or "").replace("/u/", ""), "text": body[:400]})
    post["top_comments"] = comments[:3]
    post["metoo"] = sum(1 for c in comments if METOO.search(c["text"]))
    if post.get("num_comments") is None:
        post["num_comments_seen"] = len(comments)
    return True


# ---------------------------------------------------------------- detect
P = [  # (regex, weight, kind)
    (r"\bis there (a|an|any) (tool|app|software|service|website|site|plugin|extension|platform|way|solution)\b", 4.5, "tool"),
    (r"\b(looking for|searching for|need) (a|an|some) (good |simple |cheap |free |better )?(tool|app|software|service|platform|solution|crm|plugin|system|way)\b", 4, "tool"),
    (r"\bany (good )?(tool|app|software|apps|tools|service)s? (that|for|to|which)\b", 3.5, "tool"),
    (r"\bwhat (tools?|apps?|software|stack|system) (do|does|are|should|would)\b", 3, "tool"),
    (r"\bwish (there was|there were|i had|someone would)\b", 4, "tool"),
    (r"\b(alternatives? to|replacement for|instead of using|switch(ing)? from)\b", 3.5, "alternative"),
    (r"\bany (recommendations|recs|suggestions)\b|\brecommend(ations?)? (for|on)\b|\bwhat do you (use|recommend)\b", 3, "tool"),
    (r"\bhow (do|did|can|should|would) (i|you|we|people|y'?all)\b", 2.5, "howto"),
    (r"\bwhat'?s the best way\b|\bbest way to\b", 3, "howto"),
    (r"\b(any (tips|advice)|need (help|advice)|advice needed|help me)\b", 2.5, "howto"),
    (r"\b(can'?t|cannot|couldn'?t) (figure out|find|get|seem to)\b", 3, "pain"),
    (r"\bfrustrat\w*", 3, "pain"),
    (r"\bstruggl\w*", 3, "pain"),
    (r"\bhate (when|that|how|it|doing|having)\b", 3, "pain"),
    (r"\b(sick (of|and tired)|fed up|tired of)\b", 3, "pain"),
    (r"\b(nightmare|drives? me (crazy|nuts)|killing me|losing my mind)\b", 3, "pain"),
    (r"\b(wast(e|ing|ed) (so much |hours|time|days)|takes (forever|hours|so long))\b", 3, "pain"),
    (r"\b(overwhelm\w*|burn(ed|t)? out|stuck|stressed)\b", 2.5, "pain"),
    (r"\b(tedious|painful|pain point|annoying|a mess|chaos|clunky|broken)\b", 2, "pain"),
    (r"\bmanually\b|\bspreadsheets?\b|\bcopy[- ]past(e|ing)\b", 1.5, "pain"),
    (r"\b(too expensive|can'?t afford|overpriced|price (hike|increase)|cheaper (option|alternative))\b", 3, "cost"),
]
P = [(re.compile(r, re.I), w, k) for r, w, k in P]
NEG = re.compile(r"\b(i|we) (built|made|launched|created|developed)\b|\b(check out|introducing|my (app|tool|saas|startup) is|feedback on my|roast my|giveaway|we'?re hiring|\[hiring\]|ama\b|case study|for sale|promo code|discount code|launching today)\b", re.I)
PERSONAL = re.compile(r"\b(boyfriend|girlfriend|husband|wife|marriage|marry|dating|divorce|my (mom|dad|parents|kids?)|pregnan\w*|therap\w*|medication|depress\w*|anxiety|suicid\w*|diagnos\w*|symptoms?|doctor|sex|religio\w*|pastor|church|grief|breakup|cheat(ed|ing))\b", re.I)
STRONG = re.compile(r"\b(desperate|urgent|please help|asap|losing (money|clients|customers)|can'?t (sleep|keep up)|killing|nightmare|hate|furious|seriously|honestly|exhausted|every (single )?(day|week|month))\b", re.I)
EXCLUDE = {x.lower() for x in CFG.get("exclude_subreddits", [])}
KIND_LABEL = {"tool": "Looking for a tool", "alternative": "Wants an alternative", "howto": "How-to / advice",
              "pain": "Frustration / pain", "cost": "Cost / pricing"}
TOPICS = {
    "Marketing & growth": r"\b(marketing|seo|ads?|leads?|traffic|audience|followers|newsletter|content|social media|linkedin|twitter|tiktok|instagram|outreach|cold email|growth)\b",
    "Sales & customers": r"\b(sales|customers?|clients?|crm|pipeline|churn|onboarding|support tickets?|reviews?)\b",
    "Money & admin": r"\b(invoice|invoices|invoicing|payments?|taxes?|bookkeeping|accounting|payroll|cash ?flow|budget|pricing|quickbooks|expenses?)\b",
    "Productivity & time": r"\b(productivity|focus|procrastinat\w*|todo|to-do|tasks?|calendar|schedul\w*|notes?|notion|habits?|time management|routine)\b",
    "Team & hiring": r"\b(hire|hiring|employees?|team|contractors?|freelancers?|manager|coworkers?|remote)\b",
    "Tech & dev": r"\b(code|coding|api|bug|deploy\w*|server|database|website|wordpress|shopify|hosting|python|javascript|ai|chatgpt|automation|zapier)\b",
    "Ecommerce & ops": r"\b(inventory|shipping|orders?|suppliers?|amazon|etsy|dropshipping|fulfillment|returns)\b",
    "Career & learning": r"\b(job|career|interview|resume|salary|learn\w*|course|students?|teach\w*|class)\b",
}
TOPICS = {k: re.compile(v, re.I) for k, v in TOPICS.items()}
SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def detect(p):
    title, body = p["title"], p["body"][:3000]
    text = f"{title}\n{body}"
    raw, kinds, hits = 0.0, Counter(), []
    for rx, w, k in P:
        m_t = rx.search(title)
        m_b = rx.search(body)
        if m_t or m_b:
            ww = w * (1.3 if m_t else 1.0)
            raw += ww
            kinds[k] += ww
            hits.append((m_t or m_b).group(0).lower())
    if title.strip().endswith("?"):
        raw += 1.0
    if "?" in body:
        raw += 0.5
    neg = len(NEG.findall(text))
    links = len(re.findall(r"https?://", body))
    raw -= 3.0 * min(neg, 2) + (1.5 if links >= 3 else 0)
    if p["subreddit"].lower() in EXCLUDE:
        raw -= 50
    raw -= 2.0 * min(len(PERSONAL.findall(text)), 3)
    strong = len(STRONG.findall(text))
    excl = text.count("!")
    caps = len(re.findall(r"\b[A-Z]{4,}\b", text))
    intensity = min(100, int(20 + 9 * kinds.get("pain", 0) / 3 * 3 + 10 * strong + 3 * min(excl, 5) + 3 * min(caps, 4) + 8 * p.get("metoo", 0)))
    # statement: best sentence by pattern weight
    best, best_w = title, -1
    for s in [title] + [x.strip() for x in SENT.split(body) if 15 < len(x.strip()) < 400]:
        w = sum(wt for rx, wt, _ in P if rx.search(s)) + (0.8 if s.endswith("?") else 0) + (0.6 if s is title else 0)
        if w > best_w:
            best, best_w = s, w
    stmt = re.sub(r"\s+", " ", best).strip()
    if len(stmt) > 190:
        stmt = stmt[:187].rsplit(" ", 1)[0] + "..."
    topics = [k for k, rx in TOPICS.items() if rx.search(text)][:2] or ["Other"]
    kind = kinds.most_common(1)[0][0] if kinds else "pain"
    sc, nc = p.get("score") or 0, p.get("num_comments") or p.get("num_comments_seen") or 0
    problem_score = max(0, min(100, int(round(raw * 7))))
    age_days = max(0.0, (time.time() - (p["created_utc"] or time.time())) / 86400)
    boost = 1.25 if kind in ("tool", "alternative", "cost") else 1.0
    rank = boost * problem_score * (1 + 0.12 * math.log1p(nc + max(sc, 0) / 4 + 3 * p.get("metoo", 0))) * (0.5 ** (age_days / 14))
    p.update({
        "is_problem": raw >= CFG.get("problem_threshold", 3.0),
        "problem_raw": round(raw, 2), "problem_score": problem_score, "intensity": intensity,
        "statement": stmt, "category": KIND_LABEL[kind], "topics": topics,
        "signals": sorted(set(hits))[:6], "rank": round(rank, 2), "age_days": round(age_days, 1),
    })
    return p


# ---------------------------------------------------------------- LLM (optional)
LLM_PROMPT = """You review Reddit posts and decide whether the author expresses a real problem, pain or unmet need
(not self-promotion, not news, not a meme). For each post return JSON:
{"items":[{"id":"...","is_problem":true|false,"statement":"one line, plain English, max 20 words, what they struggle with or need","intensity":0-100}]}
Posts:
"""


def llm_call(prompt):
    if os.environ.get("OPENAI_API_KEY"):
        body = {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", json.dumps(body).encode(),
                                     {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(json.loads(r.read())["choices"][0]["message"]["content"]), "openai"
    if os.environ.get("GEMINI_API_KEY"):
        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={os.environ['GEMINI_API_KEY']}"
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]), "gemini"
    return None, None


def llm_refine(posts, limit=80):
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        return None
    cand = sorted([p for p in posts if p["problem_raw"] >= 1.5], key=lambda p: -p["rank"])[:limit]
    used = None
    for i in range(0, len(cand), 20):
        batch = cand[i:i + 20]
        prompt = LLM_PROMPT + "\n".join(json.dumps({"id": p["id"], "title": p["title"], "body": p["body"][:600]}) for p in batch)
        try:
            data, used = llm_call(prompt)
        except Exception as e:
            log(f"LLM refine skipped: {e}")
            return None
        for it in (data or {}).get("items", []):
            p = next((x for x in batch if x["id"] == it.get("id")), None)
            if not p:
                continue
            p["is_problem"] = bool(it.get("is_problem"))
            if it.get("statement"):
                p["statement"] = str(it["statement"])[:190]
            if isinstance(it.get("intensity"), (int, float)):
                p["intensity"] = int(it["intensity"])
            p["llm"] = True
    return used


# ---------------------------------------------------------------- cluster
STOP = set("""a about above after again against all am an and any are as at be because been before being below between both but by can
could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it its
itself just me more most my myself no nor not now of off on once only or other our ours out over own same she should so some such than
that the their them then there these they this those through to too under until up very was we were what when where which while who why
will with would you your yours im ive dont cant get got getting anyone anybody someone does like really want need know think one also
make much way thing things still even use using used looking help tool tools app apps best good new any going time people etc yes thanks
lot something every year years month day week back go see first sure since around ever well already able
frustrat frustrated frustrating struggl struggle struggling alternative alternatives tired sick hate wish recommend recommendation
recommendations advice tips tip find anyone else stuck cause feel trying try guys hey hi help way best looking manage annoying
does doesnt work actually just right""".split())


def toks(s):
    out = []
    for w in re.findall(r"[a-z][a-z0-9']{2,}", s.lower()):
        w = w.replace("'", "")
        if w in STOP:
            continue
        for suf in ("ings", "ing", "ers", "ies", "es", "ed", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)] + ("y" if suf == "ies" else "")
                break
        if w not in STOP:
            out.append(w)
    return out


def cluster(problems, thr=0.28):
    docs = [Counter(toks(p["statement"] + " " + p["title"] + " " + p["title"])) for p in problems]
    df = Counter(t for d in docs for t in d)
    n = len(docs) or 1
    vecs = []
    for d in docs:
        v = {t: (1 + math.log(c)) * math.log((n + 1) / (df[t] + 0.5)) for t, c in d.items() if df[t] >= 1}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        vecs.append({t: x / norm for t, x in v.items()})
    clusters = []  # each: {"c": centroid dict, "m": [idx]}
    order = sorted(range(len(problems)), key=lambda i: -problems[i]["rank"])
    for i in order:
        v = vecs[i]
        best, bs = None, 0.0
        for c in clusters:
            s = sum(x * c["c"].get(t, 0) for t, x in v.items())
            if s > bs:
                best, bs = c, s
        if best and bs >= thr:
            best["m"].append(i)
            k = len(best["m"])
            cen = defaultdict(float)
            for t, x in best["c"].items():
                cen[t] += x * (k - 1) / k
            for t, x in v.items():
                cen[t] += x / k
            norm = math.sqrt(sum(x * x for x in cen.values())) or 1
            best["c"] = {t: x / norm for t, x in cen.items()}
        else:
            clusters.append({"c": dict(v), "m": [i]})
    out = []
    for c in clusters:
        mem = [problems[i] for i in c["m"]]
        if len(mem) < 2:
            continue
        tf = Counter(t for p in mem for t in set(toks(p["statement"] + " " + p["title"])))
        terms = [t for t, k in tf.most_common(12) if k >= 2][:4] or [t for t, _ in sorted(c["c"].items(), key=lambda kv: -kv[1])[:3]]
        top = max(mem, key=lambda p: p["rank"])
        out.append({
            "id": f"c{len(out) + 1}", "label": " / ".join(terms), "size": len(mem),
            "subreddits": sorted({p["subreddit"] for p in mem}),
            "topics": [t for t, _ in Counter(t for p in mem for t in p["topics"]).most_common(2)],
            "avg_intensity": round(sum(p["intensity"] for p in mem) / len(mem)),
            "total_comments": sum((p.get("num_comments") or 0) for p in mem),
            "example": top["statement"], "member_ids": [p["id"] for p in mem],
            "heat": round(sum(p["rank"] for p in mem), 1),
        })
        for p in mem:
            p["cluster"] = out[-1]["id"]
    out.sort(key=lambda c: (-c["size"], -c["heat"]))
    return out


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    cache = os.path.join(ROOT, "data", ".cache_posts.json")
    if "--reuse" in sys.argv and os.path.exists(cache):
        posts, stats = json.load(open(cache))
        for p in posts:
            p.pop("metoo", None)
    else:
        posts, stats = collect()
    if not posts:
        log("No posts collected. Keeping previous data/problems.json.")
        sys.exit(1)
    if "--reuse" not in sys.argv:
        stats["engagement_found"] = enrich_engagement(posts)
    for p in posts:
        detect(p)
    cand = sorted([p for p in posts if p["is_problem"]], key=lambda p: -p["rank"])[: CFG.get("comments_for_top_n", 30)]
    if "--reuse" not in sys.argv:
        stats["comment_threads_read"] = sum(1 for p in cand if enrich_comments(p))
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        json.dump([posts, stats], open(cache, "w"))
    else:
        for p in posts:
            if p.get("top_comments") is not None:
                p["metoo"] = sum(1 for c in p["top_comments"] if METOO.search(c["text"]))
    for p in cand:
        detect(p)  # re-score with me-too signals
    stats["llm"] = llm_refine(posts) or "none (heuristics only)"
    problems = sorted([p for p in posts if p["is_problem"]], key=lambda p: -p["rank"])
    clusters = cluster(problems)
    keep = ("id", "title", "statement", "subreddit", "author", "url", "created_utc", "age_days", "score", "num_comments",
            "num_comments_seen", "problem_score", "intensity", "category", "topics", "signals", "rank", "cluster",
            "top_comments", "metoo", "sources", "llm")
    out_p = []
    for p in problems:
        d = {k: p[k] for k in keep if k in p}
        d["excerpt"] = p["body"][:420]
        out_p.append(d)
    stats.update({"problems": len(out_p), "clusters": len(clusters), "seconds": round(time.time() - t0)})
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "demo": False,
        "source": "Reddit public RSS feeds (read-only) + Arctic Shift archive for score/comment counts",
        "config": {"subreddits": CFG["subreddits"], "searches": CFG.get("searches", [])},
        "stats": stats, "categories": dict(Counter(p["category"] for p in out_p)),
        "problems": out_p, "clusters": clusters,
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "problems.json"), "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    log(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
